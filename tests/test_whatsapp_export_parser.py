"""Tests for `connecting_dots.parsers.whatsapp_export`.

These are pure-function unit tests — no filesystem, no zip, no daemon.
We feed each test a hand-crafted transcript snippet and assert the
yielded `ParsedMessage`s.

Coverage matrix:
  - iOS line format (12-hour, U+200E mark, [bracket] separator).
  - Android line format (24-hour, dash separator, no mark).
  - DD/MM/YYYY (Indian, dayfirst=True path).
  - MM/DD/YYYY (US, dayfirst=False fallback path).
  - Multi-line message bodies (continuation lines).
  - System / banner lines (encryption notice, deleted messages).
  - Forwarded marker handling.
  - Media attachment markers (iOS + Android variants).
  - Media file-type classification (IMG / VID / PTT / AUD / DOC / STK).
  - URL extraction (link in text → message_type="url").
  - Stickers dropped (parity with live webhook).
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from connecting_dots.parsers.whatsapp_export import (
    ParsedMessage,
    parse_chat_txt,
    resolve_default_tz,
)

IST = ZoneInfo("Asia/Kolkata")
LRM = "‎"


def _parse(text: str, tz=IST) -> list[ParsedMessage]:
    return list(parse_chat_txt(text, default_tz=tz))


# --------------------------------------------------------------------------- #
# Basic line formats
# --------------------------------------------------------------------------- #


def test_ios_url_message_dd_mm_yyyy() -> None:
    text = f"{LRM}[15/01/2026, 10:23:45 AM] Dhiraj Pawar: https://youtube.com/watch?v=abc"
    msgs = _parse(text)
    assert len(msgs) == 1
    m = msgs[0]
    assert m.sender == "Dhiraj Pawar"
    assert m.message_type == "url"
    assert m.url == "https://youtube.com/watch?v=abc"
    # 10:23:45 IST -> 04:53:45 UTC (IST is UTC+5:30).
    assert m.captured_at == datetime(2026, 1, 15, 4, 53, 45, tzinfo=timezone.utc)
    assert m.media_filename is None


def test_android_url_message_24h() -> None:
    text = "15/01/2026, 10:23 - Dhiraj Pawar: https://youtube.com/watch?v=abc"
    msgs = _parse(text)
    assert len(msgs) == 1
    m = msgs[0]
    assert m.sender == "Dhiraj Pawar"
    assert m.message_type == "url"
    assert m.url == "https://youtube.com/watch?v=abc"
    # Android 24h: 10:23 IST -> 04:53 UTC.
    assert m.captured_at == datetime(2026, 1, 15, 4, 53, tzinfo=timezone.utc)


def test_us_locale_mm_dd_yyyy_falls_back_when_dayfirst_fails() -> None:
    """A US-style date like `01/13/2026` is ambiguous under `dayfirst=True`
    (13 isn't a valid month), so the parser must fall back to `dayfirst=False`
    and produce January 13."""
    text = "[01/13/2026, 09:00 AM] Dhiraj: hello"
    msgs = _parse(text)
    assert len(msgs) == 1
    # 09:00 IST -> 03:30 UTC, January 13.
    assert msgs[0].captured_at == datetime(2026, 1, 13, 3, 30, tzinfo=timezone.utc)


def test_ambiguous_date_prefers_dayfirst_indian() -> None:
    """When both interpretations parse, we MUST prefer dayfirst=True
    (Indian locale) — `03/04/2026` is April 3, not March 4."""
    text = "[03/04/2026, 12:00 PM] Dhiraj: noon"
    msgs = _parse(text)
    assert msgs[0].captured_at.month == 4
    assert msgs[0].captured_at.day == 3


def test_plain_text_message() -> None:
    text = "15/01/2026, 10:25 - Dhiraj: thinking about pricing"
    msgs = _parse(text)
    assert msgs[0].message_type == "text"
    assert msgs[0].body == "thinking about pricing"
    assert msgs[0].url is None


# --------------------------------------------------------------------------- #
# Multi-line messages
# --------------------------------------------------------------------------- #


def test_multiline_message_concatenated() -> None:
    text = (
        "15/01/2026, 10:25 - Dhiraj: first line\n"
        "second line\n"
        "third line\n"
        "15/01/2026, 10:30 - Dhiraj: next message"
    )
    msgs = _parse(text)
    assert len(msgs) == 2
    assert msgs[0].body == "first line\nsecond line\nthird line"
    assert msgs[1].body == "next message"


def test_ios_multiline_strips_lrm_per_line() -> None:
    text = (
        f"{LRM}[15/01/2026, 10:25:00 AM] Dhiraj: first\n"
        f"{LRM}continuation\n"
        f"{LRM}[15/01/2026, 10:26:00 AM] Dhiraj: next"
    )
    msgs = _parse(text)
    assert len(msgs) == 2
    assert msgs[0].body == "first\ncontinuation"


# --------------------------------------------------------------------------- #
# System messages — all skipped
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        "Messages and calls are end-to-end encrypted. No one outside of this chat, not even WhatsApp, can read or listen to them. Tap to learn more.",
        "<Media omitted>",
        "This message was deleted",
        "You deleted this message",
        f"{LRM}image omitted",
        "video omitted",
    ],
)
def test_system_lines_are_skipped(body: str) -> None:
    text = f"15/01/2026, 10:25 - Dhiraj: {body}"
    msgs = _parse(text)
    assert msgs == []


def test_encryption_banner_only_is_dropped_but_subsequent_messages_keep_flowing() -> None:
    text = (
        "15/01/2026, 10:00 - Dhiraj: Messages and calls are end-to-end encrypted. Tap to learn more.\n"
        "15/01/2026, 10:01 - Dhiraj: real first message"
    )
    msgs = _parse(text)
    assert len(msgs) == 1
    assert msgs[0].body == "real first message"


# --------------------------------------------------------------------------- #
# Forwarded marker
# --------------------------------------------------------------------------- #


def test_forwarded_prefix_stripped_body_preserved() -> None:
    text = (
        "15/01/2026, 10:25 - Dhiraj: Forwarded\n"
        "the actual content of the message"
    )
    msgs = _parse(text)
    assert len(msgs) == 1
    assert msgs[0].body == "the actual content of the message"


# --------------------------------------------------------------------------- #
# Media attachments
# --------------------------------------------------------------------------- #


def test_ios_image_attachment() -> None:
    text = f"{LRM}[15/01/2026, 10:24:12 AM] Dhiraj: {LRM}<attached: IMG-20260115-WA0001.jpg>"
    msgs = _parse(text)
    assert len(msgs) == 1
    m = msgs[0]
    assert m.message_type == "image"
    assert m.media_filename == "IMG-20260115-WA0001.jpg"
    assert m.body == ""


def test_android_image_attachment() -> None:
    text = "15/01/2026, 10:24 - Dhiraj: IMG-20260115-WA0001.jpg (file attached)"
    msgs = _parse(text)
    assert len(msgs) == 1
    assert msgs[0].message_type == "image"
    assert msgs[0].media_filename == "IMG-20260115-WA0001.jpg"


def test_voice_note_classified_as_audio() -> None:
    """PTT- prefix = push-to-talk voice note — message_type must be 'audio'."""
    text = "15/01/2026, 10:24 - Dhiraj: PTT-20260120-WA0003.opus (file attached)"
    msgs = _parse(text)
    assert msgs[0].message_type == "audio"
    assert msgs[0].media_filename.startswith("PTT-")


def test_video_attachment() -> None:
    text = "15/01/2026, 10:24 - Dhiraj: VID-20260117-WA0002.mp4 (file attached)"
    msgs = _parse(text)
    assert msgs[0].message_type == "video"


def test_document_pdf_attachment() -> None:
    text = "15/01/2026, 10:24 - Dhiraj: DOC-20260121-WA0005.pdf (file attached)"
    msgs = _parse(text)
    assert msgs[0].message_type == "document"


def test_audio_aud_prefix() -> None:
    text = "15/01/2026, 10:24 - Dhiraj: AUD-20260120-WA0004.mp3 (file attached)"
    msgs = _parse(text)
    assert msgs[0].message_type == "audio"


def test_sticker_dropped() -> None:
    """Stickers match the live-webhook rule: pure expression, drop them."""
    text = "15/01/2026, 10:24 - Dhiraj: STK-20260120-WA0006.webp (file attached)"
    msgs = _parse(text)
    assert msgs == []


def test_attachment_with_caption_ios() -> None:
    text = (
        f"{LRM}[15/01/2026, 10:24:12 AM] Dhiraj: {LRM}<attached: IMG-20260115-WA0001.jpg>\n"
        "look at this sunset"
    )
    msgs = _parse(text)
    assert msgs[0].message_type == "image"
    assert msgs[0].body == "look at this sunset"


def test_custom_named_pdf_falls_back_to_extension() -> None:
    """A user-attached file (not following WA's IMG-/DOC- convention) must
    still be classified by extension."""
    text = "15/01/2026, 10:24 - Dhiraj: invoice_jan.pdf (file attached)"
    msgs = _parse(text)
    assert msgs[0].message_type == "document"
    assert msgs[0].media_filename == "invoice_jan.pdf"


# --------------------------------------------------------------------------- #
# URL extraction
# --------------------------------------------------------------------------- #


def test_url_in_middle_of_text_extracted() -> None:
    text = "15/01/2026, 10:25 - Dhiraj: check this https://example.com/x great article"
    msgs = _parse(text)
    assert msgs[0].message_type == "url"
    assert msgs[0].url == "https://example.com/x"
    # Body preserved so the handler can use the surrounding context.
    assert "check this" in msgs[0].body


def test_url_with_trailing_punctuation_trimmed() -> None:
    text = "15/01/2026, 10:25 - Dhiraj: read this: https://example.com/x."
    msgs = _parse(text)
    assert msgs[0].url == "https://example.com/x"


def test_first_url_wins_when_multiple() -> None:
    text = "15/01/2026, 10:25 - Dhiraj: https://a.com and https://b.com"
    msgs = _parse(text)
    assert msgs[0].url == "https://a.com"


def test_http_only_scheme_accepted() -> None:
    text = "15/01/2026, 10:25 - Dhiraj: http://insecure.example.com/x"
    msgs = _parse(text)
    assert msgs[0].url == "http://insecure.example.com/x"


# --------------------------------------------------------------------------- #
# End-to-end mix
# --------------------------------------------------------------------------- #


def test_mixed_transcript() -> None:
    text = (
        f"{LRM}[15/01/2026, 09:00:00 AM] Dhiraj: Messages and calls are end-to-end encrypted. Tap to learn more.\n"
        f"{LRM}[15/01/2026, 09:01:00 AM] Dhiraj: https://youtu.be/abc\n"
        f"{LRM}[15/01/2026, 09:02:00 AM] Dhiraj: {LRM}<attached: IMG-20260115-WA0001.jpg>\n"
        f"caption text\n"
        f"{LRM}[15/01/2026, 09:03:00 AM] Dhiraj: just a random thought\n"
        f"{LRM}[15/01/2026, 09:04:00 AM] Dhiraj: This message was deleted\n"
        f"{LRM}[15/01/2026, 09:05:00 AM] Dhiraj: {LRM}<attached: PTT-20260115-WA0002.opus>\n"
    )
    msgs = _parse(text)
    types = [m.message_type for m in msgs]
    assert types == ["url", "image", "text", "audio"]
    assert msgs[1].body == "caption text"


# --------------------------------------------------------------------------- #
# Edge cases
# --------------------------------------------------------------------------- #


def test_empty_transcript() -> None:
    assert _parse("") == []


def test_garbage_before_first_header_is_dropped() -> None:
    text = (
        "this is junk\n"
        "more junk\n"
        "15/01/2026, 10:00 - Dhiraj: first real message"
    )
    msgs = _parse(text)
    assert len(msgs) == 1
    assert msgs[0].body == "first real message"


def test_unparseable_timestamp_skips_that_message_not_the_batch() -> None:
    text = (
        "99/99/9999, 99:99 - Dhiraj: garbage time\n"
        "15/01/2026, 10:00 - Dhiraj: good message"
    )
    msgs = _parse(text)
    assert len(msgs) == 1
    assert msgs[0].body == "good message"


def test_seconds_optional_in_android_format() -> None:
    """Some Android exports drop the seconds field."""
    text = "15/01/2026, 10:23 - Dhiraj: hello"
    msgs = _parse(text)
    assert len(msgs) == 1


def test_resolve_default_tz_known() -> None:
    assert str(resolve_default_tz("Asia/Kolkata")) == "Asia/Kolkata"


def test_resolve_default_tz_unknown_falls_back() -> None:
    tz = resolve_default_tz("Not/A/Zone")
    # Must be tz-aware (UTC).
    now = datetime(2026, 1, 1, tzinfo=tz)
    assert now.utcoffset().total_seconds() == 0


def test_resolve_default_tz_none_defaults_to_kolkata() -> None:
    assert str(resolve_default_tz(None)) == "Asia/Kolkata"


def test_captured_at_is_always_utc() -> None:
    """The parser always normalises to UTC regardless of input timezone."""
    text = "15/01/2026, 10:23 - Dhiraj: hello"
    msgs = _parse(text, tz=IST)
    assert msgs[0].captured_at.tzinfo == timezone.utc


def test_carriage_returns_handled() -> None:
    """Windows-line-ending exports still parse."""
    text = "15/01/2026, 10:25 - Dhiraj: hello\r\n15/01/2026, 10:26 - Dhiraj: world"
    msgs = _parse(text)
    assert len(msgs) == 2
    assert msgs[0].body == "hello"
    assert msgs[1].body == "world"
