"""Handler protocol — the load-bearing contract every per-domain handler implements.

A handler:
- Identifies itself with a stable `name` (used as the `handler` field on `NoteRecord`).
- Decides whether it owns a URL via `matches(url)`. First match wins inside the
  dispatcher, so handler ordering in the registry matters (specific before generic).
- Produces a `NoteRecord` from an `InboundEnvelope` via `handle(envelope)`.

Handlers MUST be side-effect-free with respect to the vault: the dispatcher owns
all vault writes. A handler that fails should raise — the dispatcher converts
exceptions into a degraded `NoteRecord` so the user always sees the capture.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from connecting_dots.inbound_envelope import InboundEnvelope
from connecting_dots.types import NoteRecord


class HandlerNotFound(Exception):
    """Raised when no registered handler matches a URL AND no fallback exists.

    In practice the `web` handler is the catch-all fallback, so this should
    only surface in tests with an empty registry.
    """


@runtime_checkable
class Handler(Protocol):
    """Per-domain URL handler. Implemented by `handlers/youtube.py` et al."""

    name: str  # stable identifier — e.g. "youtube", "instagram", "web", "linkedin"

    def matches(self, url: str) -> bool:
        """Return True iff this handler owns `url`. Cheap and side-effect-free."""
        ...

    def handle(self, envelope: InboundEnvelope) -> NoteRecord:
        """Extract content for `envelope.url` and return a `NoteRecord`.

        May raise on extraction failure; the dispatcher wraps the error into a
        degraded record so the URL is never silently dropped.
        """
        ...
