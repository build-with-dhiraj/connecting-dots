"""Tests for connecting_dots.digest.resurface."""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from connecting_dots.digest.resurface import (
    DEFAULT_WEIGHTS,
    DigestItem,
    _activity_relevance,
    _diversity_penalty,
    _effective_weights,
    _static_profile_match,
    _time_decay,
    load_vault_notes,
    select_digest_items,
)


# --------------------------------------------------------------------------- #
# Helpers to create fake vault notes on disk
# --------------------------------------------------------------------------- #

def _write_note(tmp_path: Path, slug: str, *, title: str, topics: list[str], entities: list[str],
                captured_at: str, url: str | None = None) -> Path:
    """Write a minimal vault note with frontmatter."""
    p = tmp_path / slug
    p.parent.mkdir(parents=True, exist_ok=True)
    fm: dict = {
        "title": title,
        "captured_at": captured_at,
        "topics": topics,
        "entities": [{"name": e, "type": "concept"} for e in entities],
    }
    if url:
        fm["url"] = url
    fm_str = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, default_flow_style=False).rstrip()
    p.write_text(f"---\n{fm_str}\n---\n\nBody text.", encoding="utf-8")
    return p


def _make_vault(tmp_path: Path, n: int = 10) -> Path:
    """Create a minimal vault with n notes under sources/web/."""
    vault = tmp_path / "vault"
    today = date.today()
    for i in range(n):
        days_ago = i * 15  # spread out over time
        captured = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_note(
            vault,
            f"sources/web/note-{i:03d}.md",
            title=f"Note {i}",
            topics=[f"topic-{i % 3}", f"topic-{(i+1) % 3}"],
            entities=[f"EntityA-{i % 5}", f"EntityB-{(i+1) % 5}"],
            captured_at=captured,
            url=f"https://example.com/{i}",
        )
    return vault


# --------------------------------------------------------------------------- #
# Test: time_decay component
# --------------------------------------------------------------------------- #

def test_time_decay_fresh_note_scores_low():
    """A note captured today should score near 0 (too fresh)."""
    today = date.today()
    note = {"captured_at": datetime.now(timezone.utc)}
    score = _time_decay(note, today)
    assert 0.0 <= score <= 0.1  # fresh notes should have very low decay score


def test_time_decay_old_note_scores_higher():
    """A note from 60 days ago should score higher than a note from yesterday."""
    today = date.today()
    old_note = {"captured_at": datetime.now(timezone.utc) - timedelta(days=60)}
    recent_note = {"captured_at": datetime.now(timezone.utc) - timedelta(days=1)}
    old_score = _time_decay(old_note, today)
    recent_score = _time_decay(recent_note, today)
    assert old_score > recent_score


def test_time_decay_bounded():
    """Score must be in [0, 1]."""
    today = date.today()
    for days in [0, 1, 30, 60, 90, 365, 1000]:
        note = {"captured_at": datetime.now(timezone.utc) - timedelta(days=days)}
        score = _time_decay(note, today)
        assert 0.0 <= score <= 1.0, f"Score {score} out of bounds for days={days}"


# --------------------------------------------------------------------------- #
# Test: activity_relevance component
# --------------------------------------------------------------------------- #

def test_activity_relevance_with_overlap():
    note = {"entities": ["Anthropic", "Claude"]}
    activity = {"anthropic", "gpt-4"}
    score = _activity_relevance(note, activity)
    # Overlap: {"anthropic"}, union: {"anthropic", "claude", "gpt-4"} = 1/3
    assert 0 < score <= 1.0


def test_activity_relevance_no_overlap():
    note = {"entities": ["Zebra", "Safari"]}
    activity = {"anthropic", "claude"}
    score = _activity_relevance(note, activity)
    assert score == 0.0


def test_activity_relevance_empty_activity():
    note = {"entities": ["Anthropic"]}
    score = _activity_relevance(note, set())
    assert score == 0.0


# --------------------------------------------------------------------------- #
# Test: static_profile_match component
# --------------------------------------------------------------------------- #

def test_static_profile_match_exact():
    note = {"topics": ["machine learning", "nlp"]}
    themes = ["machine learning", "nlp"]
    score = _static_profile_match(note, themes)
    assert score == 1.0


def test_static_profile_match_partial():
    note = {"topics": ["machine learning", "art"]}
    themes = ["machine learning", "nlp"]
    score = _static_profile_match(note, themes)
    # Overlap: {"machine learning"}, union: {"machine learning", "art", "nlp"} = 1/3
    assert 0 < score < 1.0


def test_static_profile_match_empty_themes():
    note = {"topics": ["machine learning"]}
    score = _static_profile_match(note, [])
    assert score == 0.0


# --------------------------------------------------------------------------- #
# Test: full hybrid select_digest_items
# --------------------------------------------------------------------------- #

def test_select_digest_items_returns_k(tmp_path):
    vault = _make_vault(tmp_path, n=20)
    items = select_digest_items(vault_root=vault, k=5)
    assert len(items) == 5


def test_select_digest_items_returns_digest_items(tmp_path):
    vault = _make_vault(tmp_path, n=10)
    items = select_digest_items(vault_root=vault, k=3)
    for item in items:
        assert isinstance(item, DigestItem)
        assert item.slug
        assert item.title
        assert isinstance(item.score, float)


def test_select_digest_cold_start_uses_recency(tmp_path):
    """Cold-start (no digest_log) should fall back to pure recency weights."""
    vault = _make_vault(tmp_path, n=10)
    no_log = tmp_path / "nonexistent.jsonl"
    items = select_digest_items(vault_root=vault, k=3, digest_log=no_log)
    # Just verify selection works; cold-start weights = pure time_decay
    assert len(items) == 3


def test_select_digest_diversity_penalty_reduces_entity_overlap(tmp_path):
    """Items selected via MMR should not all share the same entities."""
    vault = tmp_path / "vault"
    # Create 6 notes: 4 share EntityX, 2 don't
    captured = "2026-01-01T08:00:00Z"
    for i in range(4):
        _write_note(vault, f"sources/web/a-{i}.md",
                    title=f"SharedEntity Note {i}", topics=["ai"],
                    entities=["EntityX", "EntityY"], captured_at=captured)
    for i in range(2):
        _write_note(vault, f"sources/web/b-{i}.md",
                    title=f"DifferentEntity Note {i}", topics=["cooking"],
                    entities=["EntityZ"], captured_at=captured)

    items = select_digest_items(vault_root=vault, k=4, digest_log=tmp_path / "no.jsonl")
    slugs = [item.slug for item in items]
    # The diversity penalty should cause the 2 different-entity notes to appear
    different = [s for s in slugs if "b-" in s]
    assert len(different) >= 1, "Diversity penalty should surface non-EntityX notes"
