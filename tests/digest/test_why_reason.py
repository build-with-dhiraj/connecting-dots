"""Tests for connecting_dots.digest.why_reason (LLM mocked)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from connecting_dots.digest.resurface import DigestItem
from connecting_dots.digest.why_reason import WhyResult, generate_reason, generate_reasons


# --------------------------------------------------------------------------- #
# Mock AzureOpenAI client
# --------------------------------------------------------------------------- #

def _make_mock_client(reason: str = "Revisit this note on AI to connect recent patterns.") -> MagicMock:
    """Build a mock AzureOpenAI client that returns a canned reason."""
    mock_client = MagicMock()
    fn_mock = MagicMock()
    fn_mock.name = "record_reason"
    fn_mock.arguments = json.dumps({"reason": reason})

    call_mock = MagicMock()
    call_mock.function = fn_mock

    message_mock = MagicMock()
    message_mock.tool_calls = [call_mock]

    choice_mock = MagicMock()
    choice_mock.message = message_mock

    response_mock = MagicMock()
    response_mock.choices = [choice_mock]
    response_mock.usage.prompt_tokens = 100
    response_mock.usage.completion_tokens = 20

    mock_client.chat.completions.create.return_value = response_mock
    return mock_client


def _make_digest_item(slug: str = "sources/web/test.md", title: str = "Test Note",
                       score: float = 0.75) -> DigestItem:
    return DigestItem(slug=slug, title=title, score=score, reason="", url="https://example.com")


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def test_generate_reason_returns_why_result():
    """generate_reason should return a WhyResult with a non-empty reason."""
    item = _make_digest_item()
    client = _make_mock_client("Revisit this AI note — connected to your recent Anthropic saves.")
    result = generate_reason(item, topics=["ai", "llm"], client=client)
    assert isinstance(result, WhyResult)
    assert result.reason
    assert not result.error


def test_generate_reason_uses_title_as_fallback_on_empty_response():
    """If the LLM returns an empty reason, fallback to 'Revisit: <title>'."""
    item = _make_digest_item(title="My Special Note")
    client = _make_mock_client(reason="")  # empty reason
    result = generate_reason(item, client=client)
    assert "My Special Note" in result.reason
    assert result.error  # error is set for empty reason


def test_generate_reason_handles_api_error():
    """If the LLM call fails, reason falls back to title and error is populated."""
    item = _make_digest_item(title="Error Test Note")
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = RuntimeError("API timeout")

    result = generate_reason(item, client=mock_client)
    assert result.reason  # fallback populated
    assert "Error Test Note" in result.reason
    assert result.error
    assert "RuntimeError" in result.error


def test_generate_reasons_fills_all_items():
    """generate_reasons should return same count as input, all with reasons."""
    items = [
        _make_digest_item(slug=f"sources/web/note-{i}.md", title=f"Note {i}")
        for i in range(3)
    ]
    client = _make_mock_client("A great reason to revisit.")

    results = generate_reasons(items, client=client)
    assert len(results) == 3
    for r in results:
        assert isinstance(r, DigestItem)
        assert r.reason  # all populated
