"""Validated wrapper around the codegenned `InboundEnvelope` model.

`connecting_dots/generated/inbound_envelope.py` is produced by
`datamodel-codegen` from `schemas/inbound_envelope.schema.json`. The
codegen drops the JSON-Schema `pattern: "^https?://"` constraint on the
`url` field when emitting the Pydantic v2 model — it lands as a plain
`AnyUrl | None`, which (a) happily accepts `file://`, `ftp://`,
`javascript:` and similar non-network schemes, and (b) does not enforce
the cross-field rule that `url` is required iff `message_type == "url"`.

The schema's intent is that:
- Only `http(s)://` URLs ever reach the dispatcher / fetch handlers.
- An envelope whose `message_type == "url"` MUST carry a `url`.
- A media envelope (image/audio/video/document/sticker) MUST carry a
  non-empty `media_id`.
- A `text` envelope MUST carry non-empty `text`.

This wrapper enforces those invariants without hand-editing the
generated file (which gets regenerated on every `make gen-types-py`).

**All non-test code MUST import `InboundEnvelope` from this module**, not
from `connecting_dots.generated.inbound_envelope`. The codegen output is
kept as the source of truth for the field shapes and the JSON Schema
contract, but this wrapper is the runtime boundary.
"""
from __future__ import annotations

from urllib.parse import urlparse

from pydantic import field_validator, model_validator

from connecting_dots.generated.inbound_envelope import (
    InboundEnvelope as _GeneratedInboundEnvelope,
)
from connecting_dots.generated.inbound_envelope import MessageType, Source

# Mirrors the JSON-Schema `pattern: "^https?://"`. We keep the set as a
# tuple of literal scheme strings so the check is unambiguous and the
# error message can list what was actually permitted.
_ALLOWED_URL_SCHEMES = ("http", "https")

# Message types that must carry a non-empty `media_id`. `sticker` is in
# the schema enum for completeness — the webhook currently filters it
# out, but if one does reach here we still require a media_id so
# component #5 has something to fetch.
_MEDIA_TYPES = frozenset(
    {
        MessageType.image,
        MessageType.audio,
        MessageType.video,
        MessageType.document,
        MessageType.sticker,
    }
)


class InboundEnvelope(_GeneratedInboundEnvelope):
    """Codegenned envelope + cross-field invariants the codegen omits.

    Pydantic v2 `AnyUrl` accepts `file://`, `ftp://`, `javascript:` etc;
    the JSON Schema's `pattern: "^https?://"` constraint on the `url`
    field is dropped by `datamodel-codegen`. This subclass restores it
    and additionally enforces the cross-field rules tying `message_type`
    to which payload fields must be present:

      - message_type == "url"     -> url required, http(s) only
      - message_type in MEDIA    -> media_id required and non-empty
      - message_type == "text"   -> text required and non-empty

    Any envelope that survives `model_validate(...)` from this class is
    guaranteed to satisfy those invariants.
    """

    @field_validator("url", mode="after")
    @classmethod
    def _enforce_http_scheme(cls, value: object) -> object:
        """Reject non-http(s) schemes. Absent (None) is fine here — the
        cross-field validator decides whether absence is allowed based
        on `message_type`."""
        if value is None:
            return value
        url_str = str(value)
        if not url_str:
            return value
        try:
            parts = urlparse(url_str)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"url is not parseable: {exc}") from exc

        scheme = (parts.scheme or "").lower()
        if scheme not in _ALLOWED_URL_SCHEMES:
            raise ValueError(
                f"url scheme {scheme!r} not allowed; "
                f"must be one of {_ALLOWED_URL_SCHEMES}"
            )
        if not parts.hostname:
            raise ValueError("url is missing a host component")
        return value

    @model_validator(mode="after")
    def _enforce_message_type_consistency(self) -> "InboundEnvelope":
        """Cross-field rules tying `message_type` to required payload fields.

        Codegen produces every payload field as Optional with no
        conditional requirement. The schema's intent is that the
        meaningful field for a given `message_type` MUST be present.
        Enforcing this here means handlers never have to defensively
        re-check for the basics.
        """
        mt = self.message_type

        if mt == MessageType.url:
            if self.url is None or not str(self.url):
                raise ValueError(
                    'message_type == "url" requires a non-empty `url`'
                )

        elif mt in _MEDIA_TYPES:
            if not self.media_id:
                raise ValueError(
                    f'message_type == "{mt.value}" requires a non-empty `media_id`'
                )

        elif mt == MessageType.text:
            if not (self.text and self.text.strip()):
                raise ValueError(
                    'message_type == "text" requires a non-empty `text`'
                )

        # location / contacts / interactive / unknown have no required
        # payload field beyond raw_payload (already required by the
        # generated model). They're forwarded for component #5 to decide.

        return self


__all__ = ("InboundEnvelope", "MessageType", "Source")
