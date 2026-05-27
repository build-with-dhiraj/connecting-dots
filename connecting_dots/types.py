"""Shared dataclasses used across the dispatcher and handlers.

Kept deliberately minimal: a single `NoteRecord` shape that every handler
produces and that the vault writer consumes. NER (component #8) populates
`entities` / `topics` later; handlers leave them empty.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class NoteRecord:
    """Canonical output of every handler. The dispatcher hands this to
    `lib.vault_writer.write_note()`.

    Fields mirror the vault frontmatter shape so the writer can serialize
    without lossy translation. `entities` and `topics` are populated later
    by the NER pipeline (component #8); handlers should leave them empty.
    """

    source: str  # mirrors `InboundEnvelope.source` (whatsapp/mailto/linkedin/manual)
    handler: str  # which handler produced it: youtube/instagram/web/linkedin/failed
    url: str
    title: str
    text: str  # main extracted body — transcript / OG description / post body
    captured_at: datetime
    entities: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    raw_meta: dict[str, Any] = field(default_factory=dict)
