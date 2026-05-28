"""Tests for connecting_dots.enrichment.moc."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from connecting_dots.enrichment.moc import MoCResult, _topic_slug, synthesize


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _make_moc_response(
    *,
    synthesis: str = "Your saves on AI Engineering show...",
    essential_notes: list[dict] | None = None,
    input_tokens: int = 1800,
    output_tokens: int = 300,
    cached_input_tokens: int = 1500,
    include_tool_call: bool = True,
    arguments_as_string: bool = True,
):
    if essential_notes is None:
        essential_notes = [{"title": "Note A", "reason": "Foundational."}]
    payload = {"synthesis": synthesis, "essential_notes": essential_notes}
    args = json.dumps(payload) if arguments_as_string else payload

    tool_calls = []
    if include_tool_call:
        tool_calls.append(
            SimpleNamespace(
                id="call_moc",
                type="function",
                function=SimpleNamespace(name="record_moc_synthesis", arguments=args),
            )
        )

    message = SimpleNamespace(
        role="assistant", content=None, tool_calls=tool_calls or None
    )
    return SimpleNamespace(
        id="chatcmpl-moc",
        choices=[SimpleNamespace(index=0, message=message, finish_reason="tool_calls")],
        usage=SimpleNamespace(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            prompt_tokens_details=SimpleNamespace(cached_tokens=cached_input_tokens),
        ),
    )


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.chat.completions.create.return_value = _make_moc_response()
    return client


@pytest.fixture
def isolated_traces(tmp_path, monkeypatch):
    traces = tmp_path / "moc_traces.jsonl"
    monkeypatch.setenv("CONNECTING_DOTS_MOC_TRACES", str(traces))
    return traces


# --------------------------------------------------------------------------- #
# _topic_slug
# --------------------------------------------------------------------------- #
def test_topic_slug_strips_prefix():
    assert _topic_slug("#topic/ai-engineering") == "ai-engineering"


def test_topic_slug_no_prefix():
    assert _topic_slug("machine learning") == "machine-learning"


def test_topic_slug_cleans_special_chars():
    assert _topic_slug("#topic/AI & ML!") == "ai-ml"


# --------------------------------------------------------------------------- #
# synthesize — happy path
# --------------------------------------------------------------------------- #
def test_synthesize_returns_result(mock_client, isolated_traces):
    result = synthesize(
        topic="ai engineering",
        notes=[{"title": "Note A", "body_preview": "AI stuff"}],
        client=mock_client,
    )
    assert isinstance(result, MoCResult)
    assert result.synthesis == "Your saves on AI Engineering show..."
    assert len(result.essential_notes) == 1
    assert result.essential_notes[0]["title"] == "Note A"
    assert result.error is None


def test_synthesize_token_accounting(mock_client, isolated_traces):
    result = synthesize(
        topic="ai engineering",
        notes=[{"title": "Note A", "body_preview": "x"}],
        client=mock_client,
    )
    assert result.input_tokens == 1800
    assert result.output_tokens == 300
    assert result.cached_input_tokens == 1500


def test_synthesize_writes_trace(mock_client, isolated_traces):
    synthesize(
        topic="ai engineering",
        notes=[{"title": "N", "body_preview": "body"}],
        client=mock_client,
    )
    lines = isolated_traces.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["topic"] == "ai engineering"
    assert rec["error"] is None


def test_synthesize_error_does_not_raise(isolated_traces):
    bad_client = MagicMock()
    bad_client.chat.completions.create.side_effect = RuntimeError("network down")
    result = synthesize(
        topic="ai engineering",
        notes=[],
        client=bad_client,
    )
    assert result.error is not None
    assert "RuntimeError" in result.error
    assert result.synthesis == ""


def test_synthesize_no_tool_call_returns_empty(isolated_traces):
    client = MagicMock()
    client.chat.completions.create.return_value = _make_moc_response(include_tool_call=False)
    result = synthesize(
        topic="ai engineering",
        notes=[{"title": "N", "body_preview": "body"}],
        client=client,
    )
    assert result.synthesis == ""
    assert result.essential_notes == []


def test_synthesize_arguments_as_dict(isolated_traces):
    """Handles the case where arguments come back as a dict instead of JSON string."""
    client = MagicMock()
    client.chat.completions.create.return_value = _make_moc_response(
        arguments_as_string=False
    )
    result = synthesize(
        topic="ai engineering",
        notes=[{"title": "N", "body_preview": "b"}],
        client=client,
    )
    assert result.synthesis == "Your saves on AI Engineering show..."


def test_synthesize_prompt_system_content_stable():
    """System prompt must be byte-identical across calls for cache hits."""
    from connecting_dots.enrichment.moc import _SYSTEM_PROMPT

    # Run it twice and check the system prompt arg is the same object / content
    calls = []

    class _CapturingClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    calls.append(kwargs["messages"][0]["content"])
                    return _make_moc_response()

    client = _CapturingClient()
    for _ in range(2):
        synthesize(topic="x", notes=[], client=client, trace=False)  # type: ignore[arg-type]

    assert calls[0] == calls[1] == _SYSTEM_PROMPT
