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

from connecting_dots.inbound_envelope import InboundEnvelope, MessageType, Source


def _build(url: str) -> InboundEnvelope:
    return InboundEnvelope(
        message_id="env-test-1",
        message_type=MessageType.url,
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
        message_type=MessageType.url,
        url=AnyUrl("https://example.com/x"),
        source=Source.whatsapp,
        captured_at=datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc),
        raw_payload={"meta_field": True},
    )
    assert env.message_id == "abc-123"
    assert env.source is Source.whatsapp
    assert env.raw_payload == {"meta_field": True}


# --------------------------------------------------------------------------- #
# message_type cross-field invariants
# --------------------------------------------------------------------------- #
_BASE: dict[str, object] = {
    "message_id": "env-mt-test",
    "source": "whatsapp",
    "captured_at": datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
    "raw_payload": {},
}


def test_url_type_requires_url_field() -> None:
    """message_type == "url" without a `url` must be rejected."""
    with pytest.raises(ValidationError):
        InboundEnvelope.model_validate({**_BASE, "message_type": "url"})


def test_url_type_with_url_validates() -> None:
    env = InboundEnvelope.model_validate(
        {**_BASE, "message_type": "url", "url": "https://example.com/x"}
    )
    assert env.message_type is MessageType.url
    assert str(env.url) == "https://example.com/x"


def test_url_type_rejects_non_http_scheme() -> None:
    """Even with message_type=url, non-http schemes are still blocked."""
    with pytest.raises(ValidationError):
        InboundEnvelope.model_validate(
            {**_BASE, "message_type": "url", "url": "file:///etc/passwd"}
        )


def test_text_type_requires_non_empty_text() -> None:
    with pytest.raises(ValidationError):
        InboundEnvelope.model_validate({**_BASE, "message_type": "text"})
    with pytest.raises(ValidationError):
        InboundEnvelope.model_validate({**_BASE, "message_type": "text", "text": "   "})


def test_text_type_with_text_validates_and_url_absent() -> None:
    env = InboundEnvelope.model_validate(
        {**_BASE, "message_type": "text", "text": "hello world"}
    )
    assert env.message_type is MessageType.text
    assert env.url is None
    assert env.text == "hello world"


@pytest.mark.parametrize(
    "message_type",
    ["image", "audio", "video", "document", "sticker"],
)
def test_media_types_require_media_id(message_type: str) -> None:
    """Image/audio/video/document/sticker MUST carry a non-empty media_id —
    otherwise component #5 has nothing to fetch from Meta."""
    with pytest.raises(ValidationError):
        InboundEnvelope.model_validate({**_BASE, "message_type": message_type})
    with pytest.raises(ValidationError):
        InboundEnvelope.model_validate(
            {**_BASE, "message_type": message_type, "media_id": ""}
        )


def test_image_envelope_carries_media_metadata() -> None:
    env = InboundEnvelope.model_validate(
        {
            **_BASE,
            "message_type": "image",
            "media_id": "meta-xyz",
            "media_mime_type": "image/jpeg",
            "text": "a caption",
        }
    )
    assert env.url is None
    assert env.media_id == "meta-xyz"
    assert env.media_mime_type == "image/jpeg"
    assert env.text == "a caption"


def test_document_envelope_carries_filename() -> None:
    env = InboundEnvelope.model_validate(
        {
            **_BASE,
            "message_type": "document",
            "media_id": "meta-doc",
            "media_mime_type": "application/pdf",
            "media_filename": "invoice.pdf",
        }
    )
    assert env.media_filename == "invoice.pdf"


@pytest.mark.parametrize("message_type", ["location", "contacts", "interactive", "unknown"])
def test_metadata_only_types_need_no_payload_fields(message_type: str) -> None:
    """location/contacts/interactive/unknown carry only raw_payload — no
    url/media_id/text is required to construct."""
    env = InboundEnvelope.model_validate({**_BASE, "message_type": message_type})
    assert env.url is None
    assert env.media_id is None


def test_url_absent_is_fine_for_non_url_types() -> None:
    """The url field is OPTIONAL except when message_type == 'url'. This
    locks the regression: previously every envelope required a url."""
    env = InboundEnvelope.model_validate(
        {**_BASE, "message_type": "text", "text": "no link"}
    )
    assert env.url is None
