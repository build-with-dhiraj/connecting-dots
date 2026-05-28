"""Smart title rewriter backfill worker.

Walk every note in vault/sources/ and vault/inbox/, detect garbage titles using
`connecting_dots.enrichment.title.needs_rewrite`, call Azure OpenAI to generate
a clean noun-phrase title, and atomically rewrite the frontmatter.

Idempotency: skip notes where `raw_meta.original_title` is already set.

Usage
-----
    python -m workers.title_backfill [--limit N] [--concurrency 4] [--dry-run]
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

from connecting_dots.enrichment.title import derive_better_title, needs_rewrite, rewrite
from lib.vault_writer.writer import _resolve_vault_root

log = logging.getLogger("title_backfill")

DEFAULT_CONCURRENCY = 4

_SKIP_RELATIVE_PATHS = {"inbox/example.md"}
_SKIP_DIR_PREFIXES = ("inbox/_failed/", "_failed/", ".trash/")

# Stable frontmatter key order (must match ner_backfill.py)
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


# --------------------------------------------------------------------------- #
# Frontmatter helpers
# --------------------------------------------------------------------------- #
def _split_frontmatter(text: str) -> tuple[Optional[dict[str, Any]], str]:
    """Return (frontmatter_dict, body) or (None, text) on parse failure."""
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


def _is_already_rewritten(fm: dict[str, Any]) -> bool:
    raw_meta = fm.get("raw_meta") or {}
    return bool(isinstance(raw_meta, dict) and raw_meta.get("original_title"))


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
    fd, tmp = tempfile.mkstemp(prefix=".tmp-title-", suffix=".md", dir=str(parent))
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

    if _is_already_rewritten(fm):
        return {"status": "skipped_idempotent", "path": rel}

    old_title = str(fm.get("title") or "")
    if not needs_rewrite(old_title):
        return {"status": "skipped_good_title", "path": rel}

    if dry_run:
        log.info("  [dry-run] Would rewrite '%s' in %s", old_title, rel)
        return {"status": "dry_run", "path": rel, "old_title": old_title}

    result = rewrite(
        old_title=old_title,
        body=body,
        vault_path=rel,
        model=model,
    )

    if result.error or not result.title:
        new_fm = dict(fm)
        raw_meta = dict(new_fm.get("raw_meta") or {})
        raw_meta["title_rewrite_error"] = (result.error or "empty title")[:500]
        new_fm["raw_meta"] = raw_meta
        try:
            _write_note_atomic(note_path, new_fm, body)
        except OSError:
            pass
        return {"status": "error", "path": rel, "error": result.error}

    new_fm = dict(fm)
    new_fm["title"] = result.title
    raw_meta = dict(new_fm.get("raw_meta") or {})
    raw_meta["original_title"] = old_title
    raw_meta["title_rewritten_at"] = _now_iso()
    raw_meta["title_model"] = model or os.environ.get("TITLE_MODEL") or "gpt-4.1"
    raw_meta.pop("title_rewrite_error", None)
    new_fm["raw_meta"] = raw_meta

    try:
        _write_note_atomic(note_path, new_fm, body)
    except OSError as e:
        return {"status": "write_error", "path": rel, "error": str(e)}

    return {
        "status": "ok",
        "path": rel,
        "old_title": old_title,
        "new_title": result.title,
    }


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
        total=len(paths), desc="title-rewrite", unit="note", disable=not sys.stderr.isatty()
    )

    async def _one(p: Path) -> None:
        async with sem:
            res = await asyncio.to_thread(
                _process_one_sync, p, model=model, vault_root=vault_root, dry_run=dry_run
            )
        status = res.get("status", "")
        if status == "ok":
            counts["ok"] += 1
        elif status in ("skipped_idempotent", "skipped_good_title", "no_frontmatter"):
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
# Fix-untitled helpers
# --------------------------------------------------------------------------- #
def _is_untitled_candidate(fm: dict) -> bool:
    """True if the note was rewritten but produced 'Untitled Note' and has not
    yet been fixed by the v2 pass."""
    raw_meta = fm.get("raw_meta") or {}
    if not isinstance(raw_meta, dict):
        return False
    if not raw_meta.get("original_title"):
        return False
    if str(fm.get("title") or "").strip() != "Untitled Note":
        return False
    if raw_meta.get("title_v2_at"):
        return False
    return True


def _process_untitled_sync(
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

    if not _is_untitled_candidate(fm):
        return {"status": "skipped", "path": rel}

    if dry_run:
        log.info("  [dry-run] Would fix untitled: %s", rel)
        return {"status": "dry_run", "path": rel}

    new_title, title_source = derive_better_title(fm, model=model)

    if not new_title or new_title.lower() == "untitled note":
        return {
            "status": "error",
            "path": rel,
            "error": "derive_better_title returned empty/untitled",
        }

    new_fm = dict(fm)
    new_fm["title"] = new_title
    raw_meta = dict(new_fm.get("raw_meta") or {})
    raw_meta["title_v2_at"] = _now_iso()
    raw_meta["title_v2_source"] = title_source
    new_fm["raw_meta"] = raw_meta

    try:
        _write_note_atomic(note_path, new_fm, body)
    except OSError as e:
        return {"status": "write_error", "path": rel, "error": str(e)}

    return {
        "status": "ok",
        "path": rel,
        "old_title": "Untitled Note",
        "new_title": new_title,
        "source": title_source,
    }


async def _run_untitled_batch(
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
        total=len(paths), desc="fix-untitled", unit="note", disable=not sys.stderr.isatty()
    )

    async def _one(p: Path) -> None:
        async with sem:
            res = await asyncio.to_thread(
                _process_untitled_sync,
                p,
                model=model,
                vault_root=vault_root,
                dry_run=dry_run,
            )
        status = res.get("status", "")
        if status == "ok":
            counts["ok"] += 1
            log.info(
                "  fixed: %s -> %s [%s]",
                res.get("path"),
                res.get("new_title"),
                res.get("source"),
            )
        elif status == "skipped":
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
        prog="title_backfill",
        description="Rewrite garbage note titles with clean noun phrases via Azure OpenAI.",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--model", default=None)
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    parser.add_argument(
        "--fix-untitled",
        action="store_true",
        help=(
            "Re-title only notes still named 'Untitled Note' using smarter "
            "fallback chain (filename → entity LLM → date+source)."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    vault_root = _resolve_vault_root()
    all_paths = list(_iter_vault_notes(vault_root))
    if args.limit:
        all_paths = all_paths[: args.limit]

    if args.fix_untitled:
        log.info(
            "fix-untitled mode: scanning %d notes, concurrency=%d, dry_run=%s",
            len(all_paths),
            args.concurrency,
            args.dry_run,
        )
        counts = asyncio.run(
            _run_untitled_batch(
                all_paths,
                concurrency=max(1, args.concurrency),
                model=args.model,
                vault_root=vault_root,
                dry_run=args.dry_run,
            )
        )
        log.info(
            "Done (fix-untitled). ok=%d skipped=%d errors=%d dry_run=%d",
            counts["ok"],
            counts["skipped"],
            counts["error"],
            counts["dry_run"],
        )
        return 0

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
