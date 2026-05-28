"""Domain-tag backfill — make Obsidian's graph view immediately useful.

Obsidian's graph view only draws edges from `tags:` (frontmatter or `#hashtag`
in body) and `[[wikilinks]]`. It does **not** read the `entities:` / `topics:`
YAML arrays our NER pipeline populates. So even as NER backfill enriches the
vault, the graph stays a dense dot cloud until we mirror that semantic data
into `tags:`.

This one-shot worker is the cheap, no-LLM half of the unblock:

* It walks every `.md` under `vault/sources/` and `vault/inbox/`.
* For each note it derives 1–3 source-shaped tags from the `url`, `handler`,
  and `source` frontmatter fields:
    - `#source/<domain-slug>`   from the URL's registered domain
    - `#source/<handler-slug>`  from the `handler` field (covers no-URL notes)
    - `#ingest/<source-slug>`   from the `source` field (where it entered)
* It merges these into the note's existing `tags:` (set union — never replaces).
* Writes atomically via tmp + rename, matching `lib/vault_writer.writer`.
* Idempotent: re-running adds no new tags.

The companion piece (entity/topic → tag mirroring) lives in
`workers.ner_backfill._enrich_one_sync` so freshly-enriched notes get the
full set of cluster tags written in a single atomic rewrite.

CLI
---

    python -m workers.domain_tag_backfill              # full sweep
    python -m workers.domain_tag_backfill --limit 5    # small-batch live test
    python -m workers.domain_tag_backfill --dry-run    # log changes, write nothing
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

import tldextract
import yaml
from tqdm import tqdm

from lib.vault_writer.writer import _resolve_vault_root

log = logging.getLogger("domain_tag_backfill")

# Files to skip even if they live under the scanned roots — mirrors
# workers.ner_backfill so both backfills agree on the universe of notes.
_SKIP_RELATIVE_PATHS = {"inbox/example.md"}
_SKIP_DIR_PREFIXES = ("inbox/_failed/", "_failed/", ".trash/")

# Stable frontmatter key order — keep aligned with lib/vault_writer/writer.py
# so re-serialization doesn't shuffle existing notes' keys.
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

# Canonical domain → source slug. Anything not in this table falls back to the
# `tldextract` registered domain (e.g. `nseindia.com` → `nseindia`).
# Keys are lowercased registered_domain values.
_DOMAIN_CANONICAL = {
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "linkedin.com": "linkedin",
    "instagram.com": "instagram",
    "twitter.com": "x",
    "x.com": "x",
    "reddit.com": "reddit",
    "substack.com": "substack",
    "medium.com": "medium",
    "github.com": "github",
}


# --------------------------------------------------------------------------- #
# Slug helpers
# --------------------------------------------------------------------------- #
_SLUG_DROP_RE = re.compile(r"[^a-z0-9/\-]+")
_SLUG_COLLAPSE_RE = re.compile(r"-{2,}")


def _slugify_tag(value: str) -> str:
    """Lowercase, replace whitespace with `-`, drop everything that isn't
    `[a-z0-9/-]`. Collapse runs of `-`. Trim leading/trailing `-`.

    Returns `""` if nothing usable survives — caller must drop empty results.
    """
    if not value:
        return ""
    s = value.strip().lower().replace("_", "-")
    s = re.sub(r"\s+", "-", s)
    s = _SLUG_DROP_RE.sub("-", s)
    s = _SLUG_COLLAPSE_RE.sub("-", s)
    return s.strip("-")


# --------------------------------------------------------------------------- #
# Tag derivation
# --------------------------------------------------------------------------- #
def _domain_slug_from_url(url: str) -> Optional[str]:
    """Resolve `url` to a canonical domain slug, or None if the URL is empty /
    not a real http(s) URL.

    Uses `tldextract` to get the registered domain, then maps well-known
    sites to short canonical slugs; falls back to the unmapped domain name.
    """
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    if not parsed.netloc:
        return None

    extracted = tldextract.extract(url)
    # registered_domain is deprecated in newer tldextract — prefer the new name.
    registered = (
        getattr(extracted, "top_domain_under_public_suffix", None)
        or extracted.registered_domain
        or ""
    ).lower()

    if not registered:
        return None

    if registered in _DOMAIN_CANONICAL:
        return _DOMAIN_CANONICAL[registered]

    # *.substack.com / *.medium.com etc — match on the registered domain too.
    if registered.endswith(".substack.com") or registered == "substack.com":
        return "substack"

    # Default: use the bare domain name (the part before the public suffix).
    return _slugify_tag(extracted.domain or registered.split(".")[0])


def _handler_source_slug(handler: str) -> Optional[str]:
    """`handler=raw` → `whatsapp-raw` (audio/image/text from WhatsApp export).
    Other handlers map 1:1 to the source slug after slugifying.
    """
    h = (handler or "").strip().lower()
    if not h:
        return None
    if h == "raw":
        return "whatsapp-raw"
    return _slugify_tag(h)


def derive_source_tags(fm: dict[str, Any]) -> list[str]:
    """Return the source-shaped tags this note should carry.

    Always returns plain `#…` strings, deduped, in stable sorted order so two
    runs over the same note produce byte-identical frontmatter.
    """
    tags: set[str] = set()

    url = str(fm.get("url") or "")
    handler = str(fm.get("handler") or "")
    source = str(fm.get("source") or "")

    domain_slug = _domain_slug_from_url(url)
    if domain_slug:
        tags.add(f"#source/{domain_slug}")

    handler_slug = _handler_source_slug(handler)
    if handler_slug:
        tags.add(f"#source/{handler_slug}")

    source_slug = _slugify_tag(source)
    if source_slug:
        tags.add(f"#ingest/{source_slug}")

    return sorted(tags)


# --------------------------------------------------------------------------- #
# Tag set union
# --------------------------------------------------------------------------- #
def merge_tags(existing: Any, new_tags: Iterable[str]) -> list[str]:
    """Union `existing` (whatever shape it has) with `new_tags`.

    Obsidian accepts `tags:` as either a list of strings or a single string.
    We always emit a sorted list of `#…` strings. Existing entries are
    preserved verbatim — we only ADD, never strip — so manual user tags survive.
    """
    out: set[str] = set()

    if isinstance(existing, list):
        for item in existing:
            if isinstance(item, str) and item.strip():
                out.add(item.strip())
    elif isinstance(existing, str) and existing.strip():
        # Single-string form: split on whitespace, defensively.
        for piece in existing.split():
            if piece.strip():
                out.add(piece.strip())

    for tag in new_tags:
        if tag:
            out.add(tag)

    return sorted(out)


# --------------------------------------------------------------------------- #
# Frontmatter I/O
# --------------------------------------------------------------------------- #
def _split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str]:
    """Return (frontmatter_dict, body). (None, text) if not parseable."""
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text
    raw_fm = text[4:end]
    body = text[end + 5 :]
    try:
        fm = yaml.safe_load(raw_fm) or {}
    except yaml.YAMLError:
        return None, body
    if not isinstance(fm, dict):
        return None, body
    return fm, body


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
    """tmp + rename, identical to ner_backfill — concurrent writers can't tear."""
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
    fd, tmp = tempfile.mkstemp(prefix=".tmp-tags-", suffix=".md", dir=str(parent))
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
# Vault walk
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
            if any(rel.startswith(prefix) for prefix in _SKIP_DIR_PREFIXES):
                continue
            yield path


