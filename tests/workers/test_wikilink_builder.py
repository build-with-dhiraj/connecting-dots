"""Tests for workers.wikilink_builder — vault mutation orchestration."""
from __future__ import annotations

from pathlib import Path

import yaml

from workers.wikilink_builder import build_wikilinks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FM_TEMPLATE = """\
---
source: whatsapp
title: '{title}'
entities:
{entities_yaml}
topics: []
labels: []
tags: []
raw_meta: {{}}
---
"""


def _make_note(vault: Path, subdir: str, slug: str, title: str, entities: list[str], body: str = "") -> Path:
    d = vault / subdir
    d.mkdir(parents=True, exist_ok=True)
    fm: dict = {
        "source": "whatsapp",
        "title": title,
        "entities": entities,
        "topics": [],
        "labels": [],
        "tags": [],
        "raw_meta": {},
    }
    fm_text = "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n"
    note_path = d / f"{slug}.md"
    note_path.write_text(fm_text + body, encoding="utf-8")
    return note_path


def _read_note(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    end = text.find("\n---\n", 4)
    fm = yaml.safe_load(text[4:end]) or {}
    body = text[end + 5:]
    return fm, body


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_writes_related_section(tmp_path):
    vault = tmp_path / "vault"
    note_a = _make_note(vault, "sources", "note-a", "Note A", ["Anthropic", "Claude", "Foundry"])
    _make_note(vault, "sources", "note-b", "Note B", ["Anthropic", "Claude", "OpenAI"])
    _make_note(vault, "sources", "note-c", "Note C", ["OpenAI", "Microsoft"])

    updated, skipped, no_entities = build_wikilinks(vault, threshold=0.1, top_k=7, dry_run=False)

    assert updated >= 2  # note-a and note-b must be updated (they share entities)
    _, body_a = _read_note(note_a)
    assert "## Related notes" in body_a
    assert "[[note-b]]" in body_a


def test_idempotent_skip_when_section_exists(tmp_path):
    vault = tmp_path / "vault"
    existing_body = "\n## Related notes\n\n- [[old-note]] — 1 shared entity: Anthropic\n"
    note_a = _make_note(vault, "sources", "note-a", "Note A", ["Anthropic", "Claude"], body=existing_body)
    _make_note(vault, "sources", "note-b", "Note B", ["Anthropic", "Claude", "OpenAI"])

    updated, skipped, no_entities = build_wikilinks(vault, threshold=0.1, top_k=7, dry_run=False)

    assert skipped >= 1
    # Original section must be untouched
    _, body_a = _read_note(note_a)
    assert "[[old-note]]" in body_a


def test_force_replaces_existing_section(tmp_path):
    vault = tmp_path / "vault"
    existing_body = "\n## Related notes\n\n- [[old-note]] — 1 shared entity: Anthropic\n"
    note_a = _make_note(vault, "sources", "note-a", "Note A", ["Anthropic", "Claude"], body=existing_body)
    _make_note(vault, "sources", "note-b", "Note B", ["Anthropic", "Claude", "OpenAI"])

    build_wikilinks(vault, threshold=0.1, top_k=7, force=True, dry_run=False)

    _, body_a = _read_note(note_a)
    # Old stale link should be gone, new computed link present
    assert "[[old-note]]" not in body_a
    assert "[[note-b]]" in body_a


def test_dry_run_no_mutation(tmp_path):
    vault = tmp_path / "vault"
    note_a = _make_note(vault, "sources", "note-a", "Note A", ["Anthropic", "Claude"])
    _make_note(vault, "sources", "note-b", "Note B", ["Anthropic", "Claude"])

    original_text = note_a.read_text(encoding="utf-8")
    build_wikilinks(vault, threshold=0.1, top_k=7, dry_run=True)

    assert note_a.read_text(encoding="utf-8") == original_text


def test_skips_notes_with_no_entities(tmp_path):
    vault = tmp_path / "vault"
    note_empty = _make_note(vault, "sources", "note-empty", "Empty", [])

    updated, skipped, no_entities = build_wikilinks(vault, threshold=0.1, top_k=7)

    assert no_entities >= 1
    _, body = _read_note(note_empty)
    assert "## Related notes" not in body


def test_no_self_link(tmp_path):
    vault = tmp_path / "vault"
    note_a = _make_note(vault, "sources", "note-a", "Note A", ["Anthropic", "Claude", "Foundry"])
    _make_note(vault, "sources", "note-b", "Note B", ["Anthropic", "Claude"])

    build_wikilinks(vault, threshold=0.1, top_k=7)

    _, body_a = _read_note(note_a)
    # note-a must not link to itself
    lines = body_a.splitlines()
    wikilinks_in_related = [ln for ln in lines if ln.strip().startswith("- [[")]
    for ln in wikilinks_in_related:
        assert "[[note-a]]" not in ln
