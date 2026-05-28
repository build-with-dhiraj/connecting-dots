"""Unit tests for `connecting_dots.enrichment.ner`.

All Claude API calls are mocked — no live network. We verify:

- Tool-use response is parsed into entities + topics correctly.
- Confidence threshold drops low-confidence entities BEFORE writeback.
- `cache_control={"type": "ephemeral"}` is set on the last system block —
  this is the prompt-caching contract the README + cost projection depends on.
- The tool definition forces structured output (tool_choice + single tool).
- Errors don't propagate — `extract()` returns an empty result on any failure.
- Tokens + duration get into the trace log.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from connecting_dots.enrichment import ner
from connecting_dots.enrichment.tracer import compute_cost_usd


# --------------------------------------------------------------------------- #
# Helpers — build a fake Anthropic response
# --------------------------------------------------------------------------- #
def _make_response(
    *,
    entities: list[dict] | None = None,
    topics: list[str] | None = None,
    input_tokens: int = 1500,
    output_tokens: int = 50,
    cached_input_tokens: int = 0,
    cache_creation_tokens: int = 0,
    include_tool_use: bool = True,
    tool_name: str = "record_extraction",
):
    """Build a SimpleNamespace mimicking an anthropic.types.Message."""
    content = []
    if include_tool_use:
        tool_block = SimpleNamespace(
            type="tool_use",
            name=tool_name,
            id="toolu_test",
            input={"entities": entities or [], "topics": topics or []},
        )
        content.append(tool_block)
    return SimpleNamespace(
        content=content,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cached_input_tokens,
            cache_creation_input_tokens=cache_creation_tokens,
        ),
    )


@pytest.fixture
def isolated_traces(tmp_path, monkeypatch):
    """Redirect trace output so tests don't pollute the real log."""
    traces = tmp_path / "ner_traces.jsonl"
    monkeypatch.setenv("CONNECTING_DOTS_NER_TRACES", str(traces))
    return traces


@pytest.fixture
def mock_client():
    """A fake anthropic.Anthropic with a recording `messages.create`."""
    client = MagicMock()
    return client


# --------------------------------------------------------------------------- #
# 1. Tool-use parsing
# --------------------------------------------------------------------------- #
def test_extract_parses_tool_use_response(mock_client, isolated_traces):
    mock_client.messages.create.return_value = _make_response(
        entities=[
            {"name": "Anthropic", "type": "organization", "confidence": 0.95},
            {"name": "Claude", "type": "product", "confidence": 0.9},
        ],
        topics=["large language models", "ai tools"],
    )
    result = ner.extract(
        title="Anthropic ships Claude 4.7",
        body="Adaptive thinking only.",
        client=mock_client,
    )
    assert result.entities == ["Anthropic", "Claude"]
    assert result.topics == ["ai tools", "large language models"]
    assert result.raw["entities"][0]["confidence"] == 0.95


# --------------------------------------------------------------------------- #
# 2. Confidence threshold drops low-confidence entities
# --------------------------------------------------------------------------- #
def test_confidence_threshold_drops_low_confidence(mock_client, isolated_traces):
    mock_client.messages.create.return_value = _make_response(
        entities=[
            {"name": "HighConf Co", "type": "organization", "confidence": 0.95},
            {"name": "Marginal Co", "type": "organization", "confidence": 0.75},
            {"name": "Guess Co", "type": "organization", "confidence": 0.5},
            {"name": "Anonymous", "type": "person", "confidence": 0.69},  # just under
        ],
        topics=["test"],
    )
    result = ner.extract(title="t", body="b", client=mock_client)
    # 0.7 threshold keeps 0.95 and 0.75, drops 0.5 and 0.69.
    assert result.entities == ["HighConf Co", "Marginal Co"]
    # raw still carries all of them for debugging.
    assert len(result.raw["entities"]) == 4


def test_custom_confidence_threshold(mock_client, isolated_traces):
    mock_client.messages.create.return_value = _make_response(
        entities=[
            {"name": "A", "type": "organization", "confidence": 0.85},
            {"name": "B", "type": "organization", "confidence": 0.65},
        ],
        topics=[],
    )
    result = ner.extract(
        title="t", body="b", client=mock_client, confidence_threshold=0.6
    )
    assert result.entities == ["A", "B"]


# --------------------------------------------------------------------------- #
# 3. Prompt-caching contract — cache_control on last system block
# --------------------------------------------------------------------------- #
def test_prompt_caching_marker_on_system_block(mock_client, isolated_traces):
    """The whole cost projection in the README depends on this marker
    being present. If it ever disappears the cache hit rate goes to zero
    silently — only the trace's cached_input_tokens column would tell you,
    and only at runtime. Assert it here at the construction site."""
    mock_client.messages.create.return_value = _make_response(entities=[], topics=[])
    ner.extract(title="t", body="b", client=mock_client)

    _, kwargs = mock_client.messages.create.call_args
    system = kwargs["system"]
    assert isinstance(system, list), "system must be a list of content blocks"
    assert system, "system must have at least one block"
    last_block = system[-1]
    assert last_block.get("cache_control") == {"type": "ephemeral"}, (
        "prompt-caching breakpoint missing on last system block — "
        "this kills the 70% cache discount across the backfill"
    )


