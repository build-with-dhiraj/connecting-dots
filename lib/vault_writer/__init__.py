"""Vault writer — the only sanctioned path for creating notes in vault/.

Exposes `write_note(...)`. See `writer.py` for the implementation.
"""
from .writer import write_note, VaultWriteResult

__all__ = ["write_note", "VaultWriteResult"]
