"""Tests for tag_dedup v2 — embedding candidates + entity canonicalization."""
from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import yaml

from connecting_dots.enrichment import tag_dedup
from connecting_dots.enrichment.tag_dedup import (
    _cosine,
    _embedding_candidates,
    _lexical_fallback_candidates,
    _is_acronym,
    _map_hash,
    phase_a,
    phase_b,
    phase_d,
    _split_frontmatter,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _unit_vec(dim: int, idx: int) -> list[float]:
    """One-hot unit vector of length dim."""
    v = [0.0] * dim
    v[idx] = 1.0
    return v


def _close_vec(base: list[float], noise: float = 0.01) -> list[float]:
    """Return a vector very close to base (cosine > 0.99)."""
    result = [x + noise for x in base]
    norm = math.sqrt(sum(x * x for x in result))
    return [x / norm for x in result]


def _write_note(path: Path, tags: list[str], entities: list[str] | None = None, title: str = "Test") -> None:
    fm: dict = {"title": title, "tags": tags}
    if entities:
        fm["entities"] = entities
    content = f"---\n{yaml.safe_dump(fm, allow_unicode=True).rstrip()}\n---\n# {title}\n\nBody.\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# --------------------------------------------------------------------------- #
# _cosine
# --------------------------------------------------------------------------- #
def test_cosine_identical():
    v = [1.0, 0.0, 0.0]
    assert abs(_cosine(v, v) - 1.0) < 1e-9


def test_cosine_orthogonal():
    assert abs(_cosine([1.0, 0.0], [0.0, 1.0])) < 1e-9


# --------------------------------------------------------------------------- #
# Embedding candidates — mock embeddings → known vectors
# --------------------------------------------------------------------------- #
def test_embedding_candidates_pairs_semantic_near(tmp_path):
    """Tags with near-identical vectors should become candidate pairs."""
    cache = tmp_path / "emb.json"
    # ai and artificial-intelligence → very close vectors
    # india → orthogonal
    dim = 4
    slug_vecs = {
        "ai": _unit_vec(dim, 0),
        "artificial-intelligence": _close_vec(_unit_vec(dim, 0), noise=0.05),
        "india": _unit_vec(dim, 3),
    }

    tags = ["#entity/ai", "#entity/artificial-intelligence", "#entity/india"]
    a_map = phase_a(tags)

    # Patch _embed_texts to return known vectors
    def fake_embed(texts, deployment):
        return [slug_vecs[t] for t in texts]

    with patch.object(tag_dedup, "_embed_texts", side_effect=fake_embed):
        pairs = _embedding_candidates(a_map, embed_cache_path=cache, threshold=0.85)

    slugs_in_pairs = {(tag_dedup._slug(a), tag_dedup._slug(b)) for a, b in pairs}
    assert ("ai", "artificial-intelligence") in slugs_in_pairs or \
           ("artificial-intelligence", "ai") in slugs_in_pairs
    # india should not be paired with ai
    india_paired = any("india" in (tag_dedup._slug(a), tag_dedup._slug(b)) for a, b in pairs)
    assert not india_paired


def test_embedding_candidates_respects_namespace_boundary(tmp_path):
    """entity/ai and topic/ai must NOT be paired (different namespaces)."""
    cache = tmp_path / "emb.json"
    tags = ["#entity/ai", "#topic/ai"]
    a_map = phase_a(tags)

    slug_vecs = {"ai": [1.0, 0.0]}

    def fake_embed(texts, deployment):
        return [slug_vecs.get(t, [0.0, 0.0]) for t in texts]

    with patch.object(tag_dedup, "_embed_texts", side_effect=fake_embed):
        pairs = _embedding_candidates(a_map, embed_cache_path=cache, threshold=0.85)

    # Both canonicals are named "ai" but in different namespaces — no cross-ns pair
    cross_ns = any(
        tag_dedup._namespace(a) != tag_dedup._namespace(b) for a, b in pairs
    )
    assert not cross_ns


def test_embedding_candidates_threshold(tmp_path):
    """Pairs below threshold should not appear."""
    cache = tmp_path / "emb.json"
    slug_vecs = {
        "alpha": [1.0, 0.0],
        "beta": [0.0, 1.0],  # cosine = 0 < 0.85
    }
    tags = ["#entity/alpha", "#entity/beta"]
    a_map = phase_a(tags)

    def fake_embed(texts, deployment):
        return [slug_vecs[t] for t in texts]

    with patch.object(tag_dedup, "_embed_texts", side_effect=fake_embed):
        pairs = _embedding_candidates(a_map, embed_cache_path=cache, threshold=0.85)

    assert len(pairs) == 0


def test_embeddings_cached_to_disk(tmp_path):
    """Embeddings should be written to cache and reused on second call."""
    cache = tmp_path / "emb.json"
    slug_vecs = {"alpha": [1.0, 0.0], "beta": [0.99, 0.14]}
    tags = ["#entity/alpha", "#entity/beta"]
    a_map = phase_a(tags)

    call_count = {"n": 0}

    def fake_embed(texts, deployment):
        call_count["n"] += 1
        return [slug_vecs[t] for t in texts]

    with patch.object(tag_dedup, "_embed_texts", side_effect=fake_embed):
        _embedding_candidates(a_map, embed_cache_path=cache, threshold=0.5)
        # Second call — should reuse cache
        _embedding_candidates(a_map, embed_cache_path=cache, threshold=0.5)

    assert cache.exists()
    # _embed_texts should only be called once (second call uses cache)
    assert call_count["n"] == 1


def test_build_map_reuses_embedding_cache(tmp_path):
    """phase_c should pass embed_cache_path through and reuse cached vectors."""
    from connecting_dots.enrichment.tag_dedup import phase_c

    cache = tmp_path / "emb.json"
    # Pre-populate cache with known vectors
    dim = 3
    slug_vecs = {"alpha": _unit_vec(dim, 0), "beta": _close_vec(_unit_vec(dim, 0))}
    cache.write_text(json.dumps(slug_vecs), encoding="utf-8")

    tags = ["#entity/alpha", "#entity/beta"]
    # With skip_llm=True we don't call the LLM but the embedding logic runs
    with patch.object(tag_dedup, "_embed_texts") as mock_embed:
        phase_c(
            tags,
            cache_path=tmp_path / "map.json",
            skip_llm=True,
            embed_cache_path=cache,
            no_embeddings=False,
        )
    # embed_texts should NOT be called since both slugs are already cached
    mock_embed.assert_not_called()


# --------------------------------------------------------------------------- #
# Fallback lexical candidates
# --------------------------------------------------------------------------- #
def test_fallback_lexical_when_no_embedding_deployment():
    """When embeddings raise, _embedding_candidates falls back to lexical."""
    tags = ["#entity/machine-learning", "#entity/machine-learning-ops", "#entity/india"]
    a_map = phase_a(tags)

    with patch.object(tag_dedup, "_embed_texts", side_effect=Exception("no deployment")):
        pairs = _embedding_candidates(a_map, no_embeddings=False, threshold=0.85)

    # machine-learning and machine-learning-ops share "machine" and "learning" words
    slugs = {frozenset([tag_dedup._slug(a), tag_dedup._slug(b)]) for a, b in pairs}
    assert frozenset(["machine-learning", "machine-learning-ops"]) in slugs


def test_acronym_matching_in_fallback():
    """_is_acronym and lexical fallback should pair ml ↔ machine-learning."""
    assert _is_acronym("ml", ["machine", "learning"])
    assert not _is_acronym("ai", ["machine", "learning"])

    ns_canonicals = {"entity": ["#entity/ml", "#entity/machine-learning"]}
    pairs = _lexical_fallback_candidates(ns_canonicals)
    slugs = {frozenset([tag_dedup._slug(a), tag_dedup._slug(b)]) for a, b in pairs}
    assert frozenset(["ml", "machine-learning"]) in slugs


# --------------------------------------------------------------------------- #
# Phase B — LLM judge vetoes false merge
# --------------------------------------------------------------------------- #
def _make_judge_response(decisions: list[dict]) -> SimpleNamespace:
    args = json.dumps({"decisions": decisions})
    tool_call = SimpleNamespace(function=SimpleNamespace(arguments=args))
    message = SimpleNamespace(tool_calls=[tool_call])
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


def test_phase_b_judge_vetoes_false_merge():
    """LLM returning duplicate=False means tags stay separate."""
    tags = ["#entity/india", "#entity/indian"]
    a_map = phase_a(tags)

    # Simulate judge saying NOT duplicate
    decisions = [{"pair_index": 1, "duplicate": False, "canonical": "india"}]
    mock_response = _make_judge_response(decisions)

    with patch.object(tag_dedup, "_get_client") as mock_get_client, \
         patch.object(tag_dedup, "_embedding_candidates", return_value=[
             ("#entity/india", "#entity/indian")
         ]):
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        mock_get_client.return_value = mock_client
        result = phase_b(tags, a_map, no_embeddings=True)

    # No merge should occur
    assert "#entity/india" not in result or result.get("#entity/india") == "#entity/india"
    assert "#entity/indian" not in result or result.get("#entity/indian") == "#entity/indian"


# --------------------------------------------------------------------------- #
# Phase D — entities rewrite
# --------------------------------------------------------------------------- #
def test_phase_d_rewrites_entities_array(tmp_path):
    """phase_d should canonicalize entities: arrays using the tag canonical map."""
    note = tmp_path / "note.md"
    _write_note(
        note,
        tags=["#entity/artificial-intelligence"],
        entities=["artificial-intelligence", "Anthropic"],
    )
    canonical_map = {
        "#entity/artificial-intelligence": "#entity/ai",
        "#entity/ai": "#entity/ai",
        "#entity/anthropic": "#entity/anthropic",
    }
    phase_d(tmp_path, canonical_map, rewrite_entities=True)
    fm, _ = _split_frontmatter(note.read_text())
    assert fm is not None
    entities = fm.get("entities", [])
    assert "ai" in entities
    assert "artificial-intelligence" not in entities


def test_phase_d_entity_dedup_preserves_order(tmp_path):
    """Duplicate entities after canonicalization should be deduped, first occurrence kept."""
    note = tmp_path / "note.md"
    _write_note(
        note,
        tags=["#entity/ai"],
        entities=["Artificial Intelligence", "artificial-intelligence", "OpenAI"],
    )
    canonical_map = {
        "#entity/artificial-intelligence": "#entity/ai",
        "#entity/ai": "#entity/ai",
    }
    phase_d(tmp_path, canonical_map, rewrite_entities=True)
    fm, _ = _split_frontmatter(note.read_text())
    entities = fm.get("entities", [])
    # "ai" should appear only once
    assert entities.count("ai") == 1
    # OpenAI should still be present
    assert "OpenAI" in entities


def test_phase_d_idempotent_via_map_hash(tmp_path):
    """Second run with same map should not rewrite already-canonicalized notes."""
    note = tmp_path / "note.md"
    _write_note(note, tags=["#entity/ai"], entities=["artificial-intelligence"])
    canonical_map = {
        "#entity/artificial-intelligence": "#entity/ai",
        "#entity/ai": "#entity/ai",
    }
    phase_d(tmp_path, canonical_map, rewrite_entities=True)
    text_after_first = note.read_text()
    phase_d(tmp_path, canonical_map, rewrite_entities=True)
    assert note.read_text() == text_after_first


def test_apply_skips_already_canonicalized(tmp_path):
    """Notes stamped with the current map hash should be skipped on re-run."""
    canonical_map = {"#entity/artificial-intelligence": "#entity/ai"}
    m_hash = _map_hash(canonical_map)

    note = tmp_path / "note.md"
    fm = {
        "title": "T",
        "tags": ["#entity/ai"],
        "entities": ["ai"],
        "raw_meta": {"entities_canonicalized_at": m_hash},
    }
    content = f"---\n{yaml.safe_dump(fm).rstrip()}\n---\n# T\n\nBody.\n"
    note.write_text(content, encoding="utf-8")

    counts = phase_d(tmp_path, canonical_map, rewrite_entities=True)
    # Nothing changed — skipped or no_tags
    assert counts["updated"] == 0
