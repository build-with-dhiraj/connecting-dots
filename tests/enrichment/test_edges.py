"""Tests for connecting_dots.enrichment.edges — pure entity-overlap math."""
from __future__ import annotations

import pytest

from connecting_dots.enrichment.edges import (
    ParsedNote,
    build_entity_index,
    find_related,
    jaccard,
)


# ---------------------------------------------------------------------------
# jaccard
# ---------------------------------------------------------------------------


def test_jaccard_basic():
    a = {"Anthropic", "Claude", "Foundry"}
    b = {"Anthropic", "Claude", "OpenAI"}
    # intersection=2, union=4 → 0.5
    assert jaccard(a, b) == pytest.approx(0.5)


def test_jaccard_empty():
    assert jaccard(set(), set()) == 0.0
    assert jaccard({"a"}, set()) == 0.0
    assert jaccard(set(), {"b"}) == 0.0


# ---------------------------------------------------------------------------
# build_entity_index
# ---------------------------------------------------------------------------


def test_build_entity_index_groups_correctly():
    notes = [
        ParsedNote(slug="note-a", path="/a.md", title="A", entities=["Anthropic", "Claude"]),
        ParsedNote(slug="note-b", path="/b.md", title="B", entities=["Claude", "OpenAI"]),
        ParsedNote(slug="note-c", path="/c.md", title="C", entities=["OpenAI"]),
    ]
    index = build_entity_index(notes)
    assert index["anthropic"] == {"note-a"}
    assert index["claude"] == {"note-a", "note-b"}
    assert index["openai"] == {"note-b", "note-c"}


# ---------------------------------------------------------------------------
# find_related
# ---------------------------------------------------------------------------


def _make_notes(*specs: tuple[str, list[str]]) -> tuple[list[ParsedNote], dict]:
    """Build a list of ParsedNotes and a by-slug dict from (slug, entities) pairs."""
    notes = [
        ParsedNote(slug=slug, path=f"/{slug}.md", title=slug.replace("-", " ").title(), entities=ents)
        for slug, ents in specs
    ]
    by_slug = {n.slug: n for n in notes}
    return notes, by_slug


def test_find_related_respects_threshold():
    notes, by_slug = _make_notes(
        ("note-a", ["Anthropic", "Claude", "Foundry"]),
        ("note-b", ["Anthropic", "Claude", "OpenAI", "X", "Y", "Z"]),  # jaccard=2/7≈0.29
        ("note-c", ["SomeOther"]),  # jaccard=0
    )
    index = build_entity_index(notes)
    result = find_related(notes[0], index, by_slug, threshold=0.15)
    slugs = [r.slug for r in result]
    assert "note-b" in slugs
    assert "note-c" not in slugs


def test_find_related_top_k_truncation():
    # Build a note with 10 neighbours all above threshold
    main_entities = ["E1", "E2", "E3"]
    neighbours = [
        (f"note-{i}", ["E1", "E2", "E3", f"X{i}", f"Y{i}"])
        for i in range(10)
    ]
    notes, by_slug = _make_notes(
        ("main", main_entities),
        *neighbours,
    )
    index = build_entity_index(notes)
    result = find_related(notes[0], index, by_slug, threshold=0.1, top_k=5)
    assert len(result) == 5


def test_find_related_sorted_by_score():
    # note-high shares 3/3+1 with main; note-low shares 1/3+2 with main
    notes, by_slug = _make_notes(
        ("main", ["A", "B", "C"]),
        ("note-high", ["A", "B", "C", "D"]),  # jaccard=3/4=0.75
        ("note-low", ["A", "X", "Y"]),          # jaccard=1/5=0.20
    )
    index = build_entity_index(notes)
    result = find_related(notes[0], index, by_slug, threshold=0.1)
    assert len(result) == 2
    assert result[0].slug == "note-high"
    assert result[1].slug == "note-low"
    assert result[0].score > result[1].score
