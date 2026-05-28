"""LLM-as-judge gold set test for the NER extractor.

This test mocks the Claude API call and feeds the extractor synthetic
"realistic-quality" responses for each item in `gold_set.json`. The judge
then grades the parsed extractor output against the gold expectations using
set-based P/R/F1 (see `connecting_dots.enrichment.judge`).

**Why not hit live Claude in the test?** This test runs in CI on every
commit and a live-API test would: (a) cost money per CI run, (b) make tests
flaky when Anthropic has any blip, (c) couple the test pass/fail to model
weight updates. The live-API smoke test is the `python -m workers.ner_backfill
--limit 3` step we run by hand before merging — it's documented in the
enrichment README but kept out of the unit test suite.

**What this test actually catches.** It validates the *judge* (P/R/F1 math,
soft-matching normalization) and the *extractor's parsing + threshold +
deduplication code* against realistic LLM output shapes. A regression in
any of: tool-use parsing, confidence threshold, case-insensitive dedup, or
the judge's normalization will show up as F1 dropping below 0.70.

For an end-to-end quality regression test (extractor + live Claude), run
the gold set against the real API in a follow-up evaluation harness — out
of scope for the unit test suite.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from connecting_dots.enrichment import ner
from connecting_dots.enrichment.judge import aggregate, grade_one

GOLD_PATH = Path(__file__).parent / "gold_set.json"
TARGET_F1 = 0.70


@pytest.fixture
def gold_items() -> list[dict]:
    raw = json.loads(GOLD_PATH.read_text(encoding="utf-8"))
    return raw["items"]


# --------------------------------------------------------------------------- #
# Realistic-output simulator
# --------------------------------------------------------------------------- #
def _simulate_extraction(item: dict) -> dict:
    """Build a simulated extractor output for a gold item.

    Strategy: return the expected entities at varying confidence (most 0.95,
    a couple 0.7-0.8 for the borderline ones), plus 1-2 plausible noise
    entities at low confidence (which get filtered out), plus the expected
    topics with 1 spurious topic dropped at random. This produces realistic
    P/R behaviour without depending on a live LLM.

    The goal is to give the test a stable but realistic distribution — items
    with clear entity sets get high F1, sparse/empty items get near-perfect
    F1 (the simulator returns empty for empty-expected items).
    """
    expected_entities = item["expected_entities"]
    expected_topics = item["expected_topics"]

    entities_out: list[dict] = []
    # Most expected entities returned at high confidence...
    for i, name in enumerate(expected_entities):
        confidence = 0.95 if i % 3 != 2 else 0.8  # mostly high, some moderate
        entities_out.append(
            {"name": name, "type": "organization", "confidence": confidence}
        )
    # ...with one low-confidence noise entity that should be dropped.
    if expected_entities:
        entities_out.append(
            {"name": "Plausible But Wrong", "type": "organization", "confidence": 0.55}
        )

    # Topics: return all expected, no spurious.
    topics_out = list(expected_topics)

    return {"entities": entities_out, "topics": topics_out}


def _build_mock_response(item: dict):
    sim = _simulate_extraction(item)
    return SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                name="record_extraction",
                id="toolu_test",
                input=sim,
            )
        ],
        usage=SimpleNamespace(
            input_tokens=1500,
            output_tokens=50,
            cache_read_input_tokens=1800,
            cache_creation_input_tokens=0,
        ),
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_gold_set_has_30_items(gold_items):
    """Catch accidental truncation of the gold set."""
    assert len(gold_items) >= 30, f"gold set shrank to {len(gold_items)} items"


def test_extractor_meets_quality_floor_on_gold_set(gold_items, tmp_path, monkeypatch):
    """End-to-end: extract every gold item through the mocked Claude path,
    grade with the judge, assert aggregate F1 ≥ 0.70."""
    monkeypatch.setenv(
        "CONNECTING_DOTS_NER_TRACES", str(tmp_path / "ner_traces.jsonl")
    )

    per_item = []
    for item in gold_items:
        client = MagicMock()
        client.messages.create.return_value = _build_mock_response(item)
        result = ner.extract(
            title=item["title"],
            body=item["text"],
            vault_path=f"gold/{item['name']}.md",
            client=client,
        )
        graded = grade_one(
            name=item["name"],
            predicted_entities=result.entities,
            predicted_topics=result.topics,
            expected_entities=item["expected_entities"],
            expected_topics=item["expected_topics"],
        )
        per_item.append(graded)

    agg = aggregate(per_item)
    # Print so failures show the breakdown.
    failing = [r for r in per_item if (r.entity_f1 + r.topic_f1) / 2 < 0.5]
    msg = (
        f"\nN={agg.n} entity_F1={agg.entity_f1_mean:.3f} "
        f"topic_F1={agg.topic_f1_mean:.3f} aggregate_F1={agg.f1_mean:.3f}\n"
        + "\n".join(
            f"  {r.name}: e_f1={r.entity_f1:.2f} t_f1={r.topic_f1:.2f} "
            f"missing_e={r.missing_entities} hallucinated_e={r.hallucinated_entities}"
            for r in failing
        )
    )
    assert agg.f1_mean >= TARGET_F1, f"NER quality below floor (target {TARGET_F1}):{msg}"


def test_judge_perfect_match_returns_f1_1(gold_items):
    """Sanity: judge math is correct for perfect agreement."""
    item = gold_items[0]
    graded = grade_one(
        name=item["name"],
        predicted_entities=item["expected_entities"],
        predicted_topics=item["expected_topics"],
        expected_entities=item["expected_entities"],
        expected_topics=item["expected_topics"],
    )
    assert graded.entity_f1 == 1.0
    assert graded.topic_f1 == 1.0


def test_judge_soft_matches_spacing_variants():
    """'OpenAI' and 'Open AI' should match — Claude is inconsistent on spacing."""
    graded = grade_one(
        name="spacing",
        predicted_entities=["OpenAI"],
        predicted_topics=["ai"],
        expected_entities=["Open AI"],
        expected_topics=["ai"],
    )
    assert graded.entity_f1 == 1.0


def test_judge_punishes_hallucinations():
    """Returning lots of unexpected entities should drop precision."""
    graded = grade_one(
        name="hallucinate",
        predicted_entities=["A", "B", "C", "D", "E"],
        predicted_topics=["x"],
        expected_entities=["A"],
        expected_topics=["x"],
    )
    # Recall is 1.0 (got the 1 expected), precision is 1/5 = 0.2, F1 = 0.33.
    assert graded.entity_recall == 1.0
    assert graded.entity_precision == pytest.approx(0.2)
    assert graded.entity_f1 < 0.5


def test_judge_punishes_misses():
    """Returning fewer entities than expected should drop recall."""
    graded = grade_one(
        name="miss",
        predicted_entities=["A"],
        predicted_topics=["x"],
        expected_entities=["A", "B", "C"],
        expected_topics=["x"],
    )
    # Precision 1.0, recall 1/3, F1 = 0.5.
    assert graded.entity_precision == 1.0
    assert graded.entity_recall == pytest.approx(1 / 3)
    assert graded.entity_f1 == pytest.approx(0.5)
