"""MoC (Map of Content) generator.

Walk the vault, collect all #topic/* tags, pick the top-N topics by note count,
and write one curated MoC markdown file per topic to vault/themes/.

Usage
-----
    python -m workers.moc_generator [--top-n 30] [--min-notes 5] [--dry-run] [--force]

Idempotency
-----------
If vault/themes/<slug>.md exists and its frontmatter `generated_at` is within
the last 7 days, the topic is skipped unless --force is passed.
"""
from __future__ import annotations

import argparse
import logging
import os
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from connecting_dots.enrichment.moc import MoCResult, _topic_slug, synthesize
from lib.vault_writer.writer import _resolve_vault_root

log = logging.getLogger("moc_generator")

FRESHNESS_DAYS = 7
DEFAULT_TOP_N = 30
DEFAULT_MIN_NOTES = 5
DEFAULT_MAX_NOTES_PER_TOPIC = 20

_SKIP_DIR_PREFIXES = ("inbox/_failed/", "_failed/", ".trash/")
_SKIP_RELATIVE_PATHS = {"inbox/example.md"}

# --------------------------------------------------------------------------- #
# Vault walking
# --------------------------------------------------------------------------- #
def _iter_vault_notes(vault_root: Path):
    """Yield (path, frontmatter_dict) for every note with a YAML block."""
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
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            fm = _parse_frontmatter(text)
            if fm is None:
                continue
            yield path, fm, text


def _parse_frontmatter(text: str) -> Optional[dict[str, Any]]:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    try:
        fm = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError:
        return None
    return fm if isinstance(fm, dict) else None


def _parse_body(text: str) -> str:
    """Return body text (after the closing --- delimiter)."""
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    if end == -1:
        return text
    return text[end + 5:]


def _extract_topic_tags(fm: dict[str, Any]) -> list[str]:
    """Return all #topic/* tags from frontmatter `tags`."""
    tags = fm.get("tags") or []
    if isinstance(tags, str):
        tags = tags.split()
    topics = []
    for t in tags:
        if isinstance(t, str) and t.startswith("#topic/"):
            topics.append(t)
    return topics


# --------------------------------------------------------------------------- #
# Freshness check
# --------------------------------------------------------------------------- #
def _is_fresh(moc_path: Path, freshness_days: int) -> bool:
    """Return True if moc_path exists and was generated within freshness_days."""
    if not moc_path.exists():
        return False
    try:
        text = moc_path.read_text(encoding="utf-8")
    except OSError:
        return False
    fm = _parse_frontmatter(text)
    if not fm:
        return False
    gen_at = fm.get("generated_at")
    if not gen_at:
        return False
    try:
        dt = datetime.fromisoformat(str(gen_at).replace("Z", "+00:00"))
        cutoff = datetime.now(timezone.utc) - timedelta(days=freshness_days)
        return dt >= cutoff
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# MoC file builder
# --------------------------------------------------------------------------- #
def _build_moc_content(
    *,
    topic_tag: str,
    topic_name: str,
    result: MoCResult,
    all_note_titles: list[str],
    note_count: int,
    model: str,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    fm = {
        "type": "moc",
        "topic": topic_name,
        "generated_at": now,
        "note_count": note_count,
        "model": model,
    }
    fm_str = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, default_flow_style=False).rstrip()

    # Essential notes section
    essential_lines = []
    for e in result.essential_notes:
        title = e.get("title", "").strip()
        reason = e.get("reason", "").strip()
        if title:
            if reason:
                essential_lines.append(f"- [[{title}]] — {reason}")
            else:
                essential_lines.append(f"- [[{title}]]")

    # All notes section
    all_lines = [f"- [[{t}]]" for t in all_note_titles if t]

    essential_section = "\n".join(essential_lines) if essential_lines else "_No essential notes identified._"
    all_section = "\n".join(all_lines) if all_lines else "_No notes found._"

    synthesis = result.synthesis or "_Synthesis unavailable._"

    body = f"""# {topic_name.title()}

{synthesis}

## Essential reading

{essential_section}

## All notes ({note_count})

{all_section}
"""

    return f"---\n{fm_str}\n---\n\n{body}"


