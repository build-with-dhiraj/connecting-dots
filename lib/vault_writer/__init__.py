"""Vault writer — the only sanctioned path for creating notes in vault/.

Exposes `write_note(...)` and `stable_id(...)`. See `writer.py` for the
implementation. Routing is driven by `handler` (content-type), not `source`
(ingest channel) — see `writer._route_subdir`.
"""
from .writer import stable_id, write_note

__all__ = ["write_note", "stable_id"]
