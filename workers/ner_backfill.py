"""NER backfill + live enrichment worker (component #8).

Two modes:

- **One-shot sweep (default):** walk every `.md` under `vault/sources/` and
  `vault/inbox/` (excluding `vault/inbox/_failed/` and `vault/inbox/example.md`),
  enrich any note that doesn't already have entities or an
  `raw_meta.ner_enriched_at` marker. Used for the initial 1,464-note backfill
  and for any time you want to re-sweep the vault.

- **Live mode (`--watch`):** poll `data/ner_queue.txt` every interval and
  process each path. The stream consumer (#6) appends to this queue after
  `write_note()` returns, so newly-written notes get enriched within seconds
  without back-pressuring the write path.

Idempotency
-----------

A note is skipped if either:
  (a) `entities` is already a non-empty list, OR
  (b) `raw_meta.ner_enriched_at` is set (even with empty entities — meaning
      the extractor *ran* but legitimately found nothing).

This means a re-run after a partial sweep continues from where it stopped,
and a successful enrichment that legitimately found zero entities isn't
re-attempted on every sweep.

Error handling
--------------

`extract()` never raises — on any API/parse failure it returns an empty
result and the trace logs the error. The backfill writes
`raw_meta.ner_error: <reason>` into the frontmatter so the user can grep for
problem notes, then continues with the next file. One bad note never
crashes the loop.

Concurrency
-----------

Up to `NER_CONCURRENCY` (env, default 4) extractions run in flight
concurrently via `asyncio.to_thread()` — the `openai` SDK's sync `AzureOpenAI`
client is thread-safe and releases the GIL during HTTP, so we get real overlap.
Each note's frontmatter rewrite is its own atomic tmp+rename so concurrent
workers can't collide on disk.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re as _re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml
from tqdm import tqdm

from connecting_dots.enrichment import extract
from connecting_dots.enrichment.queue import drain_queue
from lib.vault_writer.writer import _resolve_vault_root

log = logging.getLogger("ner_backfill")

DEFAULT_CONCURRENCY = 4
DEFAULT_WATCH_INTERVAL_S = 5.0

# Files to skip even if they live under the scanned roots.
_SKIP_RELATIVE_PATHS = {"inbox/example.md"}
_SKIP_DIR_PREFIXES = ("inbox/_failed/", "_failed/", ".trash/")


# --------------------------------------------------------------------------- #
# Frontmatter read / write
# --------------------------------------------------------------------------- #
def _split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str, str]:
    """Return (frontmatter_dict, raw_frontmatter_str, body).

    Returns `(None, "", text)` when the file has no `---` block — we treat
    that as a malformed note and skip rather than fabricating frontmatter.
    """
    if not text.startswith("---\n"):
        return None, "", text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, "", text
    raw_fm = text[4:end]
    body = text[end + 5 :]
    try:
        fm = yaml.safe_load(raw_fm) or {}
    except yaml.YAMLError:
        return None, raw_fm, body
    if not isinstance(fm, dict):
        return None, raw_fm, body
    return fm, raw_fm, body


def _is_already_enriched(fm: dict[str, Any]) -> bool:
    entities = fm.get("entities") or []
    if isinstance(entities, list) and entities:
        return True
    raw_meta = fm.get("raw_meta") or {}
    if isinstance(raw_meta, dict) and raw_meta.get("ner_enriched_at"):
        return True
    return False


# Stable frontmatter key order — must match `lib/vault_writer/writer.py`
# so re-serialization doesn't reshuffle existing notes' keys. `tags` is
# inserted between `title` and `entities` so it sits with the other
# user-facing labels at the top of the YAML block.
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
# Entity / topic → tag mirroring
# --------------------------------------------------------------------------- #
_TAG_DROP_RE = _re.compile(r"[^a-z0-9\-]+")
_TAG_COLLAPSE_RE = _re.compile(r"-{2,}")


def _slugify_tag_part(value: str) -> str:
    """Slugify a single entity/topic name into a tag-safe segment.

    Lowercase, replace whitespace with `-`, drop everything that isn't
    `[a-z0-9-]`, collapse runs of `-`, trim leading/trailing `-`.
    Returns `""` if nothing usable survives.
    """
    if not value:
        return ""
    s = value.strip().lower().replace("_", "-")
    s = _re.sub(r"\s+", "-", s)
    s = _TAG_DROP_RE.sub("-", s)
    s = _TAG_COLLAPSE_RE.sub("-", s)
    return s.strip("-")


def _merge_tags(existing: Any, new_tags: list[str]) -> list[str]:
    """Set-union merge of existing tags + new tags. Preserves manual entries.

    Obsidian accepts `tags:` as either a list or a single string; we always
    write back a sorted list of `#…` strings so reruns are deterministic.
    """
    out: set[str] = set()
    if isinstance(existing, list):
        for item in existing:
            if isinstance(item, str) and item.strip():
                out.add(item.strip())
    elif isinstance(existing, str) and existing.strip():
        for piece in existing.split():
            if piece.strip():
                out.add(piece.strip())
    for tag in new_tags:
        if tag:
            out.add(tag)
    return sorted(out)


def _entity_topic_tags(entities: list[str], topics: list[str]) -> list[str]:
    """Build `#entity/<slug>` and `#topic/<slug>` tags from extraction output."""
    tags: list[str] = []
    for e in entities or []:
        slug = _slugify_tag_part(str(e))
        if slug:
            tags.append(f"#entity/{slug}")
    for t in topics or []:
        slug = _slugify_tag_part(str(t))
        if slug:
            tags.append(f"#topic/{slug}")
    return tags


def _ordered(meta: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in _FRONTMATTER_ORDER:
        if k in meta:
            out[k] = meta[k]
    for k in sorted(meta):
        if k not in out:
            out[k] = meta[k]
    return out


def _write_note_atomic(path: Path, fm: dict[str, Any], body: str) -> None:
    """Atomic frontmatter rewrite: tmp + rename, same pattern as vault writer."""
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
    fd, tmp = tempfile.mkstemp(prefix=".tmp-ner-", suffix=".md", dir=str(parent))
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
# Vault walking
# --------------------------------------------------------------------------- #
def _iter_vault_notes(vault_root: Path) -> Iterable[Path]:
    """Yield every candidate `.md` under sources/ and inbox/, in stable order."""
    roots = [vault_root / "sources", vault_root / "inbox"]
    for root in roots:
        if not root.exists():
            continue
        # rglob in sorted order so retries see the same sequence.
        for path in sorted(root.rglob("*.md")):
            rel = path.relative_to(vault_root).as_posix()
            if rel in _SKIP_RELATIVE_PATHS:
                continue
            if any(rel.startswith(prefix) for prefix in _SKIP_DIR_PREFIXES):
                continue
            yield path


# --------------------------------------------------------------------------- #
# Per-note enrichment
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _enrich_one_sync(note_path: Path, model: Optional[str], vault_root: Path) -> dict[str, Any]:
    """Read → extract → rewrite. Returns a small result dict for the progress bar.

    Errors during extraction are recorded in `raw_meta.ner_error` and the
    function returns `{"status": "error", ...}` — the worker continues.
    """
    rel = note_path.relative_to(vault_root).as_posix()

    try:
        text = note_path.read_text(encoding="utf-8")
    except OSError as e:
        return {"status": "read_error", "path": rel, "error": str(e)}

    fm, _raw_fm, body = _split_frontmatter(text)
    if fm is None:
        return {"status": "no_frontmatter", "path": rel}

    if _is_already_enriched(fm):
        return {"status": "skipped", "path": rel}

    title = str(fm.get("title") or "")
    # Body for extraction: strip the H1 the writer prepends to avoid double-
    # counting the title. Otherwise pass the full body (`extract` truncates).
    extraction_body = body.lstrip()
    if extraction_body.startswith("# "):
        # Drop the first line + blank separator.
        parts = extraction_body.split("\n", 2)
        if len(parts) >= 3:
            extraction_body = parts[2]

    result = extract(
        title=title,
        body=extraction_body,
        vault_path=rel,
        model=model,
    )

    # Build the updated frontmatter — preserve everything else verbatim.
    new_fm = dict(fm)
    new_fm["entities"] = list(result.entities)
    new_fm["topics"] = list(result.topics)

    # Mirror entities + topics into `tags:` so Obsidian's graph view + tag
    # panel cluster notes by semantic concept. Merged as a set union so
    # `#source/*` / `#ingest/*` tags from workers.domain_tag_backfill are
    # preserved, and re-runs are byte-stable. On extraction failure we still
    # keep whatever tags are already on the note — we just don't add the
    # entity/topic ones.
    if not result.error:
        mirrored = _entity_topic_tags(list(result.entities), list(result.topics))
        if mirrored:
            new_fm["tags"] = _merge_tags(new_fm.get("tags"), mirrored)

    raw_meta = dict(new_fm.get("raw_meta") or {})
    if result.error:
        # Failed extraction: record the error but do NOT stamp ner_enriched_at /
        # ner_model. The note must remain eligible for retry on the next sweep.
        raw_meta["ner_error"] = result.error[:500]
    else:
        raw_meta["ner_enriched_at"] = _now_iso()
        raw_meta["ner_model"] = model or os.environ.get("NER_MODEL") or "gpt-4.1"
        raw_meta.pop("ner_error", None)
    new_fm["raw_meta"] = raw_meta

    try:
        _write_note_atomic(note_path, new_fm, body)
    except OSError as e:
        return {"status": "write_error", "path": rel, "error": str(e)}

    if result.error:
        return {"status": "error", "path": rel, "error": result.error}

    return {
        "status": "ok",
        "path": rel,
        "entities": len(result.entities),
        "topics": len(result.topics),
        "cached_in": result.cached_input_tokens,
    }


# --------------------------------------------------------------------------- #
# Async dispatcher
# --------------------------------------------------------------------------- #
async def _run_batch(paths: list[Path], *, concurrency: int, model: Optional[str], vault_root: Path) -> dict[str, int]:
    """Process `paths` with up to `concurrency` extractions in flight.

    Returns a small counter dict for the final summary.
    """
    sem = asyncio.Semaphore(concurrency)
    counts: dict[str, int] = {"ok": 0, "skipped": 0, "error": 0, "no_fm": 0}

    progress = tqdm(total=len(paths), desc="enrich", unit="note", disable=not sys.stderr.isatty())

    async def _one(p: Path) -> None:
        async with sem:
            res = await asyncio.to_thread(_enrich_one_sync, p, model, vault_root)
        status = res.get("status")
        if status == "ok":
            counts["ok"] += 1
        elif status == "skipped":
            counts["skipped"] += 1
        elif status == "no_frontmatter":
            counts["no_fm"] += 1
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
def _resolve_concurrency(cli_value: Optional[int]) -> int:
    if cli_value is not None:
        return max(1, cli_value)
    env = os.environ.get("NER_CONCURRENCY", "").strip()
    if env.isdigit() and int(env) > 0:
        return int(env)
    return DEFAULT_CONCURRENCY


def _one_shot_sweep(limit: Optional[int], concurrency: int, model: Optional[str]) -> dict[str, int]:
    vault_root = _resolve_vault_root()
    all_paths = list(_iter_vault_notes(vault_root))
    if limit is not None and limit > 0:
        all_paths = all_paths[:limit]
    if not all_paths:
        log.info("No notes to process under %s", vault_root)
        return {"ok": 0, "skipped": 0, "error": 0, "no_fm": 0}

    log.info(
        "Sweeping %d note(s) under %s, concurrency=%d, model=%s",
        len(all_paths),
        vault_root,
        concurrency,
        model or os.environ.get("NER_MODEL") or "gpt-4.1",
    )
    return asyncio.run(
        _run_batch(all_paths, concurrency=concurrency, model=model, vault_root=vault_root)
    )


def _watch_loop(interval_s: float, concurrency: int, model: Optional[str]) -> None:
    """Drain the enrichment queue every `interval_s` until SIGTERM/Ctrl-C."""
    vault_root = _resolve_vault_root()
    log.info("Watch mode: polling enrichment queue every %.1fs", interval_s)
    while True:
        try:
            raw_paths = drain_queue()
        except Exception:
            log.exception("Failed to drain enrichment queue")
            time.sleep(interval_s)
            continue

        if not raw_paths:
            time.sleep(interval_s)
            continue

        # Resolve each queued path against the vault root if it's relative.
        resolved: list[Path] = []
        for raw in raw_paths:
            p = Path(raw)
            if not p.is_absolute():
                p = vault_root / p
            if p.exists():
                resolved.append(p)
            else:
                log.warning("Queued path no longer exists, dropping: %s", raw)

        if not resolved:
            continue

        log.info("Draining %d queued path(s)", len(resolved))
        asyncio.run(
            _run_batch(resolved, concurrency=concurrency, model=model, vault_root=vault_root)
        )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ner_backfill",
        description=(
            "NER + topic extraction backfill / live worker. "
            "Default: one-shot sweep over the vault."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N notes (for safe small-batch testing).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help=f"Max concurrent extractions (default {DEFAULT_CONCURRENCY}, env NER_CONCURRENCY).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override NER_MODEL env var (default: gpt-4.1 Azure deployment).",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help=(
            "Live mode: poll data/ner_queue.txt and process queued paths. "
            "Runs until Ctrl-C."
        ),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_WATCH_INTERVAL_S,
        help=f"Watch poll interval in seconds (default {DEFAULT_WATCH_INTERVAL_S}).",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        help="Logging level (default INFO).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    concurrency = _resolve_concurrency(args.concurrency)

    if args.watch:
        try:
            _watch_loop(args.interval, concurrency, args.model)
        except KeyboardInterrupt:
            log.info("Watch loop interrupted, exiting.")
        return 0

    counts = _one_shot_sweep(args.limit, concurrency, args.model)
    log.info(
        "Done. ok=%d skipped=%d errors=%d no_frontmatter=%d",
        counts["ok"],
        counts["skipped"],
        counts["error"],
        counts["no_fm"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
