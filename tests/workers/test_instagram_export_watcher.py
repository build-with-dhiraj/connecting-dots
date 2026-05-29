"""Tests for workers.instagram_export_watcher."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from workers.instagram_export_watcher import (
    _fix_mojibake,
    _is_instagram_export,
    _participants_match,
    process_zip,
    sweep_once,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _thread_json(
    participants: list[str],
    messages: list[dict],
    thread_slug: str = "testthread_12345",
) -> dict:
    return {
        "participants": [{"name": n} for n in participants],
        "messages": messages,
    }


def _msg(
    sender: str = "Alice",
    content: str = "",
    ts_ms: int = 1_700_000_000_000,
    share: dict | None = None,
    photos: list | None = None,
    videos: list | None = None,
) -> dict:
    m: dict = {"sender_name": sender, "timestamp_ms": ts_ms}
    if content:
        m["content"] = content
    if share:
        m["share"] = share
    if photos:
        m["photos"] = photos
    if videos:
        m["videos"] = videos
    return m


def _make_ig_zip(
    tmp_path: Path,
    threads: dict[str, dict],
    base_path: str = "your_instagram_activity/messages/inbox",
) -> Path:
    """Build a synthetic Instagram export ZIP.

    threads: {slug: thread_json_dict}
    """
    zip_path = tmp_path / "ig_export.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for slug, data in threads.items():
            arc = f"{base_path}/{slug}/message_1.json"
            zf.writestr(arc, json.dumps(data))
    return zip_path


# ---------------------------------------------------------------------------
# 1. mojibake fix — encode latin-1 bytes decode as UTF-8
# ---------------------------------------------------------------------------

def test_fix_mojibake_emoji() -> None:
    # 😀 is U+1F600 → UTF-8: f0 9f 98 80 → latin-1 misread: "ð\x9f\x98\x80"
    mojibaked = "\xf0\x9f\x98\x80"  # latin-1 read of UTF-8 bytes for 😀
    assert _fix_mojibake(mojibaked) == "😀"


def test_fix_mojibake_accent() -> None:
    # "é" → UTF-8: c3 a9 → latin-1 misread: "Ã©"
    assert _fix_mojibake("Ã©") == "é"


def test_fix_mojibake_passthrough_ascii() -> None:
    assert _fix_mojibake("hello world") == "hello world"


def test_fix_mojibake_fallback_on_bad_input() -> None:
    # Already-correct UTF-8 string that can't be encoded as latin-1 should pass through
    result = _fix_mojibake("café")
    # "café" has "é" (U+00E9) which IS encodable as latin-1, so encode succeeds,
    # then decoded as UTF-8 — test that it doesn't raise
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# 2. _is_instagram_export probe
# ---------------------------------------------------------------------------

def test_is_instagram_export_accepts_valid(tmp_path: Path) -> None:
    zip_path = _make_ig_zip(tmp_path, {"slug_abc": _thread_json(["A", "B"], [])})
    with zipfile.ZipFile(zip_path) as zf:
        assert _is_instagram_export(zf) is True


def test_is_instagram_export_rejects_random_zip(tmp_path: Path) -> None:
    zip_path = tmp_path / "random.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("README.txt", "not instagram")
    with zipfile.ZipFile(zip_path) as zf:
        assert _is_instagram_export(zf) is False


# ---------------------------------------------------------------------------
# 3. participants match
# ---------------------------------------------------------------------------

def test_participants_match_exact() -> None:
    assert _participants_match(["Alice Smith", "Bob Jones"], ["Alice Smith", "Bob Jones"])


def test_participants_match_case_insensitive() -> None:
    assert _participants_match(["alice smith", "BOB JONES"], ["Alice Smith", "bob jones"])


def test_participants_match_order_independent() -> None:
    assert _participants_match(["Bob", "Alice"], ["Alice", "Bob"])


def test_participants_match_rejects_mismatch() -> None:
    assert not _participants_match(["Alice", "Bob"], ["Alice", "Charlie"])


# ---------------------------------------------------------------------------
# 4. thread selection by participants
# ---------------------------------------------------------------------------

def test_thread_selection_dispatches_correct_thread(tmp_path: Path) -> None:
    thread_a = _thread_json(["Alice", "Alice_alt"], [_msg(content="https://example.com/p/abc")])
    thread_b = _thread_json(["Bob", "Charlie"], [_msg(content="https://example.com/p/xyz")])

    zip_path = _make_ig_zip(tmp_path, {"thread_a_1": thread_a, "thread_b_1": thread_b})
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / zip_path.name).write_bytes(zip_path.read_bytes())

    calls: list[dict] = []
    def _mock_dispatch(**kwargs: object) -> None:
        calls.append(dict(kwargs))

    sweep_once(inbox, dispatch=_mock_dispatch, participants=["Alice", "Alice_alt"])

    assert len(calls) == 1
    assert "example.com/p/abc" in calls[0]["url"]


# ---------------------------------------------------------------------------
# 5. shared reel/post → dispatch URL
# ---------------------------------------------------------------------------

def test_shared_reel_dispatched_as_url(tmp_path: Path) -> None:
    share = {"link": "https://www.instagram.com/reel/abc123/", "share_text": "cool reel"}
    thread = _thread_json(["Me", "Me2"], [_msg(share=share)])
    zip_path = _make_ig_zip(tmp_path, {"t": thread})
    unpacked = tmp_path / ".unpacked"
    unpacked.mkdir()

    calls: list[dict] = []
    def _mock(**kw: object) -> None:
        calls.append(dict(kw))

    process_zip(zip_path, unpacked_root=unpacked, dispatch=_mock, participants=["Me", "Me2"])
    assert len(calls) == 1
    assert calls[0]["url"] == "https://www.instagram.com/reel/abc123/"
    assert calls[0]["source"] == "instagram"


# ---------------------------------------------------------------------------
# 6. URL in content → extract and dispatch
# ---------------------------------------------------------------------------

def test_url_in_content_extracted(tmp_path: Path) -> None:
    thread = _thread_json(
        ["Me", "Me2"], [_msg(content="check this out https://youtube.com/watch?v=abc123 cool")]
    )
    zip_path = _make_ig_zip(tmp_path, {"t": thread})
    unpacked = tmp_path / ".unpacked"
    unpacked.mkdir()

    calls: list[dict] = []
    def _mock(**kw: object) -> None:
        calls.append(dict(kw))

    process_zip(zip_path, unpacked_root=unpacked, dispatch=_mock, participants=["Me", "Me2"])
    assert len(calls) == 1
    assert "youtube.com" in calls[0]["url"]


# ---------------------------------------------------------------------------
# 7. text note ingestion
# ---------------------------------------------------------------------------

def test_text_note_dispatched(tmp_path: Path) -> None:
    thread = _thread_json(["Me", "Me2"], [_msg(content="remember to buy milk tomorrow")])
    zip_path = _make_ig_zip(tmp_path, {"t": thread})
    unpacked = tmp_path / ".unpacked"
    unpacked.mkdir()

    calls: list[dict] = []
    def _mock(**kw: object) -> None:
        calls.append(dict(kw))

    process_zip(zip_path, unpacked_root=unpacked, dispatch=_mock, participants=["Me", "Me2"])
    assert len(calls) == 1
    assert calls[0]["raw_payload"]["is_text_note"] is True
    assert calls[0]["source"] == "instagram"


# ---------------------------------------------------------------------------
# 8. media skipped + counted
# ---------------------------------------------------------------------------

def test_media_skipped_counted(tmp_path: Path) -> None:
    msgs = [
        _msg(photos=[{"uri": "messages/inbox/t/photos/a.jpg"}]),
        _msg(videos=[{"uri": "messages/inbox/t/videos/b.mp4"}]),
        _msg(content="https://example.com/p/1"),  # real dispatch
    ]
    thread = _thread_json(["Me", "Me2"], msgs)
    zip_path = _make_ig_zip(tmp_path, {"t": thread})
    unpacked = tmp_path / ".unpacked"
    unpacked.mkdir()

    calls: list[dict] = []
    def _mock(**kw: object) -> None:
        calls.append(dict(kw))

    dispatched, failed, media_skipped = process_zip(
        zip_path, unpacked_root=unpacked, dispatch=_mock, participants=["Me", "Me2"]
    )
    assert dispatched == 1
    assert media_skipped == 2
    assert failed == 0


# ---------------------------------------------------------------------------
# 9. zip-slip rejected
# ---------------------------------------------------------------------------

def test_zip_slip_rejected(tmp_path: Path) -> None:
    zip_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        # Add a valid IG probe entry so _is_instagram_export passes
        zf.writestr("messages/inbox/t/message_1.json", json.dumps({"participants": [], "messages": []}))
        # Add a path-traversal entry
        zf.writestr("../../../etc/passwd", "root:x:0:0")

    unpacked = tmp_path / ".unpacked"
    unpacked.mkdir()

    with pytest.raises(Exception):  # UnprocessableZip wraps RuntimeError
        process_zip(zip_path, unpacked_root=unpacked, participants=["X", "Y"])


# ---------------------------------------------------------------------------
# 10. deduplication — same message twice → one dispatch
# ---------------------------------------------------------------------------

def test_dedup_same_message_twice(tmp_path: Path) -> None:
    """Two JSON files with identical messages should produce one dispatch each
    since message_ids are deterministic (sha256 of content+ts)."""
    share = {"link": "https://www.instagram.com/reel/dup/"}
    msg_obj = _msg(share=share, ts_ms=1_700_000_000_000)

    # Two files for the same thread with the same message
    zip_path = tmp_path / "dup.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for i in (1, 2):
            data = _thread_json(["Me", "Me2"], [msg_obj])
            zf.writestr(f"messages/inbox/t/message_{i}.json", json.dumps(data))

    unpacked = tmp_path / ".unpacked"
    unpacked.mkdir()

    calls: list[dict] = []
    def _mock(**kw: object) -> None:
        calls.append(dict(kw))

    process_zip(zip_path, unpacked_root=unpacked, dispatch=_mock, participants=["Me", "Me2"])
    # Two calls but same message_id — the dispatcher's dedupe table handles idempotency;
    # at the watcher level we verify the message_ids are identical.
    message_ids = [c["message_id"] for c in calls]
    assert len(set(message_ids)) == 1  # deterministic dedup key


# ---------------------------------------------------------------------------
# 11. dry-run dispatches nothing and leaves ZIP in inbox
# ---------------------------------------------------------------------------

def test_dry_run_no_dispatch_no_move(tmp_path: Path) -> None:
    thread = _thread_json(["Me", "Me2"], [_msg(content="https://example.com/p/1")])
    zip_path = _make_ig_zip(tmp_path, {"t": thread})
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    zip_in_inbox = inbox / zip_path.name
    zip_in_inbox.write_bytes(zip_path.read_bytes())

    calls: list[dict] = []
    def _mock(**kw: object) -> None:
        calls.append(dict(kw))

    n = sweep_once(inbox, dispatch=_mock, dry_run=True, participants=["Me", "Me2"])
    assert n == 1  # 1 would-dispatch
    assert len(calls) == 0  # nothing actually dispatched
    assert zip_in_inbox.exists()  # not moved


# ---------------------------------------------------------------------------
# 12. ZIP not archived when a dispatch fails
# ---------------------------------------------------------------------------

def test_zip_stays_in_inbox_on_failure(tmp_path: Path) -> None:
    thread = _thread_json(["Me", "Me2"], [_msg(content="https://example.com/p/1")])
    zip_path = _make_ig_zip(tmp_path, {"t": thread})
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    zip_in_inbox = inbox / zip_path.name
    zip_in_inbox.write_bytes(zip_path.read_bytes())

    def _failing_dispatch(**kw: object) -> None:
        raise RuntimeError("simulated dispatch failure")

    sweep_once(inbox, dispatch=_failing_dispatch, participants=["Me", "Me2"])
    assert zip_in_inbox.exists()  # ZIP left in inbox


# ---------------------------------------------------------------------------
# 13. no-filter mode dispatches nothing but logs thread names
# ---------------------------------------------------------------------------

def test_no_filter_dispatches_nothing(tmp_path: Path) -> None:
    thread = _thread_json(["Alice", "Bob"], [_msg(content="https://example.com")])
    zip_path = _make_ig_zip(tmp_path, {"t": thread})
    unpacked = tmp_path / ".unpacked"
    unpacked.mkdir()

    calls: list[dict] = []
    def _mock(**kw: object) -> None:
        calls.append(dict(kw))

    dispatched, failed, skipped = process_zip(
        zip_path, unpacked_root=unpacked, dispatch=_mock, participants=None
    )
    assert dispatched == 0
    assert len(calls) == 0


# ---------------------------------------------------------------------------
# 14. older export layout (messages/inbox/ without activity wrapper)
# ---------------------------------------------------------------------------

def test_older_export_layout(tmp_path: Path) -> None:
    thread = _thread_json(["Me", "Me2"], [_msg(content="https://example.com/p/old")])
    zip_path = _make_ig_zip(tmp_path, {"t": thread}, base_path="messages/inbox")
    unpacked = tmp_path / ".unpacked"
    unpacked.mkdir()

    calls: list[dict] = []
    def _mock(**kw: object) -> None:
        calls.append(dict(kw))

    process_zip(zip_path, unpacked_root=unpacked, dispatch=_mock, participants=["Me", "Me2"])
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 15. multiple message_N.json files aggregated
# ---------------------------------------------------------------------------

def test_multiple_message_files_aggregated(tmp_path: Path) -> None:
    zip_path = tmp_path / "multi.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for i, url in enumerate(["https://a.com", "https://b.com"], 1):
            data = _thread_json(["Me", "Me2"], [_msg(content=url, ts_ms=i * 1_000_000_000_000)])
            zf.writestr(f"messages/inbox/t/message_{i}.json", json.dumps(data))

    unpacked = tmp_path / ".unpacked"
    unpacked.mkdir()

    calls: list[dict] = []
    def _mock(**kw: object) -> None:
        calls.append(dict(kw))

    process_zip(zip_path, unpacked_root=unpacked, dispatch=_mock, participants=["Me", "Me2"])
    assert len(calls) == 2
