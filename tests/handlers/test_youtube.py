"""Tests for `connecting_dots.handlers.youtube`.

Network is stubbed everywhere:
    - `_fetch_metadata` is monkeypatched to return a fixed dict
    - `_fetch_transcript` is monkeypatched to return either a fixture or None
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import AnyUrl

from connecting_dots.inbound_envelope import InboundEnvelope, MessageType, Source
from connecting_dots.handlers import youtube as yt
from connecting_dots.handlers.youtube import YouTubeHandler, extract_video_id


# ---------- recorded fixtures ----------

_FIXTURE_SNIPPETS: list[dict[str, Any]] = [
    {"text": "Welcome to the show.", "start": 0.0, "duration": 2.5},
    {"text": "Today we're talking about neural radiance fields.", "start": 2.5, "duration": 3.2},
    {"text": "It's a technique for novel view synthesis.", "start": 5.7, "duration": 2.8},
    {"text": "Let's dive in.", "start": 8.5, "duration": 1.5},
]

_FIXTURE_META: dict[str, Any] = {
    "title": "Neural Radiance Fields Explained",
    "channel": "Two Minute Papers",
    "duration_s": 612,
    "language": "en",
    "view_count": 123456,
    "upload_date": "20240312",
}


def _make_envelope(url: str) -> InboundEnvelope:
    return InboundEnvelope(
        message_id="test-1",
        message_type=MessageType.url,
        url=AnyUrl(url),
        source=Source.whatsapp,
        captured_at=datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc),
        raw_payload={},
    )


# ---------- extract_video_id() / matches() ----------


@pytest.mark.parametrize(
    "url,expected_id",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?t=42", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/abcDEF12345", "abcDEF12345"),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=PL1", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/v/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        # extra query params
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&feature=share", "dQw4w9WgXcQ"),
    ],
)
def test_extract_video_id_accepts_youtube_flavors(url: str, expected_id: str) -> None:
    assert extract_video_id(url) == expected_id


@pytest.mark.parametrize(
    "url",
    [
        "https://vimeo.com/123456789",
        "https://example.com/watch?v=dQw4w9WgXcQ",
        "https://twitter.com/user/status/123",
        "https://www.youtube.com/",  # no video
        "https://www.youtube.com/watch",  # no v param
        "https://www.youtube.com/watch?v=tooShort",  # 8 chars, not 11
        "https://www.youtube.com/results?search_query=cats",
        "not a url at all",
        "",
    ],
)
def test_extract_video_id_rejects_non_youtube(url: str) -> None:
    assert extract_video_id(url) is None


def test_matches_accepts_all_yt_flavors() -> None:
    handler = YouTubeHandler()
    for url in [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/abcDEF12345",
        "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
    ]:
        assert handler.matches(url), f"should match: {url}"


def test_matches_rejects_non_youtube() -> None:
    handler = YouTubeHandler()
    for url in [
        "https://vimeo.com/123456789",
        "https://example.com/random",
        "https://twitter.com/x/status/1",
    ]:
        assert not handler.matches(url), f"should not match: {url}"


# ---------- handle() with stubbed network ----------


def test_handle_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(yt, "_fetch_metadata", lambda _vid: dict(_FIXTURE_META))
    monkeypatch.setattr(
        yt,
        "_fetch_transcript",
        lambda _vid, _langs: (list(_FIXTURE_SNIPPETS), "en"),
    )

    handler = YouTubeHandler()
    envelope = _make_envelope("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    note = handler.handle(envelope)

    assert note.source == "whatsapp"
    assert note.handler == "youtube"
    assert note.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert note.title == "Neural Radiance Fields Explained"
    assert "neural radiance fields" in note.text.lower()
    assert "transcript_unavailable" not in note.raw_meta
    assert note.raw_meta["video_id"] == "dQw4w9WgXcQ"
    assert note.raw_meta["channel"] == "Two Minute Papers"
    assert note.raw_meta["duration_s"] == 612
    assert note.raw_meta["language"] == "en"
    assert note.raw_meta["view_count"] == 123456


def test_handle_missing_transcript_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(yt, "_fetch_metadata", lambda _vid: dict(_FIXTURE_META))
    monkeypatch.setattr(yt, "_fetch_transcript", lambda _vid, _langs: None)

    handler = YouTubeHandler()
    envelope = _make_envelope("https://youtu.be/dQw4w9WgXcQ")
    note = handler.handle(envelope)

    assert note.text == ""
    assert note.raw_meta["transcript_unavailable"] is True
    assert note.raw_meta["reason"] == "NoTranscriptFound"
    # title from metadata still present — handler should degrade transcript only
    assert note.title == "Neural Radiance Fields Explained"


def test_handle_transcripts_disabled_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the transcript API raises (disabled / IP blocked / age restricted),
    the handler swallows it and produces a degraded NoteRecord."""

    class FakeDisabled(Exception):
        pass

    def _raise(_vid: str, _langs: tuple[str, ...]) -> Any:
        raise FakeDisabled("captions are disabled")

    monkeypatch.setattr(yt, "_fetch_metadata", lambda _vid: dict(_FIXTURE_META))
    monkeypatch.setattr(yt, "_fetch_transcript", _raise)

    handler = YouTubeHandler()
    envelope = _make_envelope("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    note = handler.handle(envelope)

    assert note.text == ""
    assert note.raw_meta["transcript_unavailable"] is True
    assert note.raw_meta["reason"] == "FakeDisabled"


def test_handle_missing_metadata_uses_synthetic_title(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(yt, "_fetch_metadata", lambda _vid: {})
    monkeypatch.setattr(
        yt,
        "_fetch_transcript",
        lambda _vid, _langs: (list(_FIXTURE_SNIPPETS), "en"),
    )

    handler = YouTubeHandler()
    envelope = _make_envelope("https://youtu.be/dQw4w9WgXcQ")
    note = handler.handle(envelope)

    assert note.title == "YouTube video dQw4w9WgXcQ"
    assert note.text  # transcript was joined


def test_handle_raises_on_non_youtube_url(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = YouTubeHandler()
    # Bypass URL validation in the envelope by constructing it with a YT URL
    # then mutating — pydantic frozen models block this, so we test the
    # internal guard by calling matches=False then handle on a non-YT URL via
    # a different envelope construction:
    envelope = _make_envelope("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    # patch extract_video_id to simulate the "matches lied" case
    monkeypatch.setattr(yt, "extract_video_id", lambda _u: None)
    with pytest.raises(ValueError, match="non-YouTube URL"):
        handler.handle(envelope)
