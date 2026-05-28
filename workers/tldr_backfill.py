"""2-sentence TL;DR backfill worker.

Walk every note, skip those already processed (raw_meta.tldr_at set) or too
short (< 200 chars of body), call Azure OpenAI to generate a 2-sentence TL;DR,
and prepend it to the note body.

Usage
-----
    python -m workers.tldr_backfill [--limit N] [--concurrency 4] [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml
from tqdm import tqdm

from connecting_dots.enrichment.tldr import MIN_BODY_CHARS, extract
from lib.vault_writer.writer import _resolve_vault_root

log = logging.getLogger("tldr_backfill")

DEFAULT_CONCURRENCY = 4

_SKIP_RELATIVE_PATHS = {"inbox/example.md"}
_SKIP_DIR_PREFIXES = ("inbox/_failed/", "_failed/", ".trash/")

_FRONTMATTER_ORDER = (
    "source",
    "handler",
    "captured_at",
    "url",
    "title",
    "tags",
    "entities",
    "topics",
    "labels",
    "raw_meta",
)

# Avoid double-prepending if someone re-runs
_TLDR_MARKER = "> **TL;DR.**"


# --------------------------------------------------------------------------- #
# Frontmatter helpers (mirrors ner_backfill.py)
# --------------------------------------------------------------------------- #
def _split_frontmatter(text: str) -> tuple[Optional[dict[str, Any]], str]:
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text
    try:
        fm = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError:
        return None, text[end + 5:]
    if not isinstance(fm, dict):
        return None, text[end + 5:]
    return fm, text[end + 5:]


def _ordered(meta: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in _FRONTMATTER_ORDER:
        if k in meta:
            out[k] = meta[k]
    for k in sorted(meta):
        if k not in out:
            out[k] = meta[k]
    return out


def _is_already_tldrd(fm: dict[str, Any], body: str) -> bool:
    raw_meta = fm.get("raw_meta") or {}
    if isinstance(raw_meta, dict) and raw_meta.get("tldr_at"):
        return True
    # Belt-and-suspenders: also check if the TL;DR marker is already in the body
    return _TLDR_MARKER in body


# --------------------------------------------------------------------------- #
# Vault walking
# --------------------------------------------------------------------------- #
def _iter_vault_notes(vault_root: Path) -> Iterable[Path]:
    roots = [vault_root / "sources", vault_root / "inbox"]
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.md")):
            rel = path.relative_to(vault_root).as_posix()
            if rel in _SKIP_RELATIVE_PATHS:
                continue
            if any(rel.startswith(p) for p in _SKIP_DIR_PREFIXES):
                continue
            yield path


# --------------------------------------------------------------------------- #
# Atomic write
# --------------------------------------------------------------------------- #
def _write_note_atomic(path: Path, fm: dict[str, Any], body: str) -> None:
    serialized_fm = yaml.safe_dump(
        _ordered(fm),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).rstrip()
    content = f"---\n{serialized_fm}\n---\n{body}"
    if not content.endswith("\n"):
        content += "\n"

    parent = path.parent
    fd, tmp = tempfile.mkstemp(prefix=".tmp-tldr-", suffix=".md", dir=str(parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.rename(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


# --------------------------------------------------------------------------- #
# Per-note processing
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _process_one_sync(
    note_path: Path,
    *,
    model: Optional[str],
    vault_root: Path,
    dry_run: bool,
) -> dict[str, Any]:
    rel = note_path.relative_to(vault_root).as_posix()

    try:
        text = note_path.read_text(encoding="utf-8")
    except OSError as e:
        return {"status": "read_error", "path": rel, "error": str(e)}

    fm, body = _split_frontmatter(text)
    if fm is None:
        return {"status": "no_frontmatter", "path": rel}

    if _is_already_tldrd(fm, body):
        return {"status": "skipped_idempotent", "path": rel}

    # Skip short notes
    body_text = body.strip()
    if len(body_text) < MIN_BODY_CHARS:
        return {"status": "skipped_too_short", "path": rel, "len": len(body_text)}

    if dry_run:
        log.info("  [dry-run] Would add TL;DR to %s (%d chars)", rel, len(body_text))
        return {"status": "dry_run", "path": rel}

    result = extract(
        body=body_text,
        vault_path=rel,
        model=model,
    )

    if result.error or not result.sentence_1 or not result.sentence_2:
        new_fm = dict(fm)
        raw_meta = dict(new_fm.get("raw_meta") or {})
        raw_meta["tldr_error"] = (result.error or "incomplete sentences")[:500]
        new_fm["raw_meta"] = raw_meta
        try:
            _write_note_atomic(note_path, new_fm, body)
        except OSError:
            pass
        return {"status": "error", "path": rel, "error": result.error}

    # Prepend TL;DR blockquote
    tldr_line = result.as_blockquote()
    # Preserve blank line between frontmatter delimiter and body
    new_body = f"\n{tldr_line}\n\n{body.lstrip()}"

    new_fm = dict(fm)
    raw_meta = dict(new_fm.get("raw_meta") or {})
    raw_meta["tldr_at"] = _now_iso()
    raw_meta["tldr_model"] = model or os.environ.get("TLDR_MODEL") or "gpt-4.1"
    raw_meta.pop("tldr_error", None)
    new_fm["raw_meta"] = raw_meta

    try:
        _write_note_atomic(note_path, new_fm, new_body)
    except OSError as e:
        return {"status": "write_error", "path": rel, "error": str(e)}

    return {"status": "ok", "path": rel}


# --------------------------------------------------------------------------- #
# Async batch runner
# --------------------------------------------------------------------------- #
async def _run_batch(
    paths: list[Path],
    *,
    concurrency: int,
    model: Optional[str],
    vault_root: Path,
    dry_run: bool,
) -> dict[str, int]:
    sem = asyncio.Semaphore(concurrency)
    counts: dict[str, int] = {"ok": 0, "skipped": 0, "error": 0, "dry_run": 0}

    progress = tqdm(
        total=len(paths), desc="tldr", unit="note", disable=not sys.stderr.isatty()
    )

    async def _one(p: Path) -> None:
        async with sem:
            res = await asyncio.to_thread(
                _process_one_sync, p, model=model, vault_root=vault_root, dry_run=dry_run
            )
        status = res.get("status", "")
        if status == "ok":
            counts["ok"] += 1
        elif status in ("skipped_idempotent", "skipped_too_short", "no_frontmatter"):
            counts["skipped"] += 1
        elif status == "dry_run":
            counts["dry_run"] += 1
        else:
            counts["error"] += 1
            log.warning("%s: %s", res.get("path"), res)
        progress.update(1)
        progress.set_postfix(ok=counts["ok"], skip=counts["skipped"], err=counts["error"])

    await asyncio.gather(*(_one(p) for p in paths))
    progress.close()
    return counts


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tldr_backfill",
        description="Prepend 2-sentence TL;DR to all vault notes via Azure OpenAI.",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--model", default=None)
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    vault_root = _resolve_vault_root()
    all_paths = list(_iter_vault_notes(vault_root))
    if args.limit:
        all_paths = all_paths[: args.limit]

    log.info(
        "Processing %d notes, concurrency=%d, dry_run=%s",
        len(all_paths),
        args.concurrency,
        args.dry_run,
    )

    counts = asyncio.run(
        _run_batch(
            all_paths,
            concurrency=max(1, args.concurrency),
            model=args.model,
            vault_root=vault_root,
            dry_run=args.dry_run,
        )
    )
    log.info(
        "Done. ok=%d skipped=%d errors=%d dry_run=%d",
        counts["ok"],
        counts["skipped"],
        counts["error"],
        counts["dry_run"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
