"""Channel-agnostic URL dispatcher interface.

Consumed by all ingest channels (component #1 WhatsApp inbound, component #6
mailto IMAP, future LinkedIn/manual). Component #2 will provide the real
implementation that routes to the per-domain handlers (#3 YT, #4 IG, #5 web/PDF).

This module exposes:
    - SourceChannel: the closed set of ingest sources
    - CapturedURL: the normalized payload every channel produces
    - URLDispatcher: Protocol that the real dispatcher must satisfy
    - dispatch_url: module-level convenience that delegates to the registered
      dispatcher; defaults to a mock that logs (so channels can be developed
      and tested before component #2 lands)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

logger = logging.getLogger(__name__)

SourceChannel = Literal["whatsapp", "mailto", "linkedin", "manual"]


@dataclass(frozen=True)
class CapturedURL:
    """Normalized capture event from any channel."""

    url: str
    source: SourceChannel
    captured_at: datetime
    raw_payload: dict[str, Any] = field(default_factory=dict)


class URLDispatcher(Protocol):
    """Protocol component #2 must implement."""

    def dispatch_url(
        self,
        url: str,
        source: SourceChannel,
        captured_at: datetime,
        raw_payload: dict[str, Any],
    ) -> None: ...


class _MockDispatcher:
    """Stand-in dispatcher used until component #2 ships.

    Logs the capture and stores it in-memory so tests can assert on it.
    """

    def __init__(self) -> None:
        self.captures: list[CapturedURL] = []

    def dispatch_url(
        self,
        url: str,
        source: SourceChannel,
        captured_at: datetime,
        raw_payload: dict[str, Any],
    ) -> None:
        event = CapturedURL(url=url, source=source, captured_at=captured_at, raw_payload=raw_payload)
        self.captures.append(event)
        logger.info("[mock-dispatcher] captured url=%s source=%s at=%s", url, source, captured_at.isoformat())


_dispatcher: URLDispatcher = _MockDispatcher()


def register_dispatcher(dispatcher: URLDispatcher) -> None:
    """Component #2 calls this at startup to install the real dispatcher."""
    global _dispatcher
    _dispatcher = dispatcher


def get_dispatcher() -> URLDispatcher:
    return _dispatcher


def dispatch_url(
    url: str,
    source: SourceChannel,
    captured_at: datetime,
    raw_payload: dict[str, Any] | None = None,
) -> None:
    """Public entry point used by all ingest channels."""
    _dispatcher.dispatch_url(url, source, captured_at, raw_payload or {})
