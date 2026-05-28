"""Tests for connecting_dots.digest.wa_send."""
from __future__ import annotations

import httpx
import pytest
import respx

from connecting_dots.digest.labels import decode_row_id
from connecting_dots.digest.resurface import DigestItem
from connecting_dots.digest.wa_send import (
    _MAX_BODY,
    _MAX_BUTTON_TITLE,
    _build_button_payload,
    _build_interactive_payload,
    send_digest,
)


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
# Test: _build_button_payload shape
# --------------------------------------------------------------------------- #

def test_build_button_payload_shape():
    """Single-item payload should have the correct Meta interactive button structure."""
    items = _make_items(1)
    payload = _build_button_payload(items[0], index=1, total=5, to=TO)

    assert payload["messaging_product"] == "whatsapp"
    assert payload["type"] == "interactive"
    assert payload["to"] == TO

    interactive = payload["interactive"]
    assert interactive["type"] == "button"
    assert "header" in interactive
    assert "body" in interactive
    assert "action" in interactive

    buttons = interactive["action"]["buttons"]
    assert len(buttons) == 3
    for btn in buttons:
        assert btn["type"] == "reply"
        assert "id" in btn["reply"]
        assert "title" in btn["reply"]


def test_build_button_payload_header_label():
    """Header text should show 'Pick N of M'."""
    items = _make_items(1)
    payload = _build_button_payload(items[0], index=2, total=5, to=TO)
    assert payload["interactive"]["header"]["text"] == "Pick 2 of 5"


def test_build_button_payload_button_ids_encode_reaction():
    """Button reply IDs should encode slug + reaction code via encode_row_id."""
    item = DigestItem(slug="sources/web/test.md", title="Test", score=0.5, reason="Why", url=None)
    payload = _build_button_payload(item, index=1, total=1, to=TO)
    buttons = payload["interactive"]["action"]["buttons"]
    ids = [b["reply"]["id"] for b in buttons]

    assert any("__up" in bid for bid in ids)
    assert any("__shrug" in bid for bid in ids)
    assert any("__down" in bid for bid in ids)


def test_build_button_payload_ids_roundtrip():
    """Button reply IDs must round-trip through decode_row_id."""
    item = DigestItem(slug="sources/web/note-42.md", title="Roundtrip", score=0.9, reason=None, url=None)
    payload = _build_button_payload(item, index=1, total=1, to=TO)
    buttons = payload["interactive"]["action"]["buttons"]
    for btn in buttons:
        decoded = decode_row_id(btn["reply"]["id"])
        assert decoded is not None, f"decode_row_id returned None for id={btn['reply']['id']!r}"
        slug, reaction = decoded
        assert slug == item.slug


def test_build_button_payload_title_length():
    """Button titles must be ≤ 20 chars (WA limit)."""
    items = _make_items(1)
    payload = _build_button_payload(items[0], index=1, total=5, to=TO)
    for btn in payload["interactive"]["action"]["buttons"]:
        title = btn["reply"]["title"]
        assert len(title) <= _MAX_BUTTON_TITLE, f"Button title too long: {title!r}"


def test_build_button_payload_body_length():
    """Body text must be ≤ 1024 chars (WA limit)."""
    long_reason = "x" * 2000
    item = DigestItem(slug="s/w/x.md", title="Short Title", score=0.5, reason=long_reason, url=None)
    payload = _build_button_payload(item, index=1, total=1, to=TO)
    body = payload["interactive"]["body"]["text"]
    assert len(body) <= _MAX_BODY, f"Body too long: {len(body)} chars"


def test_build_button_payload_button_id_length():
    """Button reply IDs must be ≤ 200 chars."""
    long_slug = "sources/web/" + "a" * 300 + ".md"
    item = DigestItem(slug=long_slug, title="Long slug", score=0.5, reason=None, url=None)
    payload = _build_button_payload(item, index=1, total=1, to=TO)
    for btn in payload["interactive"]["action"]["buttons"]:
        assert len(btn["reply"]["id"]) <= 200


# --------------------------------------------------------------------------- #
# Test: backward-compat shim
# --------------------------------------------------------------------------- #

def test_build_interactive_payload_shim_returns_button_type():
    """The old _build_interactive_payload name should return a button payload."""
    items = _make_items(3)
    payload = _build_interactive_payload(items, TO)
    assert payload["interactive"]["type"] == "button"


def test_build_interactive_payload_shim_empty_raises():
    """The shim should raise ValueError on empty items."""
    with pytest.raises(ValueError, match="empty"):
        _build_interactive_payload([], TO)


# --------------------------------------------------------------------------- #
# Test: send_digest — 5 items → 5 POST calls
# --------------------------------------------------------------------------- #

@respx.mock
def test_send_digest_posts_one_per_item():
    """send_digest should make one POST per item."""
    n = 5
    call_count = 0

    def handler(request):
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"messages": [{"id": f"wamid.{call_count}"}]})

    respx.post(WA_URL).mock(side_effect=handler)

    items = _make_items(n)
    result = send_digest(items, to=TO, access_token=TOKEN, phone_number_id=PHONE_ID)

    assert call_count == n
    assert result["sent"] == n
    assert len(result["message_ids"]) == n
    assert result["failed"] == []


@respx.mock
def test_send_digest_each_payload_is_button_type():
    """Each POST payload should have interactive.type == 'button' with 3 buttons."""
    payloads = []

    def handler(request):
        import json
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.x"}]})

    respx.post(WA_URL).mock(side_effect=handler)

    items = _make_items(5)
    send_digest(items, to=TO, access_token=TOKEN, phone_number_id=PHONE_ID)

    assert len(payloads) == 5
    for payload in payloads:
        interactive = payload["interactive"]
        assert interactive["type"] == "button"
        assert len(interactive["action"]["buttons"]) == 3


# --------------------------------------------------------------------------- #
# Test: successful send
# --------------------------------------------------------------------------- #

@respx.mock
def test_send_digest_success():
    """send_digest should POST to Meta graph API and return aggregated response."""
    respx.post(WA_URL).mock(return_value=httpx.Response(
        200,
        json={"messages": [{"id": "wamid.abc123"}]},
    ))

    items = _make_items(3)
    result = send_digest(items, to=TO, access_token=TOKEN, phone_number_id=PHONE_ID)
    assert result["sent"] == 3
    assert len(result["message_ids"]) == 3


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


@respx.mock
def test_send_digest_retries_on_5xx():
    """5xx errors should be retried up to max_retries times."""
    call_count = 0

    def handler(request):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return httpx.Response(503, json={"error": "service unavailable"})
        return httpx.Response(200, json={"messages": [{"id": "wamid.retry"}]})

    respx.post(WA_URL).mock(side_effect=handler)

    items = _make_items(1)
    result = send_digest(items, to=TO, access_token=TOKEN, phone_number_id=PHONE_ID, max_retries=2)
    assert result["sent"] == 1
    assert call_count == 3
