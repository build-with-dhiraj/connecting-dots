"""Tests for connecting_dots.enrichment.body_cleanup."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from connecting_dots.enrichment.body_cleanup import (
    BodyCleanupResult,
    _extract_tldr_prefix,
    clean_body,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _make_cleanup_response(
    *,
    cleaned_markdown: str = "# Article\n\nReal content here.",
    removed_kinds: list[str] | None = None,
    removed_count: int = 2,
    input_tokens: int = 800,
    output_tokens: int = 200,
    cached_input_tokens: int = 600,
    include_tool_call: bool = True,
    arguments_as_string: bool = True,
):
    payload: dict = {"cleaned_markdown": cleaned_markdown, "removed_count": removed_count}
    if removed_kinds is not None:
        payload["removed_kinds"] = removed_kinds

    args = json.dumps(payload) if arguments_as_string else payload

    tool_calls = []
    if include_tool_call:
        tool_calls.append(
            SimpleNamespace(
                id="call_cleanup",
                type="function",
                function=SimpleNamespace(name="record_cleaned_body", arguments=args),
            )
        )

    message = SimpleNamespace(
        role="assistant", content=None, tool_calls=tool_calls or None
    )
    return SimpleNamespace(
        id="chatcmpl-cleanup",
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
    client.chat.completions.create.return_value = _make_cleanup_response()
    return client


# --------------------------------------------------------------------------- #
# _extract_tldr_prefix
# --------------------------------------------------------------------------- #
def test_extract_tldr_prefix_present():
    body = "> **TL;DR.** Sentence one. Sentence two.\n\n# Title\n\nContent here."
    tldr, rest = _extract_tldr_prefix(body)
    assert tldr.startswith("> **TL;DR.**")
    assert "# Title" in rest
    assert "Content here." in rest


def test_extract_tldr_prefix_absent():
    body = "# Title\n\nNo TL;DR here."
    tldr, rest = _extract_tldr_prefix(body)
    assert tldr == ""
    assert rest == body


# --------------------------------------------------------------------------- #
# clean_body — happy path
# --------------------------------------------------------------------------- #
def test_llm_response_parsing_mocked(mock_client):
    body = "x" * 1000
    result = clean_body(body=body, client=mock_client)
    assert isinstance(result, BodyCleanupResult)
    assert result.cleaned_markdown == "# Article\n\nReal content here."
    assert result.error is None


def test_token_accounting(mock_client):
    result = clean_body(body="x" * 1000, client=mock_client)
    assert result.input_tokens == 800
    assert result.output_tokens == 200
    assert result.cached_input_tokens == 600


def test_removed_kinds_parsed(mock_client):
    mock_client.chat.completions.create.return_value = _make_cleanup_response(
        removed_kinds=["cookie", "navigation", "newsletter"]
    )
    result = clean_body(body="x" * 1000, client=mock_client)
    assert "cookie" in result.removed_kinds
    assert "navigation" in result.removed_kinds
    assert "newsletter" in result.removed_kinds


# --------------------------------------------------------------------------- #
# clean_body — error path
# --------------------------------------------------------------------------- #
def test_error_does_not_raise():
    bad = MagicMock()
    bad.chat.completions.create.side_effect = TimeoutError("API timeout")
    result = clean_body(body="x" * 1000, client=bad)
    assert result.error is not None
    assert "TimeoutError" in result.error
    assert result.cleaned_markdown == ""


def test_empty_tool_call_returns_error(mock_client):
    mock_client.chat.completions.create.return_value = _make_cleanup_response(
        include_tool_call=False
    )
    result = clean_body(body="x" * 1000, client=mock_client)
    assert result.error is not None
    assert result.cleaned_markdown == ""