# --------------------------------------------------------------------------- #
# Per-note tag application
# --------------------------------------------------------------------------- #
def apply_tags_to_note(path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Read the note, compute the union of existing + derived tags, rewrite
    only if the set changed. Returns a small status dict for the runner.
    """
    rel = str(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return {"status": "read_error", "path": rel, "error": str(e)}

    fm, body = _split_frontmatter(text)
    if fm is None:
        return {"status": "no_frontmatter", "path": rel}

    derived = derive_source_tags(fm)
    if not derived:
        # Nothing we can attach — leave the note alone.
        return {"status": "no_derived_tags", "path": rel}

    merged = merge_tags(fm.get("tags"), derived)

    # Idempotency check — only normalize the comparable shape.
    existing_normalized = merge_tags(fm.get("tags"), [])
    if merged == existing_normalized:
        return {"status": "skipped", "path": rel, "tags": merged}

    new_fm = dict(fm)
    new_fm["tags"] = merged

    if dry_run:
        return {
            "status": "would_update",
            "path": rel,
            "added": sorted(set(merged) - set(existing_normalized)),
            "tags": merged,
        }

    try:
        _write_note_atomic(path, new_fm, body)
    except OSError as e:
        return {"status": "write_error", "path": rel, "error": str(e)}

    return {
        "status": "updated",
        "path": rel,
        "added": sorted(set(merged) - set(existing_normalized)),
        "tags": merged,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _run(limit: Optional[int], dry_run: bool) -> dict[str, int]:
    vault_root = _resolve_vault_root()
    all_paths = list(_iter_vault_notes(vault_root))
    if limit is not None and limit > 0:
        all_paths = all_paths[:limit]

    if not all_paths:
        log.info("No notes found under %s", vault_root)
        return {"updated": 0, "skipped": 0, "no_fm": 0, "no_tags": 0, "error": 0}

    log.info(
        "Tag-backfill sweep: %d note(s) under %s (dry_run=%s)",
        len(all_paths),
        vault_root,
        dry_run,
    )

    counts = {"updated": 0, "skipped": 0, "no_fm": 0, "no_tags": 0, "error": 0}
    progress = tqdm(
        total=len(all_paths), desc="tags", unit="note", disable=not sys.stderr.isatty()
    )

    for path in all_paths:
        res = apply_tags_to_note(path, dry_run=dry_run)
        status = res.get("status")
        if status in ("updated", "would_update"):
            counts["updated"] += 1
            if dry_run:
                log.info("would update %s → +%s", res["path"], res["added"])
        elif status == "skipped":
            counts["skipped"] += 1
        elif status == "no_frontmatter":
            counts["no_fm"] += 1
        elif status == "no_derived_tags":
            counts["no_tags"] += 1
        else:
            counts["error"] += 1
            log.warning("%s: %s", res.get("path"), res)
        progress.update(1)
        progress.set_postfix(
            upd=counts["updated"],
            skip=counts["skipped"],
            err=counts["error"],
        )

    progress.close()
    return counts


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="domain_tag_backfill",
        description=(
            "Add #source/<domain>, #source/<handler>, and #ingest/<source> tags "
            "to every vault note. Idempotent. No LLM calls — runs in seconds."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N notes (for safe small-batch testing).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would change but write nothing.",
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

    counts = _run(args.limit, args.dry_run)
    log.info(
        "Done. updated=%d skipped=%d no_frontmatter=%d no_derived_tags=%d errors=%d",
        counts["updated"],
        counts["skipped"],
        counts["no_fm"],
        counts["no_tags"],
        counts["error"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