def test_uses_tool_choice_for_structured_output(mock_client, isolated_traces):
    mock_client.messages.create.return_value = _make_response(entities=[], topics=[])
    ner.extract(title="t", body="b", client=mock_client)
    _, kwargs = mock_client.messages.create.call_args
    # Must force the model into tool-use; otherwise structured output isn't guaranteed.
    assert kwargs["tool_choice"] == {"type": "tool", "name": "record_extraction"}
    tools = kwargs["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "record_extraction"
    # Schema requires both entities and topics.
    required = tools[0]["input_schema"]["required"]
    assert "entities" in required and "topics" in required


def test_body_truncation_bounds_token_cost(mock_client, isolated_traces):
    mock_client.messages.create.return_value = _make_response(entities=[], topics=[])
    long_body = "X" * (ner.MAX_BODY_CHARS + 5_000)
    ner.extract(title="t", body=long_body, client=mock_client)
    _, kwargs = mock_client.messages.create.call_args
    user_content = kwargs["messages"][0]["content"]
    # The body is included after a "Body: " prefix. Total user content length
    # should be bounded by MAX_BODY_CHARS + the prefix overhead.
    assert len(user_content) <= ner.MAX_BODY_CHARS + 100


# --------------------------------------------------------------------------- #
# 4. Error handling — never raises
# --------------------------------------------------------------------------- #
def test_api_error_returns_empty_result(mock_client, isolated_traces):
    mock_client.messages.create.side_effect = RuntimeError("rate limited")
    result = ner.extract(
        title="t", body="b", client=mock_client, vault_path="sources/web/foo.md"
    )
    assert result.entities == []
    assert result.topics == []
    # Trace was still written with the error captured.
    lines = isolated_traces.read_text(encoding="utf-8").strip().splitlines()
    trace = json.loads(lines[-1])
    assert trace["error"] and "rate limited" in trace["error"]
    assert trace["vault_path"] == "sources/web/foo.md"


def test_missing_tool_use_block_returns_empty(mock_client, isolated_traces):
    # Model returned only text, no tool_use. We treat that as zero entities.
    mock_client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="...")],
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )
    result = ner.extract(title="t", body="b", client=mock_client)
    assert result.entities == []
    assert result.topics == []


def test_malformed_entity_entries_are_skipped(mock_client, isolated_traces):
    mock_client.messages.create.return_value = _make_response(
        entities=[
            {"name": "Valid Co", "type": "organization", "confidence": 0.9},
            {"name": "", "type": "person", "confidence": 0.95},  # empty name
            {"name": "Bad Conf", "type": "person", "confidence": "not_a_number"},
            "not even a dict",
        ],
        topics=["a", "", "a", "  ", "b"],  # dedupe + drop empty
    )
    result = ner.extract(title="t", body="b", client=mock_client)
    assert result.entities == ["Valid Co"]
    assert result.topics == ["a", "b"]


# --------------------------------------------------------------------------- #
# 5. Trace + usage accounting
# --------------------------------------------------------------------------- #
def test_trace_captures_token_counts_and_cost(mock_client, isolated_traces):
    mock_client.messages.create.return_value = _make_response(
        entities=[{"name": "X", "type": "concept", "confidence": 0.9}],
        topics=["y"],
        input_tokens=500,
        output_tokens=40,
        cached_input_tokens=1800,
        cache_creation_tokens=0,
    )
    result = ner.extract(
        title="t",
        body="b",
        vault_path="sources/web/test.md",
        client=mock_client,
        model="claude-haiku-4-5",
    )
    assert result.cached_input_tokens == 1800
    assert result.input_tokens == 500

    lines = isolated_traces.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    trace = json.loads(lines[0])
    assert trace["model"] == "claude-haiku-4-5"
    assert trace["input_tokens"] == 500
    assert trace["output_tokens"] == 40
    assert trace["cached_input_tokens"] == 1800
    assert trace["entities_count"] == 1
    assert trace["topics_count"] == 1
    assert trace["error"] is None
    # Cost: 500 input + 40 output + 1800 cached at Haiku 4.5 rates.
    expected = compute_cost_usd(
        model="claude-haiku-4-5",
        input_tokens=500,
        output_tokens=40,
        cached_input_tokens=1800,
    )
    assert trace["cost_usd"] == pytest.approx(expected)


def test_dedup_entities_case_insensitive(mock_client, isolated_traces):
    mock_client.messages.create.return_value = _make_response(
        entities=[
            {"name": "OpenAI", "type": "organization", "confidence": 0.95},
            {"name": "openai", "type": "organization", "confidence": 0.9},
            {"name": "OPENAI", "type": "organization", "confidence": 0.85},
        ],
        topics=[],
    )
    result = ner.extract(title="t", body="b", client=mock_client)
    assert result.entities == ["OpenAI"]
