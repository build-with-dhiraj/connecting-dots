"""Tests for connecting_dots.digest.wa_send."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from connecting_dots.digest.resurface import DigestItem
from connecting_dots.digest.wa_send import _build_interactive_payload, send_digest


def _make_items(n: int = 3) -> list[DigestItem]:
    return [
        DigestItem(
            slug=f"sources/web/note-{i}.md",
            title=f"Note {i}: A Great Save",
            score=0.8 - i * 0.1,
            reason=f"Revisit this because of topic connection {i}.",
            url=f"https://example.com/{i}",
        )
        for i in range(n)
    ]


PHONE_ID = "12345678901"
TOKEN = "test-token"
TO = "918595087697"
WA_URL = f"https://graph.facebook.com/v22.0/{PHONE_ID}/messages"


# --------------------------------------------------------------------------- #
# Test: request shape
# --------------------------------------------------------------------------- #

def test_build_interactive_payload_shape():
    """Payload should have the correct Meta interactive list structure."""
    items = _make_items(3)
    payload = _build_interactive_payload(items, TO)

    assert payload["messaging_product"] == "whatsapp"
    assert payload["type"] == "interactive"
    assert payload["to"] == TO

    interactive = payload["interactive"]
    assert interactive["type"] == "list"
    assert "header" in interactive
    assert "body" in interactive
    assert "action" in interactive

    # Each item should have a section with 3 reaction rows
    sections = interactive["action"]["sections"]
    assert len(sections) == len(items)
    for section in sections:
        assert len(section["rows"]) == 3  # up, shrug, down
        for row in section["rows"]:
            assert "__" in row["id"]  # slug__reaction encoded


def test_build_interactive_payload_encodes_row_ids():
    """Row IDs should encode slug and reaction code."""
    items = [DigestItem(slug="sources/web/test.md", title="Test", score=0.5, reason="Why", url=None)]
    payload = _build_interactive_payload(items, TO)
    rows = payload["interactive"]["action"]["sections"][0]["rows"]
    row_ids = [r["id"] for r in rows]
    assert any("__up" in rid for rid in row_ids)
    assert any("__shrug" in rid for rid in row_ids)
    assert any("__down" in rid for rid in row_ids)


# --------------------------------------------------------------------------- #
# Test: successful send
# --------------------------------------------------------------------------- #

@respx.mock
def test_send_digest_success():
    """send_digest should POST to Meta graph API and return response."""
    respx.post(WA_URL).mock(return_value=httpx.Response(
        200,
        json={"messages": [{"id": "wamid.abc123"}]},
    ))

    items = _make_items(3)
    result = send_digest(items, to=TO, access_token=TOKEN, phone_number_id=PHONE_ID)
    assert result["messages"][0]["id"] == "wamid.abc123"


# --------------------------------------------------------------------------- #
# Test: error handling / retry
# --------------------------------------------------------------------------- #

@respx.mock
def test_send_digest_raises_on_4xx():
    """4xx errors should raise immediately without retry."""
    respx.post(WA_URL).mock(return_value=httpx.Response(
        401,
        json={"error": {"message": "Invalid OAuth access token"}},
    ))

    items = _make_items(2)
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        send_digest(items, to=TO, access_token="bad-token", phone_number_id=PHONE_ID)
    assert exc_info.value.response.status_code == 401


@respx.mock
def test_send_digest_raises_on_empty_items():
    """Empty items list should raise ValueError before any HTTP call."""
    with pytest.raises(ValueError, match="empty"):
        send_digest([], to=TO, access_token=TOKEN, phone_number_id=PHONE_ID)
