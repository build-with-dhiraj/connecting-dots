"""Tests for connecting_dots.digest.labels."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from connecting_dots.digest.labels import (
    LabelRow,
    decode_row_id,
    encode_row_id,
    read_labels,
    write_label,
)


# --------------------------------------------------------------------------- #
# encode / decode
# --------------------------------------------------------------------------- #

def test_encode_decode_roundtrip():
    slug = "sources/web/some-note.md"
    for short in ("up", "shrug", "down"):
        row_id = encode_row_id(slug, short)
        decoded = decode_row_id(row_id)
        assert decoded is not None
        dec_slug, dec_reaction = decoded
        assert dec_slug == slug


def test_decode_invalid_row_id_returns_none():
    assert decode_row_id("no-separator-here") is None
    assert decode_row_id("slug__unknown_code") is None
    assert decode_row_id("__up") is None  # empty slug


# --------------------------------------------------------------------------- #
# write
# --------------------------------------------------------------------------- #

def test_write_label_creates_file(tmp_path):
    db = tmp_path / "labels.jsonl"
    row = write_label("sources/web/note.md", "thumbs_up", "918595087697", labels_db=db)
    assert db.exists()
    assert row.reaction == "thumbs_up"
    lines = db.read_text().strip().splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["item_slug"] == "sources/web/note.md"
    assert obj["reaction"] == "thumbs_up"
    assert obj["user"] == "918595087697"


def test_write_label_appends(tmp_path):
    db = tmp_path / "labels.jsonl"
    write_label("a.md", "thumbs_up", "user1", labels_db=db)
    write_label("b.md", "shrug", "user1", labels_db=db)
    lines = db.read_text().strip().splitlines()
    assert len(lines) == 2


def test_write_label_invalid_reaction(tmp_path):
    db = tmp_path / "labels.jsonl"
    with pytest.raises(ValueError, match="Invalid reaction"):
        write_label("a.md", "love", "user1", labels_db=db)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# read / deduplicate
# --------------------------------------------------------------------------- #

def test_read_labels_deduplicates_by_slug_user(tmp_path):
    """Last reaction for (slug, user) wins."""
    db = tmp_path / "labels.jsonl"
    write_label("a.md", "thumbs_up", "user1", labels_db=db, timestamp="2026-01-01T08:00:00Z")
    write_label("a.md", "thumbs_down", "user1", labels_db=db, timestamp="2026-01-02T08:00:00Z")
    rows = read_labels(labels_db=db)
    assert len(rows) == 1
    assert rows[0].reaction == "thumbs_down"  # last write wins
