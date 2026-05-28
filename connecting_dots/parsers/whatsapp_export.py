"""Parser for the WhatsApp "Export Chat (with Media)" `_chat.txt` format.

WhatsApp's in-app export feature drops a ZIP that contains:
  - A `*.txt` transcript at the ZIP root (filename varies — sometimes
    `_chat.txt`, sometimes `WhatsApp Chat with Me.txt`, etc.).
  - Media files in the same root, with WA's own naming convention:
    `IMG-YYYYMMDD-WA####.jpg`, `VID-…-WA####.mp4`, `PTT-…-WA####.opus`
    (push-to-talk voice note), `AUD-…-WA####.mp3`, `DOC-…-WA####.pdf`,
    `STK-…-WA####.webp` (stickers — skipped, see live webhook).

The transcript line format varies by platform and locale. Roughly:

  iOS (12-hour, U+200E mark):
    ‎[15/01/2026, 10:23:45 AM] Dhiraj Pawar: https://example.com
    ‎[15/01/2026, 10:24:12 AM] Dhiraj Pawar: ‎<attached: IMG-20260115-WA0001.jpg>

  Android (24-hour, dash separator, no mark):
    15/01/2026, 10:23 - Dhiraj Pawar: https://example.com
    15/01/2026, 10:24 - Dhiraj Pawar: IMG-20260115-WA0001.jpg (file attached)

  US locale uses MM/DD/YYYY instead of DD/MM/YYYY (we prefer DD/MM/YYYY
  since the user is in India, but accept both).

The module exposes a single public entrypoint:

    parse_chat_txt(text, *, default_tz) -> Iterator[ParsedMessage]

`ParsedMessage` is a dataclass with the fields the watcher needs to build
an `InboundEnvelope`. No I/O — the caller is responsible for reading the
file and for resolving `media_filename` to a path in the ZIP.

Skipped lines:
  - WA's encryption banner ("Messages and calls are end-to-end encrypted").
  - "<Media omitted>" placeholder (only present when the user exports
    *without* media — we treat it as a no-op since the export-with-media
    flow gives us the actual file).
  - "This message was deleted" / "You deleted this message".
  - Group-admin events ("You changed the group's icon" etc.) — these
    don't apply to a self-chat but the parser is lenient.

Continuation lines (no leading timestamp) are concatenated onto the
previous message with `\n`.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from typing import Final, Iterable, Iterator
from zoneinfo import ZoneInfo

from dateutil import parser as dateutil_parser

logger = logging.getLogger(__name__)

# U+200E LEFT-TO-RIGHT MARK — iOS sprinkles this on every line and around
# media attachment markers. Strip aggressively before anything else.
LRM = "‎"

# URL regex matches the one in lib/inbound-dispatch.ts and
# workers/mailto_poller.py — keep the three in sync.
_URL_RE: Final = re.compile(r"https?://[^\s<>\"'\])}]+", re.IGNORECASE)

# Line headers:
#   iOS:     `[DD/MM/YYYY, H:MM:SS AM] Sender: body`
#            `[DD/MM/YY, H:MM AM] Sender: body`
#   Android: `DD/MM/YYYY, HH:MM - Sender: body`
#            `DD/MM/YY, H:MM PM - Sender: body`
#
# We split into:
#   group("ts")     -> raw timestamp string
#   group("sender") -> sender display name (may be empty for system messages)
#   group("body")   -> rest of the line (caption, attachment marker, url, …)
#
# Both formats end the sender with `: ` (colon-space). System messages
# (encryption banner, "you deleted this message") don't have a sender —
# they live entirely in the part after the timestamp separator. We let
# those parse with an empty sender and detect them downstream.
_IOS_LINE_RE: Final = re.compile(
    r"^\[(?P<ts>[^\]]+)\]\s(?:(?P<sender>[^:]+?):\s)?(?P<body>.*)$"
)
_ANDROID_LINE_RE: Final = re.compile(
    r"^(?P<ts>\d{1,4}[/\.\-]\d{1,2}[/\.\-]\d{2,4},\s\d{1,2}:\d{2}(?::\d{2})?(?:\s?[AP]M)?)"
    r"\s-\s(?:(?P<sender>[^:]+?):\s)?(?P<body>.*)$"
)

# Attachment markers.
#   iOS:     `‎<attached: IMG-20260115-WA0001.jpg>` (LRM already stripped)
#   Android: `IMG-20260115-WA0001.jpg (file attached)`
_IOS_ATTACH_RE: Final = re.compile(r"^<attached:\s*(?P<name>[^>]+)>\s*(?P<caption>.*)$")
_ANDROID_ATTACH_RE: Final = re.compile(
    r"^(?P<name>\S+\.\S+)\s\(file attached\)\s*(?P<caption>.*)$"
)

# Filename-prefix → message_type. PTT = push-to-talk (voice note).
# Order matters — `PTT-` must beat the generic `AUD-` prefix.
_MEDIA_PREFIX_TYPE: Final[tuple[tuple[str, str], ...]] = (
    ("PTT-", "audio"),
    ("AUD-", "audio"),
    ("IMG-", "image"),
    ("VID-", "video"),
    ("STK-", "sticker"),
    ("DOC-", "document"),
)

# Fallback by extension when the filename doesn't follow the WA convention
# (e.g. user attached a custom-named PDF). Map common extensions → type.
_EXT_TYPE: Final[dict[str, str]] = {
    # images
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image",
    ".webp": "image", ".heic": "image",
    # videos
    ".mp4": "video", ".mov": "video", ".3gp": "video", ".mkv": "video",
    # audio
    ".opus": "audio", ".mp3": "audio", ".m4a": "audio", ".ogg": "audio",
    ".aac": "audio", ".wav": "audio",
    # docs
    ".pdf": "document", ".doc": "document", ".docx": "document",
    ".xls": "document", ".xlsx": "document", ".ppt": "document",
    ".pptx": "document", ".txt": "document", ".csv": "document",
    ".zip": "document",
}

# System / meta lines we silently skip. Matched as case-insensitive
# substrings on the BODY portion (after the timestamp header is stripped).
_SYSTEM_LINE_FRAGMENTS: Final[tuple[str, ...]] = (
    "messages and calls are end-to-end encrypted",
    "<media omitted>",
    "this message was deleted",
    "you deleted this message",
    "missed voice call",
    "missed video call",
    "you changed this group's icon",
    "you changed the subject",
    "waiting for this message",
    # iOS-specific phrasings
    "‎image omitted",
    "‎video omitted",
    "‎audio omitted",
    "‎document omitted",
    "image omitted",
    "video omitted",
    "audio omitted",
    "document omitted",
    "‎sticker omitted",
    "sticker omitted",
)

# "Forwarded" marker — iOS prefixes forwarded messages with a leading
# `‎` + ``Forwarded`` glyph; Android writes it on its own continuation
# line. We strip the literal marker from the body but keep the content.
_FORWARDED_RE: Final = re.compile(r"^Forwarded\s*\n?", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Output type
# --------------------------------------------------------------------------- #


@dataclass
class ParsedMessage:
    """A single message extracted from the chat transcript."""

    sender: str
    captured_at: datetime  # always tz-aware
    body: str  # full text body (multi-line preserved with \n)
    message_type: str  # one of url / text / image / audio / video / document
    url: str | None = None
    media_filename: str | None = None
    original_line: str = ""  # the first (header) line as it appeared
    # Free-form bag for future fields without breaking the constructor.
    extras: dict[str, object] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Timestamp parsing
# --------------------------------------------------------------------------- #


def _parse_timestamp(raw: str, *, default_tz: tzinfo) -> datetime:
    """Parse an iOS or Android timestamp string into a tz-aware datetime.

    Strategy:
      1. Try `dayfirst=True` (Indian default; user is in IN).
      2. On ValueError, retry with `dayfirst=False` (US locale exports).
      3. If both fail, raise — the caller drops the message rather than
         silently misdating it.

    Any naive datetime gets `default_tz` applied (export files don't carry
    timezone metadata; the user's phone clock matched the local zone when
    the chat happened).
    """
    s = raw.strip()
    last_exc: Exception | None = None
    for dayfirst in (True, False):
        try:
            dt = dateutil_parser.parse(s, dayfirst=dayfirst)
            break
        except (ValueError, OverflowError) as exc:
            last_exc = exc
            continue
    else:  # pragma: no cover — unreachable; the break above always exits
        raise ValueError(f"unparseable timestamp: {raw!r}") from last_exc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    # Normalize to UTC for downstream consumers — InboundEnvelope.captured_at
    # is an `AwareDatetime` and the schema is timezone-agnostic, but storing
    # everything in UTC keeps dedupe keys stable across DST shifts.
    return dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# Line classification
# --------------------------------------------------------------------------- #


def _split_header(raw_line: str) -> tuple[str, str, str] | None:
    """If `raw_line` starts a new message, return (ts_str, sender, body).

    Returns None for continuation lines and unparseable garbage. The caller
    treats None as "append to previous message body".
    """
    line = raw_line.lstrip(LRM).rstrip("\r\n")
    if not line:
        return None
    m = _IOS_LINE_RE.match(line)
    if m:
        return m.group("ts"), (m.group("sender") or "").strip(), m.group("body")
    m = _ANDROID_LINE_RE.match(line)
    if m:
        return m.group("ts"), (m.group("sender") or "").strip(), m.group("body")
    return None


def _is_system_message(body: str) -> bool:
    needle = body.lower().strip()
    needle_no_lrm = needle.replace(LRM, "")
    return any(frag in needle or frag in needle_no_lrm for frag in _SYSTEM_LINE_FRAGMENTS)


def _detect_attachment(body: str) -> tuple[str, str] | None:
    """Return (filename, caption) if body is an attachment marker, else None."""
    # Strip the LRM that iOS sprinkles directly before `<attached:`.
    stripped = body.lstrip(LRM)
    m = _IOS_ATTACH_RE.match(stripped)
    if m:
        return m.group("name").strip(), m.group("caption").strip()
    m = _ANDROID_ATTACH_RE.match(stripped)
    if m:
        return m.group("name").strip(), m.group("caption").strip()
    return None


def _classify_media(filename: str) -> str:
    """Return the message_type for a media filename.

    Falls back to `document` for unknown extensions so we never lose the
    attachment — component #5 can decide what to do with it later.
    """
    for prefix, mt in _MEDIA_PREFIX_TYPE:
        if filename.startswith(prefix):
            return mt
    # Extension fallback.
    lower = filename.lower()
    for ext, mt in _EXT_TYPE.items():
        if lower.endswith(ext):
            return mt
    return "document"


def _first_url(text: str) -> str | None:
    if not text:
        return None
    m = _URL_RE.search(text)
    if not m:
        return None
    return m.group(0).rstrip(".,);:!?")


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def resolve_default_tz(tz_name: str | None) -> tzinfo:
    """Resolve a tz name (e.g. ``"Asia/Kolkata"``) to a tzinfo, with a
    UTC fallback on unknown names. Public so the watcher can share it."""
    if not tz_name:
        return ZoneInfo("Asia/Kolkata")
    try:
        return ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 — zoneinfo raises a ZoneInfoNotFoundError
        logger.warning("[whatsapp-export] unknown tz %r — falling back to UTC", tz_name)
        return timezone.utc


def parse_chat_txt(
    text: str,
    *,
    default_tz: tzinfo,
) -> Iterator[ParsedMessage]:
    """Parse a WhatsApp `_chat.txt` body into `ParsedMessage`s.

    `text` is the full file contents (newline-separated). `default_tz` is
    applied to naive timestamps — IST for the user's local export. The
    iterator yields one `ParsedMessage` per non-system, non-deleted line.
    """
    # Buffer for the message currently being assembled. `pending` is
    # `(ts_str, sender, body_lines, original_first_line)` or None.
    pending: tuple[str, str, list[str], str] | None = None

    def _flush() -> Iterator[ParsedMessage]:
        nonlocal pending
        if pending is None:
            return
        ts_str, sender, lines, original = pending
        pending = None
        body = "\n".join(lines).strip()
        # Strip the "Forwarded" prefix if present — keep the content.
        body = _FORWARDED_RE.sub("", body, count=1).strip()

        if _is_system_message(body):
            return
        if not body and not sender:
            return  # garbage line, no signal

        try:
            captured_at = _parse_timestamp(ts_str, default_tz=default_tz)
        except ValueError as exc:
            logger.warning("[whatsapp-export] skipping unparseable ts %r: %s", ts_str, exc)
            return

        # Attachment?
        attach = _detect_attachment(body)
        if attach is not None:
            filename, caption = attach
            mt = _classify_media(filename)
            if mt == "sticker":
                # Mirror the live webhook: stickers are pure expression,
                # zero signal — drop them.
                return
            yield ParsedMessage(
                sender=sender,
                captured_at=captured_at,
                body=caption,
                message_type=mt,
                url=None,
                media_filename=filename,
                original_line=original,
            )
            return

        # Plain text — has a URL?
        url = _first_url(body)
        if url is not None:
            yield ParsedMessage(
                sender=sender,
                captured_at=captured_at,
                body=body,
                message_type="url",
                url=url,
                media_filename=None,
                original_line=original,
            )
            return

        yield ParsedMessage(
            sender=sender,
            captured_at=captured_at,
            body=body,
            message_type="text",
            url=None,
            media_filename=None,
            original_line=original,
        )

    for raw_line in text.splitlines():
        header = _split_header(raw_line)
        if header is None:
            # Continuation of the previous message (or stray garbage before
            # any header — we drop those).
            if pending is not None:
                pending[2].append(raw_line.lstrip(LRM).rstrip("\r\n"))
            continue
        # New message starts here. Flush whatever was pending first.
        yield from _flush()
        ts_str, sender, body = header
        pending = (ts_str, sender, [body], raw_line.rstrip("\r\n"))

    # Final flush.
    yield from _flush()


__all__ = (
    "LRM",
    "ParsedMessage",
    "parse_chat_txt",
    "resolve_default_tz",
)


# Re-exported for the watcher's test suite which wants to iterate text
# lines directly without re-reading file objects.
def parse_lines(
    lines: Iterable[str],
    *,
    default_tz: tzinfo,
) -> Iterator[ParsedMessage]:
    """Convenience wrapper: parse an iterable of already-split lines."""
    yield from parse_chat_txt("\n".join(lines), default_tz=default_tz)
