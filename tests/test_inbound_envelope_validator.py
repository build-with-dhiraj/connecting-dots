"""Tests for the `InboundEnvelope` URL scheme validator wrapper.

The JSON Schema at `schemas/inbound_envelope.schema.json` declares
`pattern: "^https?://"` on the `url` field. `datamodel-codegen` drops
that constraint when emitting `connecting_dots/generated/inbound_envelope.py`
(the field lands as a plain `AnyUrl`). The non-codegenned wrapper at
`connecting_dots/inbound_envelope.py` restores the constraint via a
`@field_validator`.

All non-test code MUST import `InboundEnvelope` from the wrapper, so any
envelope reaching the dispatcher / handlers is guaranteed to carry an
http(s) URL with a host.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import AnyUrl, ValidationError

from connecting_dots.inbound_envelope import InboundEnvelope, Source


def _build(url: str) -> InboundEnvelope:
    return InboundEnvelope(
        message_id="env-test-1",
        url=AnyUrl(url),
        source=Source.manual,
        captured_at=datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc),
        raw_payload={},
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "https://example.com/path?q=1",
        "http://plain-http.example.com/post",
        "https://sub.domain.example.com:8443/x",
    ],
)
def test_validator_accepts_http_and_https(url: str) -> None:
    """All http(s) URLs with a host must construct successfully."""
    env = _build(url)
    assert str(env.url).startswith(("http://", "https://"))


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/file",
        "ftps://example.com/file",
        "javascript:alert(1)",
        "data:text/html,<script>",
        "mailto:hi@example.com",
        "gopher://example.com/",
        "ws://example.com/socket",
        "wss://example.com/socket",
    ],
)
def test_validator_rejects_non_http_schemes(url: str) -> None:
    """Anything that isn't http(s) must be rejected at envelope construction.

    These are the exfiltration primitives the schema's `^https?://` pattern
    is meant to lock out — `file://` for local-file reads, `ftp://` for
    out-of-band data transfer, `javascript:` for nothing legitimate.
    """
    with pytest.raises(ValidationError) as exc_info:
        _build(url)
    msg = str(exc_info.value).lower()
    assert "scheme" in msg or "not allowed" in msg or "url" in msg


def test_validator_rejects_url_without_host() -> None:
    """Pydantic's `AnyUrl` normalizes most host-less inputs (e.g. it folds
    `https:///x` into `https://x/`), so the easiest way to exercise the
    wrapper's host check is via `urlparse` returning an empty hostname.
    Pydantic 2.x rejects `https://` outright at parse time — covered here
    just to lock the contract that nothing host-less ever reaches the
    handler layer."""
    with pytest.raises(ValidationError):
        _build("https://")


def test_envelope_carries_other_fields_intact() -> None:
    """The validator must not break the rest of the model — `source`,
    `captured_at`, `raw_payload` should round-trip unchanged."""
    env = InboundEnvelope(
        message_id="abc-123",
        url=AnyUrl("https://example.com/x"),
        source=Source.whatsapp,
        captured_at=datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc),
        raw_payload={"meta_field": True},
    )
    assert env.message_id == "abc-123"
    assert env.source is Source.whatsapp
    assert env.raw_payload == {"meta_field": True}
