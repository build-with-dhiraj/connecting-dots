"""Bundle 3 — Cross-source wikilinks via entity overlap.

Walk every vault note, compute Jaccard similarity over entities arrays,
and write a "Related notes" section into each note's body.

Usage:
    python -m workers.wikilink_builder [--threshold 0.15] [--top-k 7]
                                       [--limit N] [--force] [--dry-run]

Idempotency:
    Notes that already have a "## Related notes" section are skipped unless
    --force is passed. Re-runs are safe and cheap.

Cost: ~$0 (pure Python set math, no LLM calls).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import yaml

try:
    from tqdm import tqdm  # type: ignore[import-untyped]
except ImportError:
    tqdm = None  # type: ignore[assignment]

from connecting_dots.enrichment.edges import (
    ParsedNote,
    Related,
    build_entity_index,
    find_related,
)

VAULT_ROOT = Path(__file__).parent.parent / "vault"
RELATED_SECTION_HEADER = "\n## Related notes\n"


# ---------------------------------------------------------------------------
# Helpers (inlined from vault_writer pattern — no cross-module coupling)
# ---------------------------------------------------------------------------


def _split_frontmatter(text: str) -> tuple[dict | None, str]:
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text
    return yaml.safe_load(text[4:end]) or {}, text[end + 5:]


def _write_note_atomic(path: Path, fm: dict, body: str) -> None:
    """Atomic frontmatter+body write using tmp+rename."""
    new_text = "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n" + body
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(path)


def _iter_vault_notes(vault_root: Path) -> Iterable[Path]:
    """Yield every candidate .md under sources/ and inbox/, in stable order."""
    roots = [vault_root / "sources", vault_root / "inbox"]
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.md")):
            rel = path.relative_to(vault_root).as_posix()
            if rel in {"inbox/example.md"} or any(
                rel.startswith(p)
                for p in ("inbox/_failed/", "_failed/", ".trash/")
            ):
                continue
            yield path


def _note_slug(path: Path) -> str:
    """Return filename without .md extension (Obsidian wikilink target)."""
    return path.stem


def _build_related_section(related: list[Related]) -> str:
    """Render the '## Related notes' markdown section."""
    lines = ["\n## Related notes\n"]
    for r in related:
        shared_str = ", ".join(r.shared)
        count = len(r.shared)
        noun = "entity" if count == 1 else "entities"
        lines.append(f"- [[{r.slug}]] — {count} shared {noun}: {shared_str}")
    return "\n".join(lines) + "\n"


def _strip_related_section(body: str) -> str:
    """Remove an existing '## Related notes' section (and everything after it)."""
    idx = body.find(RELATED_SECTION_HEADER)
    if idx == -1:
        return body
    return body[:idx]


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def load_all_notes(vault_root: Path) -> tuple[list[ParsedNote], dict[str, tuple[Path, dict, str]]]:
    """Load all notes, returning ParsedNotes for indexing and raw data for writing.

    Returns:
        notes: list of ParsedNote (for building the entity index)
        note_data: slug → (path, frontmatter, body) for mutation
    """
    notes: list[ParsedNote] = []
    note_data: dict[str, tuple[Path, dict, str]] = {}

    for path in _iter_vault_notes(vault_root):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue

        fm, body = _split_frontmatter(text)
        if fm is None:
            continue

        slug = _note_slug(path)
        entities: list[str] = fm.get("entities") or []
        title: str = fm.get("title") or slug

        notes.append(ParsedNote(slug=slug, path=str(path), title=title, entities=entities))
        note_data[slug] = (path, fm, body)

    return notes, note_data


def build_wikilinks(
    vault_root: Path,
    threshold: float = 0.15,
    top_k: int = 7,
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> tuple[int, int, int]:
    """Build wikilinks for all vault notes.

    Returns:
        (updated, skipped, no_entities) counts
    """
    print("Loading all vault notes…")
    notes, note_data = load_all_notes(vault_root)
    print(f"  Loaded {len(notes)} notes.")

    print("Building entity index…")
    index = build_entity_index(notes)
    print(f"  Index has {len(index)} unique entities.")

    all_notes_by_slug: dict[str, ParsedNote] = {n.slug: n for n in notes}

    candidates = list(notes)
    if limit is not None:
        candidates = candidates[:limit]

    updated = skipped = no_entities = 0

    iterator: Iterable[ParsedNote] = candidates
    if tqdm is not None:
        iterator = tqdm(candidates, desc="Building wikilinks", unit="note")

    for note in iterator:
        path, fm, body = note_data[note.slug]

        # Skip notes with no entities
        if not note.entities:
            no_entities += 1
            continue

        # Idempotency check
        has_section = RELATED_SECTION_HEADER in body
        if has_section and not force:
            skipped += 1
            continue

        # Compute related notes
        related = find_related(note, index, all_notes_by_slug, threshold=threshold, top_k=top_k)

        # Strip existing section if force
        clean_body = _strip_related_section(body) if has_section else body

        # Build new section
        if related:
            new_body = clean_body.rstrip("\n") + "\n" + _build_related_section(related)
        else:
            new_body = clean_body

        if dry_run:
            print(f"\n[dry-run] {note.slug}")
            if related:
                for r in related:
                    print(f"  → [[{r.slug}]] (score={r.score:.2f}, shared={r.shared})")
            else:
                print("  → (no related notes above threshold)")
            updated += 1
            continue

        # Stamp raw_meta
        raw_meta = fm.get("raw_meta") or {}
        raw_meta["wikilinks_at"] = datetime.now(timezone.utc).isoformat()
        raw_meta["wikilinks_count"] = len(related)
        fm["raw_meta"] = raw_meta

        _write_note_atomic(path, fm, new_body)
        updated += 1

    return updated, skipped, no_entities


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build [[wikilinks]] between related vault notes via entity overlap."
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.15,
        help="Minimum Jaccard similarity (default: 0.15)",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=7,
        help="Maximum related notes per note (default: 7)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N notes (useful for testing)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace existing 'Related notes' sections instead of skipping",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without mutating any files",
    )
    args = parser.parse_args()

    updated, skipped, no_entities = build_wikilinks(
        vault_root=VAULT_ROOT,
        threshold=args.threshold,
        top_k=args.top_k,
        limit=args.limit,
        force=args.force,
        dry_run=args.dry_run,
    )

    mode = "[dry-run] " if args.dry_run else ""
    print(f"\n{mode}Done. updated={updated} skipped={skipped} no_entities={no_entities}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
