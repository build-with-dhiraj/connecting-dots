"""Tests for `connecting_dots.enrichment.tracer`.

Covers:
- One JSON object per line (no torn records under concurrent appenders).
- Each line is valid JSON.
- Cost math against the published pricing constants.
- Env override redirects writes.
"""
from __future__ import annotations

import json
import threading

import pytest

from connecting_dots.enrichment.tracer import (
    Trace,
    append_trace,
    compute_cost_usd,
)


@pytest.fixture
def traces_path(tmp_path, monkeypatch):
    p = tmp_path / "nested" / "ner_traces.jsonl"
    monkeypatch.setenv("CONNECTING_DOTS_NER_TRACES", str(p))
    return p


# --------------------------------------------------------------------------- #
# Append + JSON validity
# --------------------------------------------------------------------------- #
def test_append_creates_parent_dir_and_file(traces_path):
    assert not traces_path.exists()
    append_trace(
        Trace(
            vault_path="sources/web/x.md",
            model="gpt-4.1",
            input_tokens=100,
            output_tokens=20,
            cached_input_tokens=0,
            cost_usd=0.00012,
            entities_count=2,
            topics_count=1,
            duration_ms=412.0,
        )
    )
    assert traces_path.exists()
    lines = traces_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["vault_path"] == "sources/web/x.md"
    assert parsed["entities_count"] == 2
    assert parsed["error"] is None


def test_append_multiple_records_one_per_line(traces_path):
    for i in range(5):
        append_trace(
            Trace(
                vault_path=f"x{i}.md",
                model="gpt-4.1",
                input_tokens=10 * i,
                output_tokens=2,
                cached_input_tokens=0,
                cost_usd=0.0,
                entities_count=i,
                topics_count=0,
                duration_ms=1.0,
            )
        )
    lines = traces_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    for i, line in enumerate(lines):
        parsed = json.loads(line)
        assert parsed["vault_path"] == f"x{i}.md"


def test_concurrent_appends_produce_intact_lines(traces_path):
    """Hammer it from threads. Every line must still be parseable JSON."""

    def worker(idx: int) -> None:
        for j in range(10):
            append_trace(
                Trace(
                    vault_path=f"t{idx}-{j}.md",
                    model="gpt-4.1",
                    input_tokens=j,
                    output_tokens=1,
                    cached_input_tokens=0,
                    cost_usd=0.0,
                    entities_count=0,
                    topics_count=0,
                    duration_ms=0.1,
                )
            )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = traces_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 80
    # Every line must be valid JSON.
    for line in lines:
        json.loads(line)


# --------------------------------------------------------------------------- #
# Cost math
# --------------------------------------------------------------------------- #
def test_cost_gpt_4_1_full_input_output():
    # 1M input tokens at $2.00 = $2.00. 1M output at $8.00 = $8.00. Total $10.00.
    cost = compute_cost_usd(
        model="gpt-4.1",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert cost == pytest.approx(10.0)


def test_cost_gpt_4_1_with_cache_hit():
    # gpt-4.1 on Azure: input $2.00/1M, cached $1.00/1M, output $8.00/1M.
    # 100 fresh input + 900 cached + 50 output:
    # input  : 100  * 2.00/1M  = $0.0002
    # cached : 900  * 1.00/1M  = $0.0009
    # output : 50   * 8.00/1M  = $0.0004
    # total                    = $0.0015
    cost = compute_cost_usd(
        model="gpt-4.1",
        input_tokens=100,
        output_tokens=50,
        cached_input_tokens=900,
    )
    assert cost == pytest.approx(0.0015, abs=1e-6)


def test_cost_unknown_model_returns_zero():
    cost = compute_cost_usd(
        model="some-unknown-model-99-0",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert cost == 0.0


def test_cost_cache_read_is_half_of_input():
    """Azure's automatic prompt-prefix cache reads are billed at 50% of the
    input rate. 1M cached reads at gpt-4.1 = $1.00."""
    cost = compute_cost_usd(
        model="gpt-4.1",
        input_tokens=0,
        output_tokens=0,
        cached_input_tokens=1_000_000,
    )
    assert cost == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Timestamp default
# --------------------------------------------------------------------------- #
def test_trace_auto_populates_timestamp():
    t = Trace(
        vault_path="x.md",
        model="gpt-4.1",
        input_tokens=1,
        output_tokens=1,
        cached_input_tokens=0,
        cost_usd=0.0,
        entities_count=0,
        topics_count=0,
        duration_ms=1.0,
    )
    # ISO 8601 with Z suffix, UTC.
    assert t.timestamp.endswith("Z")
    assert "T" in t.timestamp
