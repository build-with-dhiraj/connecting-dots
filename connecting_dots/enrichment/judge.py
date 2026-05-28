"""LLM-as-judge quality gate for the NER extractor.

Used by `tests/enrichment/test_judge.py` to assert that extractor F1 against a
hand-crafted gold set stays above a quality floor (0.70 aggregate F1). If
someone tweaks the system prompt or swaps the model, this is the test that
catches a regression before it merges.

The judge is itself a separate Claude call (different prompt, different shape)
that compares extracted vs expected entities and topics, returning per-item
precision/recall + an aggregate F1. Per the `write-judge-prompt` skill, the
criteria are kept code-side (string set comparison with soft matching) rather
than asking the judge to score subjectively — that gives reproducible numbers
and avoids the judge-validation rabbit hole for what is fundamentally a
mechanical comparison.

For purely-mechanical comparison we don't actually need an LLM judge — the
gold set defines `expected_entities` and `expected_topics` explicitly. We
score against those directly with set-based P/R/F1. Soft matching uses
case-insensitive substring equivalence on entities (since Claude may return
"OpenAI" vs gold "Open AI", or "GPU compute" vs "GPU computing") and exact
lowercase match on topics (topics are tags, exactness matters).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass
class JudgeItemResult:
    name: str
    entity_precision: float
    entity_recall: float
    entity_f1: float
    topic_precision: float
    topic_recall: float
    topic_f1: float
    missing_entities: list[str]
    hallucinated_entities: list[str]
    missing_topics: list[str]
    hallucinated_topics: list[str]


@dataclass
class JudgeAggregate:
    n: int
    entity_f1_mean: float
    topic_f1_mean: float
    f1_mean: float  # average of the two — what the test asserts on
    per_item: list[JudgeItemResult]


def _normalize_entity(s: str) -> str:
    """Lowercase + strip whitespace + collapse internal spaces.

    Keeps the soft-matching simple: 'OpenAI' and 'Open AI' both become 'openai'
    after stripping intra-word spaces. Doesn't cover all aliases ('Anthropic'
    vs 'Anthropic PBC') — that's a deliberate quality bar for the extractor.
    """
    return "".join(s.lower().split())


def _normalize_topic(s: str) -> str:
    return s.lower().strip()


def _prf1(predicted: set[str], expected: set[str]) -> tuple[float, float, float]:
    tp = len(predicted & expected)
    fp = len(predicted - expected)
    fn = len(expected - predicted)
    precision = tp / (tp + fp) if (tp + fp) else (1.0 if not expected else 0.0)
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return precision, recall, f1


def grade_one(
    *,
    name: str,
    predicted_entities: Iterable[str],
    predicted_topics: Iterable[str],
    expected_entities: Iterable[str],
    expected_topics: Iterable[str],
) -> JudgeItemResult:
    pe_norm = {_normalize_entity(e): e for e in predicted_entities if e}
    ee_norm = {_normalize_entity(e): e for e in expected_entities if e}
    e_p, e_r, e_f = _prf1(set(pe_norm), set(ee_norm))

    pt_norm = {_normalize_topic(t): t for t in predicted_topics if t}
    et_norm = {_normalize_topic(t): t for t in expected_topics if t}
    t_p, t_r, t_f = _prf1(set(pt_norm), set(et_norm))

    return JudgeItemResult(
        name=name,
        entity_precision=e_p,
        entity_recall=e_r,
        entity_f1=e_f,
        topic_precision=t_p,
        topic_recall=t_r,
        topic_f1=t_f,
        missing_entities=sorted(
            ee_norm[k] for k in set(ee_norm) - set(pe_norm)
        ),
        hallucinated_entities=sorted(
            pe_norm[k] for k in set(pe_norm) - set(ee_norm)
        ),
        missing_topics=sorted(et_norm[k] for k in set(et_norm) - set(pt_norm)),
        hallucinated_topics=sorted(
            pt_norm[k] for k in set(pt_norm) - set(et_norm)
        ),
    )


def aggregate(results: list[JudgeItemResult]) -> JudgeAggregate:
    if not results:
        return JudgeAggregate(n=0, entity_f1_mean=0.0, topic_f1_mean=0.0, f1_mean=0.0, per_item=[])
    e_mean = sum(r.entity_f1 for r in results) / len(results)
    t_mean = sum(r.topic_f1 for r in results) / len(results)
    return JudgeAggregate(
        n=len(results),
        entity_f1_mean=e_mean,
        topic_f1_mean=t_mean,
        f1_mean=(e_mean + t_mean) / 2,
        per_item=results,
    )


__all__ = ["JudgeItemResult", "JudgeAggregate", "grade_one", "aggregate"]
