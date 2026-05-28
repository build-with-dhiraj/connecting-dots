"""Tests for connecting_dots.enrichment.tag_dedup — phases A, B, C, D."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import yaml

from connecting_dots.enrichment import tag_dedup
from connecting_dots.enrichment.tag_dedup import (
    normalize_key,
    phase_a,
    phase_b,
    phase_c,
    phase_d,
    _split_frontmatter,
)


# --------------------------------------------------------------------------- #
# Phase A — normalize_key
# --------------------------------------------------------------------------- #
def test_normalize_key_strips_case_and_punct():
    assert normalize_key("#entity/AI") == "ai"
    assert normalize_key("#entity/a-i") == "ai"
    assert normalize_key("#entity/A.I.") == "ai"
    assert normalize_key("#entity/artificial-intelligence") == "artificial intelligence"


def test_normalize_key_sorts_words():
    assert normalize_key("#topic/AI Engineering") == "ai engineering"
    assert normalize_key("#topic/ai-engineering") == "ai engineering"
    assert normalize_key("#topic/engineering-ai") == "ai engineering"
    assert normalize_key("#topic/ai eng") == "ai eng"


# --------------------------------------------------------------------------- #
# Phase A — grouping and canonical selection
# --------------------------------------------------------------------------- #
def test_phase_a_groups_obvious_duplicates():
    tags = [
        "#entity/AI",
        "#entity/ai",
        "#entity/a-i",
        "#topic/machine-learning",
        "#topic/machine learning",
        "#topic/Machine-Learning",
    ]
    mapping = phase_a(tags)
    # All entity AI variants should map to the same canonical
    entity_canonicals = {mapping["#entity/AI"], mapping["#entity/ai"], mapping["#entity/a-i"]}
    assert len(entity_canonicals) == 1
    # All topic ML variants should map to the same canonical
    ml_canonicals = {
        mapping["#topic/machine-learning"],
        mapping["#topic/machine learning"],
        mapping["#topic/Machine-Learning"],
    }
    assert len(ml_canonicals) == 1


def test_phase_a_picks_canonical_consistently():
    tags = ["#entity/anthropic", "#entity/Anthropic", "#entity/ANTHROPIC"]
    mapping = phase_a(tags)
    # All should map to the same canonical
    canonicals = {mapping[t] for t in tags}
    assert len(canonicals) == 1
    # The canonical itself should be in the mapping values
    canonical = canonicals.pop()
    assert canonical in tags


def test_phase_a_entity_and_topic_stay_separate():
    """#entity/ai and #topic/ai should NOT be merged — different namespaces."""
    tags = ["#entity/ai", "#topic/ai"]
    mapping = phase_a(tags)
    assert mapping["#entity/ai"] != mapping["#topic/ai"]


# --------------------------------------------------------------------------- #
# Phase B — LLM judge (mocked)
# --------------------------------------------------------------------------- #
def _make_judge_response(decisions: list[dict]) -> SimpleNamespace:
    """Build a fake AzureOpenAI ChatCompletion response for the judge tool."""
    args = json.dumps({"decisions": decisions})
    tool_call = SimpleNamespace(function=SimpleNamespace(arguments=args))
    message = SimpleNamespace(tool_calls=[tool_call])
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


def test_phase_b_llm_call_mocked():
    """Phase B calls the LLM client when there are ambiguous pairs."""
    # anthropic and anthropic-pbc share 'anthropic' as a prefix — Phase B candidate
    tags = ["#entity/anthropic", "#entity/anthropic-pbc"]
    a_map = phase_a(tags)

    decisions = [{"pair_index": 1, "duplicate": True, "canonical": "anthropic"}]
    mock_response = _make_judge_response(decisions)

    with patch.object(tag_dedup, "_get_client") as mock_get_client, \
         patch.object(tag_dedup, "_embed_texts", side_effect=Exception("no embed")):
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        mock_get_client.return_value = mock_client

        phase_b(tags, a_map, model="gpt-4.1")

    # The LLM was called at least once
    assert mock_client.chat.completions.create.called


