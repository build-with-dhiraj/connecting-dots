"""Unit tests for `connecting_dots.enrichment.ner`.

All Azure OpenAI API calls are mocked — no live network. We verify:

- Function-call response is parsed into entities + topics correctly.
- Confidence threshold drops low-confidence entities BEFORE writeback.
- `tool_choice` forces the model into a function call (no free-text output).
- The system prompt content stays byte-identical across calls — this is the
  contract Azure's automatic prompt-prefix cache depends on.
- Errors don't propagate — `extract()` returns an empty result on any failure.
- Tokens (including cached_tokens from `prompt_tokens_details`) + duration
  get into the trace log.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from connecting_dots.enrichment import ner
from connecting_dots.enrichment.tracer import compute_cost_usd


# --------------------------------------------------------------------------- #
# Helpers — build a fake AzureOpenAI chat-completion response
# --------------------------------------------------------------------------- #
def _make_response(
    *,
    entities: list[dict] | None = None,
    topics: list[str] | None = None,
    input_tokens: int = 1500,
    output_tokens: int = 50,
    cached_input_tokens: int = 0,
    include_tool_call: bool = True,
    function_name: str = "record_extraction",
    arguments_as_string: bool = True,
):
    """Build a SimpleNamespace mimicking an openai.types.chat.ChatCompletion.

    OpenAI returns tool-call arguments as a JSON-encoded string on the wire.
    We default to that shape so the parsing path under test is the real one.
    """
    args: dict | str
    payload = {"entities": entities or [], "topics": topics or []}
    args = json.dumps(payload) if arguments_as_string else payload

    tool_calls = []
    if include_tool_call:
        tool_calls.append(
            SimpleNamespace(
                id="call_test",
                type="function",
                function=SimpleNamespace(name=function_name, arguments=args),
            )
        )

    message = SimpleNamespace(
        role="assistant",
        content=None,
        tool_calls=tool_calls or None,
    )

    return SimpleNamespace(
        id="chatcmpl-test",
        choices=[SimpleNamespace(index=0, message=message, finish_reason="tool_calls")],
        usage=SimpleNamespace(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            prompt_tokens_details=SimpleNamespace(cached_tokens=cached_input_tokens),
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
    """A fake AzureOpenAI with a recording `chat.completions.create`."""
    client = MagicMock()
    return client


# --------------------------------------------------------------------------- #
# 1. Function-call parsing
# --------------------------------------------------------------------------- #
def test_extract_parses_function_call_response(mock_client, isolated_traces):
    mock_client.chat.completions.create.return_value = _make_response(
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
    assert result.error is None


def test_extract_parses_dict_arguments_as_well(mock_client, isolated_traces):
    """Some test mocks pass `arguments` as a dict instead of a JSON string;
    the parser must handle both shapes."""
    mock_client.chat.completions.create.return_value = _make_response(
        entities=[{"name": "X", "type": "concept", "confidence": 0.9}],
        topics=["t"],
        arguments_as_string=False,
    )
    result = ner.extract(title="t", body="b", client=mock_client)
    assert result.entities == ["X"]
    assert result.topics == ["t"]


# --------------------------------------------------------------------------- #
# 2. Confidence threshold drops low-confidence entities
# --------------------------------------------------------------------------- #
def test_confidence_threshold_drops_low_confidence(mock_client, isolated_traces):
    mock_client.chat.completions.create.return_value = _make_response(
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
    mock_client.chat.completions.create.return_value = _make_response(
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
# 3. Prompt-cache contract — stable system prompt across calls
# --------------------------------------------------------------------------- #
def test_system_prompt_is_stable_across_calls(mock_client, isolated_traces):
    """Azure OpenAI auto-caches the prompt prefix when it's bit-identical
    across calls. We do not pass any cache_control flag — instead, the
    contract is that messages[0] (system) is byte-stable. Test that two
    successive calls send the exact same system prompt content."""
    mock_client.chat.completions.create.return_value = _make_response(
        entities=[], topics=[]
    )
    ner.extract(title="note one", body="body one", client=mock_client)
    ner.extract(title="note two — different body", body="completely different", client=mock_client)

    first_call_kwargs = mock_client.chat.completions.create.call_args_list[0].kwargs
    second_call_kwargs = mock_client.chat.completions.create.call_args_list[1].kwargs

    sys_first = first_call_kwargs["messages"][0]
    sys_second = second_call_kwargs["messages"][0]
    assert sys_first == sys_second, (
        "system prompt drifted between calls — Azure's automatic prompt-prefix "
        "cache will miss and the cost projection in the README is wrong"
    )
    assert sys_first["role"] == "system"
    # And the user messages MUST differ (sanity check on the test itself).
    assert first_call_kwargs["messages"][1] != second_call_kwargs["messages"][1]


def test_uses_tool_choice_for_structured_output(mock_client, isolated_traces):
    mock_client.chat.completions.create.return_value = _make_response(
        entities=[], topics=[]
    )
    ner.extract(title="t", body="b", client=mock_client)
    _, kwargs = mock_client.chat.completions.create.call_args
    # Must force the model into the function call; otherwise structured
    # output isn't guaranteed.
    assert kwargs["tool_choice"] == {
        "type": "function",
        "function": {"name": "record_extraction"},
    }
    tools = kwargs["tools"]
    assert len(tools) == 1
    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "record_extraction"
    # Schema requires both entities and topics.
    required = tools[0]["function"]["parameters"]["required"]
    assert "entities" in required and "topics" in required


def test_body_truncation_bounds_token_cost(mock_client, isolated_traces):
    mock_client.chat.completions.create.return_value = _make_response(
        entities=[], topics=[]
    )
    long_body = "X" * (ner.MAX_BODY_CHARS + 5_000)
    ner.extract(title="t", body=long_body, client=mock_client)
    _, kwargs = mock_client.chat.completions.create.call_args
    user_content = kwargs["messages"][1]["content"]
    # The body is included after a "Body: " prefix. Total user content length
    # should be bounded by MAX_BODY_CHARS + the prefix overhead.
    assert len(user_content) <= ner.MAX_BODY_CHARS + 100


# --------------------------------------------------------------------------- #
# 4. Error handling — never raises
# --------------------------------------------------------------------------- #
def test_api_error_returns_empty_result(mock_client, isolated_traces):
    mock_client.chat.completions.create.side_effect = RuntimeError("rate limited")
    result = ner.extract(
        title="t", body="b", client=mock_client, vault_path="sources/web/foo.md"
    )
    assert result.entities == []
    assert result.topics == []
    assert result.error and "rate limited" in result.error
    # Trace was still written with the error captured.
    lines = isolated_traces.read_text(encoding="utf-8").strip().splitlines()
    trace = json.loads(lines[-1])
    assert trace["error"] and "rate limited" in trace["error"]
    assert trace["vault_path"] == "sources/web/foo.md"


def test_openai_authentication_error_returns_empty_result(mock_client, isolated_traces):
    """A class-name closer to the real openai.AuthenticationError shape — we
    just need to assert that any subclass of Exception is caught and surfaced
    as `.error`, not raised."""

    class _AuthErr(Exception):
        pass

    _AuthErr.__name__ = "AuthenticationError"
    mock_client.chat.completions.create.side_effect = _AuthErr("invalid api key")
    result = ner.extract(title="t", body="b", client=mock_client)
    assert result.entities == []
    assert result.error is not None
    assert "AuthenticationError" in result.error
    assert "invalid api key" in result.error


def test_missing_tool_call_returns_empty(mock_client, isolated_traces):
    # Model returned only text, no tool_calls. We treat that as zero entities.
    mock_client.chat.completions.create.return_value = SimpleNamespace(
        choices=[
            SimpleNamespace(
                index=0,
                message=SimpleNamespace(
                    role="assistant",
                    content="just text, no function call",
                    tool_calls=None,
                ),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        ),
    )
    result = ner.extract(title="t", body="b", client=mock_client)
    assert result.entities == []
    assert result.topics == []
    assert result.error is None  # not an error per se — just empty extraction


def test_malformed_entity_entries_are_skipped(mock_client, isolated_traces):
    mock_client.chat.completions.create.return_value = _make_response(
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
    mock_client.chat.completions.create.return_value = _make_response(
        entities=[{"name": "X", "type": "concept", "confidence": 0.9}],
        topics=["y"],
        input_tokens=2300,  # OpenAI's prompt_tokens INCLUDES cached prefix
        output_tokens=40,
        cached_input_tokens=1800,
    )
    result = ner.extract(
        title="t",
        body="b",
        vault_path="sources/web/test.md",
        client=mock_client,
        model="gpt-4.1",
    )
    assert result.cached_input_tokens == 1800
    assert result.input_tokens == 2300

    lines = isolated_traces.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    trace = json.loads(lines[0])
    assert trace["model"] == "gpt-4.1"
    assert trace["input_tokens"] == 2300
    assert trace["output_tokens"] == 40
    assert trace["cached_input_tokens"] == 1800
    assert trace["entities_count"] == 1
    assert trace["topics_count"] == 1
    assert trace["error"] is None
    expected = compute_cost_usd(
        model="gpt-4.1",
        input_tokens=2300,
        output_tokens=40,
        cached_input_tokens=1800,
    )
    assert trace["cost_usd"] == pytest.approx(expected)


def test_dedup_entities_case_insensitive(mock_client, isolated_traces):
    mock_client.chat.completions.create.return_value = _make_response(
        entities=[
            {"name": "OpenAI", "type": "organization", "confidence": 0.95},
            {"name": "openai", "type": "organization", "confidence": 0.9},
            {"name": "OPENAI", "type": "organization", "confidence": 0.85},
        ],
        topics=[],
    )
    result = ner.extract(title="t", body="b", client=mock_client)
    assert result.entities == ["OpenAI"]


def test_default_model_is_gpt_4_1(mock_client, isolated_traces, monkeypatch):
    monkeypatch.delenv("NER_MODEL", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT", raising=False)
    mock_client.chat.completions.create.return_value = _make_response(
        entities=[], topics=[]
    )
    ner.extract(title="t", body="b", client=mock_client)
    _, kwargs = mock_client.chat.completions.create.call_args
    assert kwargs["model"] == "gpt-4.1"