# --------------------------------------------------------------------------- #
# Atomic write
# --------------------------------------------------------------------------- #
def _write_moc_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-moc-", suffix=".md", dir=str(path.parent))
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
# Main logic
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(
    *,
    top_n: int = DEFAULT_TOP_N,
    min_notes: int = DEFAULT_MIN_NOTES,
    dry_run: bool = False,
    force: bool = False,
    model: Optional[str] = None,
) -> dict[str, int]:
    vault_root = _resolve_vault_root()
    themes_dir = vault_root / "themes"

    chosen_model = (
        model
        or os.environ.get("MOC_MODEL")
        or os.environ.get("AZURE_OPENAI_DEPLOYMENT")
        or "gpt-4.1"
    )

    log.info("Walking vault at %s...", vault_root)

    # Collect topic → list of (path, title, body_preview, captured_at)
    topic_notes: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for path, fm, text in _iter_vault_notes(vault_root):
        topic_tags = _extract_topic_tags(fm)
        if not topic_tags:
            continue
        title = str(fm.get("title") or path.stem)
        body = _parse_body(text).strip()
        preview = body[:200]
        captured_at = str(fm.get("captured_at") or "")
        for tag in topic_tags:
            topic_notes[tag].append(
                {"title": title, "body_preview": preview, "captured_at": captured_at}
            )

    if not topic_notes:
        log.info("No #topic/* tags found in vault.")
        return {"processed": 0, "skipped": 0, "errors": 0}

    # Pick top-N topics by note count
    topic_counts = Counter({tag: len(notes) for tag, notes in topic_notes.items()})
    selected = [
        (tag, count)
        for tag, count in topic_counts.most_common(top_n)
        if count >= min_notes
    ]

    log.info(
        "Found %d topics with >= %d notes. Processing top %d.",
        len([c for c in topic_counts.values() if c >= min_notes]),
        min_notes,
        len(selected),
    )

    counts = {"processed": 0, "skipped": 0, "errors": 0}

    for topic_tag, note_count in selected:
        slug = _topic_slug(topic_tag)
        topic_name = slug.replace("-", " ")
        moc_path = themes_dir / f"{slug}.md"

        # Freshness / idempotency check
        if not force and _is_fresh(moc_path, FRESHNESS_DAYS):
            log.info("  [skip] %s (generated within %d days)", slug, FRESHNESS_DAYS)
            counts["skipped"] += 1
            continue

        # Sort by captured_at desc, take top N
        notes = sorted(
            topic_notes[topic_tag],
            key=lambda n: n.get("captured_at") or "",
            reverse=True,
        )[:DEFAULT_MAX_NOTES_PER_TOPIC]

        all_titles = [n["title"] for n in topic_notes[topic_tag]]

        if dry_run:
            log.info(
                "  [dry-run] Would write %s (%d notes → %d sampled)",
                moc_path,
                note_count,
                len(notes),
            )
            counts["processed"] += 1
            continue

        log.info("  Synthesising %s (%d notes)...", topic_name, note_count)

        result = synthesize(
            topic=topic_name,
            notes=notes,
            model=chosen_model,
        )

        if result.error:
            log.warning("  Synthesis error for %s: %s", topic_name, result.error)
            counts["errors"] += 1
            continue

        content = _build_moc_content(
            topic_tag=topic_tag,
            topic_name=topic_name,
            result=result,
            all_note_titles=all_titles,
            note_count=note_count,
            model=chosen_model,
        )

        try:
            _write_moc_atomic(moc_path, content)
            log.info("  Wrote %s", moc_path.name)
            counts["processed"] += 1
        except OSError as e:
            log.error("  Write error for %s: %s", slug, e)
            counts["errors"] += 1

    return counts


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="moc_generator",
        description="Generate Map of Content pages for top topics in the vault.",
    )
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--min-notes", type=int, default=DEFAULT_MIN_NOTES)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Regenerate even if fresh.")
    parser.add_argument("--model", default=None)
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    counts = run(
        top_n=args.top_n,
        min_notes=args.min_notes,
        dry_run=args.dry_run,
        force=args.force,
        model=args.model,
    )
    log.info(
        "Done. processed=%d skipped=%d errors=%d",
        counts["processed"],
        counts["skipped"],
        counts["errors"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
