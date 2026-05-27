"""Raw catch-all handler for non-URL WhatsApp envelopes.

Component #5 (PDF + image vision + voice transcription) will fetch the
actual media bytes from Meta's media-download endpoint and enrich these
notes later. For now this handler's job is purely to preserve the
envelope into the vault so nothing is silently dropped: title + (any)
caption-or-body text + a `raw_meta` blob carrying `message_type`,
`media_id`, `mime_type`, and `filename` so the future enrichment worker
has everything it needs to fetch + process.

Routing:
- This handler is NEVER reached via the URL-based `_pick_handler` path
  used by `dispatch_url`. Its `matches()` returns True so it's a valid
  catch-all if it ever IS placed in the URL registry, but the dispatcher
  bypasses URL routing entirely for non-URL message types via
  `dispatch_envelope`.
- The dispatcher imports `RawHandler` (or the module-level `handler`
  instance) directly and calls `handle(envelope)` for any envelope whose
  `message_type` is not `"url"`.

The output `NoteRecord.handler` is `"raw"` so the vault writer can route
these to `vault/inbox/_raw/` (component #5 polls that subdir).
"""
from __future__ import annotations

import logging
from typing import Final

from connecting_dots.inbound_envelope import InboundEnvelope, MessageType
from connecting_dots.types import NoteRecord

logger = logging.getLogger(__name__)

_TITLE_MAX: Final = 60


def _title_from(envelope: InboundEnvelope) -> str:
    """First 60 chars of body text if present, else `WhatsApp <type>`."""
    text = (envelope.text or "").strip()
    if text:
        snippet = text[:_TITLE_MAX]
        # Avoid mid-word truncation when easy.
        if len(text) > _TITLE_MAX and " " in snippet:
            snippet = snippet.rsplit(" ", 1)[0]
        return snippet
    mt = envelope.message_type
    type_label = mt.value if isinstance(mt, MessageType) else str(mt)
    return f"WhatsApp {type_label}"


class RawHandler:
    """Catch-all for non-URL inbound envelopes (text, media, location, …).

    `matches()` returns True unconditionally so this handler is safe to
    place at the bottom of any registry — but in practice the dispatcher
    invokes it directly from `dispatch_envelope` and bypasses URL
    matching entirely for non-URL message types.
    """

    name = "raw"

    def matches(self, url: str) -> bool:  # noqa: ARG002 — protocol signature
        return True

    def handle(self, envelope: InboundEnvelope) -> NoteRecord:
        title = _title_from(envelope)
        text = (envelope.text or "").strip()
        mt = envelope.message_type
        message_type_str = mt.value if isinstance(mt, MessageType) else str(mt)

        raw_meta: dict[str, object] = {
            "message_type": message_type_str,
            "media_id": envelope.media_id,
            "media_mime_type": envelope.media_mime_type,
            "media_filename": envelope.media_filename,
            "raw_payload": envelope.raw_payload,
            # Flag for component #5: this note is awaiting enrichment.
            "pending_enrichment": envelope.media_id is not None,
        }

        # `url` is None for non-URL envelopes; we still pass an empty
        # string to satisfy NoteRecord's str-typed `url` field. The
        # vault writer will treat empty url as "no canonical link".
        url = str(envelope.url) if envelope.url is not None else ""

        return NoteRecord(
            source=envelope.source.value,
            handler=self.name,
            url=url,
            title=title,
            text=text,
            captured_at=envelope.captured_at,
            raw_meta=raw_meta,
        )


# Canonical module-level singleton (matches the convention used by the
# other handlers, e.g. `youtube_handler`, `instagram_handler`).
handler = RawHandler()

__all__ = ("RawHandler", "handler")
