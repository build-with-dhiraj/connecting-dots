"""Label reader/writer for digest reactions (thumbs_up / shrug / thumbs_down).

Storage format: data/labels.jsonl, one JSON object per line:
    {"timestamp": "2026-05-29T08:05:00Z", "item_slug": "sources/web/...", "reaction": "thumbs_up", "user": "918595087697"}

Reaction values: "thumbs_up" | "shrug" | "thumbs_down"
Row IDs in WA interactive messages encode: "<item_slug>:<reaction>"
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, NamedTuple, Optional

log = logging.getLogger(__name__)

Reaction = Literal["thumbs_up", "shrug", "thumbs_down"]
VALID_REACTIONS: frozenset[str] = frozenset({"thumbs_up", "shrug", "thumbs_down"})

# WA button ID → reaction mapping
# Row IDs in the interactive list encode "<slug>__<short_reaction>"
# We use two underscores to avoid collision with slugs that contain single underscores.
_SHORT_TO_REACTION: dict[str, Reaction] = {
    "up": "thumbs_up",
    "shrug": "shrug",
    "down": "thumbs_down",
}

_WRITE_LOCK = threading.Lock()


class LabelRow(NamedTuple):
    timestamp: str
    item_slug: str
    reaction: Reaction
    user: str


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

def _default_labels_db() -> Path:
    env = os.environ.get("LABELS_DB")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent.parent / "data" / "labels.jsonl"


# --------------------------------------------------------------------------- #
# Row ID encoding / decoding
# --------------------------------------------------------------------------- #

def encode_row_id(slug: str, short_reaction: str) -> str:
    """Encode a WA interactive list row ID from slug + short reaction key.

    Format: "<slug>__<short_reaction>" (max 200 chars for WA API limit).
    WA row IDs are limited to 200 chars. We truncate the slug if necessary.
    """
    sep = "__"
    max_slug = 200 - len(sep) - len(short_reaction)
    truncated = slug[:max_slug]
    return f"{truncated}{sep}{short_reaction}"


def decode_row_id(row_id: str) -> Optional[tuple[str, Reaction]]:
    """Decode a WA interactive row ID back to (slug, reaction).

    Returns None if the format is invalid.
    """
    sep = "__"
    if sep not in row_id:
        return None
    # Split on last occurrence of __ to handle slugs that contain __
    idx = row_id.rfind(sep)
    slug = row_id[:idx]
    short = row_id[idx + len(sep):]
    reaction = _SHORT_TO_REACTION.get(short)
    if reaction is None:
        return None
    if not slug:
        return None
    return slug, reaction


# --------------------------------------------------------------------------- #
# Write
# --------------------------------------------------------------------------- #

def write_label(
    item_slug: str,
    reaction: Reaction,
    user: str,
    *,
    labels_db: Optional[Path] = None,
    timestamp: Optional[str] = None,
) -> LabelRow:
    """Append a label row to labels.jsonl.

    Args:
        item_slug: Vault-relative path of the labelled note.
        reaction: "thumbs_up" | "shrug" | "thumbs_down".
        user: WhatsApp phone number of the user (e.g. "918595087697").
        labels_db: Override path (for testing).
        timestamp: Override ISO timestamp (for testing).

    Returns:
        The LabelRow that was written.

    Raises:
        ValueError: if reaction is not a valid value.
    """
    if reaction not in VALID_REACTIONS:
        raise ValueError(f"Invalid reaction: {reaction!r}. Must be one of {sorted(VALID_REACTIONS)}")

    ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = LabelRow(timestamp=ts, item_slug=item_slug, reaction=reaction, user=user)
    path = labels_db or _default_labels_db()

    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({
        "timestamp": row.timestamp,
        "item_slug": row.item_slug,
        "reaction": row.reaction,
        "user": row.user,
    }, ensure_ascii=False) + "\n"

    with _WRITE_LOCK:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)

    log.info("Label written: %s → %s (user=%s)", item_slug, reaction, user)
    return row


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #

def read_labels(
    *,
    labels_db: Optional[Path] = None,
    reaction_filter: Optional[Reaction] = None,
    user_filter: Optional[str] = None,
) -> list[LabelRow]:
    """Read all label rows, optionally filtered by reaction and/or user.

    Deduplication: if the same (item_slug, user) pair appears multiple times,
    the LAST entry wins (most recent reaction for a given note).
    """
    path = labels_db or _default_labels_db()
    if not path.exists():
        return []

    # Use dict to deduplicate: (slug, user) → last row
    seen: dict[tuple[str, str], LabelRow] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                reaction = obj.get("reaction", "")
                if reaction not in VALID_REACTIONS:
                    continue
                row = LabelRow(
                    timestamp=str(obj.get("timestamp", "")),
                    item_slug=str(obj.get("item_slug", "")),
                    reaction=reaction,  # type: ignore[arg-type]
                    user=str(obj.get("user", "")),
                )
                seen[(row.item_slug, row.user)] = row
    except OSError as e:
        log.warning("Could not read labels from %s: %s", path, e)
        return []

    rows = list(seen.values())
    if reaction_filter:
        rows = [r for r in rows if r.reaction == reaction_filter]
    if user_filter:
        rows = [r for r in rows if r.user == user_filter]
    return rows


__all__ = [
    "Reaction",
    "VALID_REACTIONS",
    "LabelRow",
    "encode_row_id",
    "decode_row_id",
    "write_label",
    "read_labels",
]
