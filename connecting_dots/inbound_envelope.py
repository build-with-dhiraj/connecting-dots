"""Validated wrapper around the codegenned `InboundEnvelope` model.

`connecting_dots/generated/inbound_envelope.py` is produced by
`datamodel-codegen` from `schemas/inbound_envelope.schema.json`. The
codegen drops the JSON-Schema `pattern: "^https?://"` constraint on the
`url` field when emitting the Pydantic v2 model — it lands as a plain
`AnyUrl`, which happily accepts `file://`, `ftp://`, `javascript:` and
similar non-network schemes.

The schema's intent is that *only* `http(s)://` URLs ever reach the
dispatcher / fetch handlers. To enforce that without hand-editing the
generated file (which gets regenerated on every `make gen-types-py`),
this module defines a subclass that adds a `@field_validator` rejecting
any other scheme.

**All non-test code MUST import `InboundEnvelope` from this module**, not
from `connecting_dots.generated.inbound_envelope`. The codegen output is
kept as the source of truth for the field shapes and the JSON Schema
contract, but this wrapper is the runtime boundary.
"""
from __future__ import annotations

from urllib.parse import urlparse

from pydantic import field_validator

from connecting_dots.generated.inbound_envelope import (
    InboundEnvelope as _GeneratedInboundEnvelope,
)
from connecting_dots.generated.inbound_envelope import Source

# Mirrors the JSON-Schema `pattern: "^https?://"`. We keep the set as a
# tuple of literal scheme strings so the check is unambiguous and the
# error message can list what was actually permitted.
_ALLOWED_URL_SCHEMES = ("http", "https")


class InboundEnvelope(_GeneratedInboundEnvelope):
    """Codegenned envelope + a scheme check that the codegen omits.

    Pydantic v2 `AnyUrl` accepts `file://`, `ftp://`, `javascript:` etc;
    the JSON Schema's `pattern: "^https?://"` constraint on the `url`
    field is dropped by `datamodel-codegen`. This subclass restores it.
    Any envelope that survives `model_validate(...)` from this class
    is guaranteed to carry an http(s) URL with a host.
    """

    @field_validator("url", mode="after")
    @classmethod
    def _enforce_http_scheme(cls, value: object) -> object:
        # `value` arrives as a pydantic AnyUrl (or coerced equivalent).
        # Stringify and parse to keep this independent of pydantic's
        # internal URL representation across versions.
        url_str = str(value)
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


__all__ = ("InboundEnvelope", "Source")