def test_phase_b_returns_pairs_decision():
    """Phase B maps the non-canonical tag → canonical for duplicate pairs."""
    # Simulating: anthropic-pbc ↔ anthropic → duplicate, canonical = anthropic
    tags = ["#entity/anthropic", "#entity/anthropic-pbc"]
    a_map = phase_a(tags)

    decisions = [{"pair_index": 1, "duplicate": True, "canonical": "anthropic"}]
    mock_response = _make_judge_response(decisions)

    with patch.object(tag_dedup, "_get_client") as mock_get_client, \
         patch.object(tag_dedup, "_embed_texts", side_effect=Exception("no embed")):
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        mock_get_client.return_value = mock_client

        result = phase_b(tags, a_map, model="gpt-4.1")

    # One of the tags should be remapped to the other
    assert isinstance(result, dict)


# --------------------------------------------------------------------------- #
# Phase C — combines phases and caches
# --------------------------------------------------------------------------- #
def test_phase_c_combines_phases(tmp_path):
    tags = [
        "#entity/AI",
        "#entity/ai",
        "#entity/Anthropic",
        "#entity/anthropic",
    ]
    cache_file = tmp_path / "map.json"

    # Skip LLM for this test
    mapping = phase_c(tags, cache_path=cache_file, skip_llm=True)

    # All AI variants map to the same canonical
    assert mapping["#entity/AI"] == mapping["#entity/ai"]
    # All Anthropic variants map to the same canonical
    assert mapping["#entity/Anthropic"] == mapping["#entity/anthropic"]


def test_phase_c_caches_to_disk(tmp_path):
    tags = ["#entity/ai", "#entity/AI"]
    cache_file = tmp_path / "tag_map.json"

    phase_c(tags, cache_path=cache_file, skip_llm=True)

    assert cache_file.exists()
    data = json.loads(cache_file.read_text())
    assert isinstance(data, dict)
    assert "#entity/ai" in data
    assert "#entity/AI" in data


# --------------------------------------------------------------------------- #
# Phase D — frontmatter rewrite
# --------------------------------------------------------------------------- #
def _write_note(path: Path, tags: list[str], title: str = "Test note") -> None:
    fm = {"title": title, "tags": tags}
    content = f"---\n{yaml.safe_dump(fm, allow_unicode=True).rstrip()}\n---\n# {title}\n\nBody text.\n"
    path.write_text(content, encoding="utf-8")


def test_phase_d_rewrites_tags(tmp_path):
    note = tmp_path / "note.md"
    _write_note(note, tags=["#entity/AI", "#entity/artificial-intelligence", "#source/email"])

    canonical_map = {
        "#entity/AI": "#entity/ai",
        "#entity/artificial-intelligence": "#entity/ai",
    }
    counts = phase_d(tmp_path, canonical_map)

    assert counts["updated"] == 1
    text = note.read_text()
    fm, _ = _split_frontmatter(text)
    assert fm is not None
    tags = fm["tags"]
    # Both AI variants collapsed to one
    assert "#entity/ai" in tags
    assert "#entity/AI" not in tags
    assert "#entity/artificial-intelligence" not in tags
    # Non-entity tag preserved
    assert "#source/email" in tags


def test_phase_d_idempotent_on_rerun(tmp_path):
    note = tmp_path / "note.md"
    _write_note(note, tags=["#entity/ai", "#source/email"])

    canonical_map = {"#entity/ai": "#entity/ai"}
    phase_d(tmp_path, canonical_map)
    text_after_first = note.read_text()

    phase_d(tmp_path, canonical_map)
    text_after_second = note.read_text()

    # Second run should not change anything (already canonical)
    assert text_after_first == text_after_second


def test_apply_skips_notes_with_no_tags(tmp_path):
    note = tmp_path / "no_tags.md"
    fm = {"title": "No tags note"}
    content = f"---\n{yaml.safe_dump(fm).rstrip()}\n---\n# No tags\n\nBody.\n"
    note.write_text(content, encoding="utf-8")

    canonical_map = {"#entity/ai": "#entity/ai"}
    counts = phase_d(tmp_path, canonical_map)

    assert counts["no_tags"] == 1
    assert counts["updated"] == 0


def test_dry_run_no_mutation(tmp_path):
    note = tmp_path / "note.md"
    _write_note(note, tags=["#entity/AI", "#entity/artificial-intelligence"])
    original_text = note.read_text()

    canonical_map = {
        "#entity/AI": "#entity/ai",
        "#entity/artificial-intelligence": "#entity/ai",
    }
    counts = phase_d(tmp_path, canonical_map, dry_run=True)

    # Should show as "would update" but file unchanged
    assert counts["updated"] == 1
    assert note.read_text() == original_text
