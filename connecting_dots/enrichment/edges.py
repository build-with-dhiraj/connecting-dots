"""Pure-function module for entity-overlap edge computation between vault notes.

No LLM calls. No external deps beyond stdlib. All math is set operations.
"""
from __future__ import annotations

from typing import NamedTuple


class ParsedNote(NamedTuple):
    """Minimal representation of a vault note for edge computation."""

    slug: str  # filename without .md
    path: str  # absolute path string
    title: str  # from frontmatter `title` field (or slug if missing)
    entities: list[str]  # from frontmatter `entities` array


class Related(NamedTuple):
    """A related note with its similarity score and shared entities."""

    slug: str
    title: str
    score: float  # Jaccard similarity
    shared: list[str]  # shared entity names, sorted


def jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity between two sets. Returns 0.0 if both empty."""
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def build_entity_index(notes: list[ParsedNote]) -> dict[str, set[str]]:
    """Build entity → set[slug] index from all notes.

    Each key is a lowercase entity string. Each value is the set of note slugs
    that have that entity in their entities array.
    """
    index: dict[str, set[str]] = {}
    for note in notes:
        for entity in note.entities:
            key = entity.lower()
            if key not in index:
                index[key] = set()
            index[key].add(note.slug)
    return index


def find_related(
    note: ParsedNote,
    index: dict[str, set[str]],
    all_notes: dict[str, ParsedNote],
    threshold: float = 0.15,
    top_k: int = 7,
) -> list[Related]:
    """Find notes related to `note` via entity Jaccard similarity.

    Args:
        note: The source note.
        index: entity → set[slug] co-occurrence index.
        all_notes: slug → ParsedNote lookup dict.
        threshold: Minimum Jaccard score to include.
        top_k: Maximum number of related notes to return.

    Returns:
        List of Related, sorted by score descending, length ≤ top_k.
    """
    if not note.entities:
        return []

    a_entities = {e.lower() for e in note.entities}

    # Collect candidate slugs from the union of index entries for each entity
    candidate_slugs: set[str] = set()
    for entity in a_entities:
        candidate_slugs.update(index.get(entity, set()))

    # Remove self
    candidate_slugs.discard(note.slug)

    results: list[Related] = []
    for slug in candidate_slugs:
        candidate = all_notes.get(slug)
        if candidate is None:
            continue
        b_entities = {e.lower() for e in candidate.entities}
        score = jaccard(a_entities, b_entities)
        if score < threshold:
            continue
        shared = sorted(
            (e for e in note.entities if e.lower() in b_entities),
            key=str.lower,
        )
        results.append(Related(slug=slug, title=candidate.title, score=score, shared=shared))

    results.sort(key=lambda r: (-r.score, r.slug))
    return results[:top_k]
