"""Hermetic tests for workers.youtube_transcript_recovery.

All 16 tests use only tmp_path, mocks, and monkeypatching — no real network,
no real YouTube API, no real Azure OpenAI.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import yaml

from workers.youtube_transcript_recovery import (
    _CAPTION_BATCH_SIZE,
    recover,
    triage,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_note(
    tmp_path: Path,
    video_id: str,
    *,
    transcript_unavailable: bool = True,
    transcript_recovered_at: str | None = None,
    truly_no_captions: bool = False,
    extra_meta: dict | None = None,
    body: str = "",
    name: str | None = None,
) -> Path:
    """Create a minimal YouTube note in vault/sources/youtube/."""
    yt_dir = tmp_path / "vault" / "sources" / "youtube"
    yt_dir.mkdir(parents=True, exist_ok=True)
    note_name = name or f"{video_id}.md"
    path = yt_dir / note_name

    raw_meta: dict[str, Any] = {"video_id": video_id}
    if transcript_unavailable:
        raw_meta["transcript_unavailable"] = True
        raw_meta["reason"] = "NoTranscriptFound"
    if transcript_recovered_at:
        raw_meta["transcript_recovered_at"] = transcript_recovered_at
    if truly_no_captions:
        raw_meta["truly_no_captions"] = True
    if extra_meta:
        raw_meta.update(extra_meta)

    fm: dict[str, Any] = {
        "source": "youtube",
        "title": f"Test video {video_id}",
        "raw_meta": raw_meta,
    }
    serialized_fm = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip()
    content = f"---\n{serialized_fm}\n---\n{body}\n"
    path.write_text(content, encoding="utf-8")
    return path


def _build_yt_client(caption_map: dict[str, bool]) -> MagicMock:
    """Build a mock YouTube API client that returns caption info from a dict."""
    client = MagicMock()

    def videos_list_execute(**kwargs):
        id_param = kwargs.get("id", "")
        ids = [i for i in id_param.split(",") if i]
        items = []
        for vid in ids:
            if vid in caption_map:
                items.append({
                    "id": vid,
                    "contentDetails": {
                        "caption": "true" if caption_map[vid] else "false"
                    },
                })
        return {"items": items}

    videos_list_mock = MagicMock()
    videos_list_mock.execute.side_effect = lambda: videos_list_execute(
        id=videos_list_mock._last_id
    )

    def videos_list_call(**kwargs):
        m = MagicMock()
        id_param = kwargs.get("id", "")
        m.execute.return_value = videos_list_execute(id=id_param)
        return m

    client.videos().list.side_effect = videos_list_call
    return client


def _read_fm(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}
    return yaml.safe_load(text[4:end]) or {}


def _read_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    if end == -1:
        return ""
    return text[end + 5:]


def _mock_transcript(snippets=None):
    """Return a mock _fetch_transcript result."""
    if snippets is None:
        snippets = [{"text": "Hello world.", "start": 0.0, "duration": 1.0}]
    return (snippets, "en")


# ---------------------------------------------------------------------------
# Test 1 — Triage partitions caption-true vs caption-false
# ---------------------------------------------------------------------------

def test_triage_partitions_caption_true_and_false(tmp_path):
    vault = tmp_path / "vault"
    vid_true = "captionTRUE1"
    vid_false = "captionFALS1"
    _make_note(tmp_path, vid_true)
    _make_note(tmp_path, vid_false)

    caption_map = {vid_true: True, vid_false: False}
    client = _build_yt_client(caption_map)

    ct, cf = triage(vault, client=client, dry_run=True)

    ct_ids = {vid for _, _, vid in ct}
    cf_ids = {vid for _, _, vid in cf}
    assert vid_true in ct_ids
    assert vid_false in cf_ids
    assert vid_true not in cf_ids
    assert vid_false not in ct_ids


# ---------------------------------------------------------------------------
# Test 2 — Triage batches API calls in chunks of ≤50 IDs
# ---------------------------------------------------------------------------

def test_triage_batches_in_chunks_of_50(tmp_path):
    vault = tmp_path / "vault"
    # 51 notes → should trigger 2 API calls
    vids = [f"batchvid{i:04d}" for i in range(51)]
    for vid in vids:
        _make_note(tmp_path, vid)

    caption_map = {vid: False for vid in vids}
    client = _build_yt_client(caption_map)

    triage(vault, client=client, dry_run=True)

    assert client.videos().list.call_count == 2


# ---------------------------------------------------------------------------
# Test 3 — Recovery writes transcript to note body on success
# ---------------------------------------------------------------------------

def test_recover_writes_transcript_body(tmp_path):
    vid = "bodywrite1234"
    path = _make_note(tmp_path, vid)

    snippets = [{"text": "This is the recovered transcript.", "start": 0.0, "duration": 2.0}]

    with patch("workers.youtube_transcript_recovery._fetch_transcript", return_value=(snippets, "en")), \
         patch("workers.youtube_transcript_recovery._generate_tldr", return_value=None), \
         patch("workers.youtube_transcript_recovery.time.sleep"):
        recover([(path, _read_fm(path), vid)], caption_true=True, dry_run=False)

    body = _read_body(path)
    assert "This is the recovered transcript." in body


# ---------------------------------------------------------------------------
# Test 4 — Recovery stamps transcript_recovered_at and clears transcript_unavailable
# ---------------------------------------------------------------------------

def test_recover_stamps_recovered_at_and_clears_flag(tmp_path):
    vid = "stamptest1234"
    path = _make_note(tmp_path, vid)

    snippets = [{"text": "Some content.", "start": 0.0, "duration": 1.5}]

    with patch("workers.youtube_transcript_recovery._fetch_transcript", return_value=(snippets, "en")), \
         patch("workers.youtube_transcript_recovery._generate_tldr", return_value=None), \
         patch("workers.youtube_transcript_recovery.time.sleep"):
        recover([(path, _read_fm(path), vid)], caption_true=True, dry_run=False)

    fm = _read_fm(path)
    raw_meta = fm.get("raw_meta") or {}
    assert "transcript_recovered_at" in raw_meta
    assert "transcript_unavailable" not in raw_meta


# ---------------------------------------------------------------------------
# Test 5 — time.sleep is called between requests (throttle verified)
# ---------------------------------------------------------------------------

def test_recover_sleeps_between_requests(tmp_path):
    vids = [f"sleeptest{i:04d}" for i in range(3)]
    paths = [_make_note(tmp_path, vid) for vid in vids]
    candidates = [(p, _read_fm(p), v) for p, v in zip(paths, vids)]

    snippets = [{"text": "Text.", "start": 0.0, "duration": 1.0}]

    with patch("workers.youtube_transcript_recovery._fetch_transcript", return_value=(snippets, "en")), \
         patch("workers.youtube_transcript_recovery._generate_tldr", return_value=None), \
         patch("workers.youtube_transcript_recovery.time.sleep") as mock_sleep:
        recover(candidates, caption_true=True, dry_run=False, delay_s=1.0, jitter_s=0.0)

    # One sleep per candidate
    assert mock_sleep.call_count == 3
    for c in mock_sleep.call_args_list:
        assert c.args[0] >= 1.0


# ---------------------------------------------------------------------------
# Test 6 — Repeated NoTranscript on caption-true → exponential backoff, not truly_no_captions
# ---------------------------------------------------------------------------

def test_block_signal_triggers_backoff_not_truly_no_captions(tmp_path):
    from youtube_transcript_api import NoTranscriptFound  # type: ignore[import-not-found]

    vids = [f"blockvid{i:04d}" for i in range(5)]
    paths = [_make_note(tmp_path, vid) for vid in vids]
    candidates = [(p, _read_fm(p), v) for p, v in zip(paths, vids)]

    with patch(
        "workers.youtube_transcript_recovery._fetch_transcript",
        side_effect=NoTranscriptFound("v", [], []),
    ), patch("workers.youtube_transcript_recovery.time.sleep") as mock_sleep:
        recover(candidates, caption_true=True, dry_run=False, delay_s=0.0, jitter_s=0.0)

    # None should be marked truly_no_captions
    for path in paths:
        fm = _read_fm(path)
        raw_meta = fm.get("raw_meta") or {}
        assert "truly_no_captions" not in raw_meta, f"{path.name} was wrongly marked truly_no_captions"

    # Backoff sleep should have been triggered after _BLOCK_THRESHOLD consecutive failures
    # (in addition to the per-request throttle sleeps which are 0.0 here)
    backoff_calls = [c for c in mock_sleep.call_args_list if c.args[0] >= 30.0]
    assert len(backoff_calls) >= 1, "Expected at least one backoff sleep call"


# ---------------------------------------------------------------------------
# Test 7 — Idempotent: skip notes already stamped transcript_recovered_at
# ---------------------------------------------------------------------------

def test_idempotent_skips_already_recovered(tmp_path):
    vid = "alreadydone1"
    path = _make_note(tmp_path, vid, transcript_recovered_at="2025-01-01T00:00:00Z")

    with patch("workers.youtube_transcript_recovery._fetch_transcript") as mock_fetch, \
         patch("workers.youtube_transcript_recovery.time.sleep"):
        recover([(path, _read_fm(path), vid)], caption_true=True, dry_run=False)

    mock_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# Test 8 — Idempotent: skip notes already stamped truly_no_captions
# ---------------------------------------------------------------------------

def test_idempotent_skips_truly_no_captions(tmp_path):
    vid = "trulynocap11"
    path = _make_note(tmp_path, vid, truly_no_captions=True)

    with patch("workers.youtube_transcript_recovery._fetch_transcript") as mock_fetch, \
         patch("workers.youtube_transcript_recovery.time.sleep"):
        recover([(path, _read_fm(path), vid)], caption_true=False, dry_run=False)

    mock_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# Test 9 — caption=false confirmed by fetch → stamp truly_no_captions: true
# ---------------------------------------------------------------------------

def test_caption_false_confirmed_stamps_truly_no_captions(tmp_path):
    from youtube_transcript_api import NoTranscriptFound  # type: ignore[import-not-found]

    vid = "captfalsecfm1"
    path = _make_note(tmp_path, vid)

    with patch(
        "workers.youtube_transcript_recovery._fetch_transcript",
        side_effect=NoTranscriptFound("v", [], []),
    ), patch("workers.youtube_transcript_recovery.time.sleep"):
        recover([(path, _read_fm(path), vid)], caption_true=False, dry_run=False)

    fm = _read_fm(path)
    raw_meta = fm.get("raw_meta") or {}
    assert raw_meta.get("truly_no_captions") is True


# ---------------------------------------------------------------------------
# Test 10 — --dry-run writes nothing
# ---------------------------------------------------------------------------

def test_dry_run_writes_nothing(tmp_path):
    vid = "dryrunvideo1"
    path = _make_note(tmp_path, vid)
    original_text = path.read_text(encoding="utf-8")

    snippets = [{"text": "Should not be written.", "start": 0.0, "duration": 1.0}]

    with patch("workers.youtube_transcript_recovery._fetch_transcript", return_value=(snippets, "en")), \
         patch("workers.youtube_transcript_recovery._generate_tldr", return_value="A tldr."), \
         patch("workers.youtube_transcript_recovery.time.sleep"):
        recovered = recover(
            [(path, _read_fm(path), vid)],
            caption_true=True,
            dry_run=True,
        )

    assert path.read_text(encoding="utf-8") == original_text
    assert recovered == 1  # counts as recovered even in dry-run


# ---------------------------------------------------------------------------
# Test 11 — tldr written to frontmatter after recovery (mock Azure)
# ---------------------------------------------------------------------------

def test_tldr_written_to_frontmatter_after_recovery(tmp_path):
    vid = "tldrvideo1234"
    path = _make_note(tmp_path, vid)

    snippets = [{"text": "Interesting content.", "start": 0.0, "duration": 2.0}]

    mock_azure = MagicMock()
    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=MagicMock(content="This video is about interesting content."))]
    mock_azure.chat.completions.create.return_value = mock_response

    with patch("workers.youtube_transcript_recovery._fetch_transcript", return_value=(snippets, "en")), \
         patch("workers.youtube_transcript_recovery.time.sleep"):
        recover(
            [(path, _read_fm(path), vid)],
            caption_true=True,
            dry_run=False,
            azure_client=mock_azure,
        )

    fm = _read_fm(path)
    assert "tldr" in fm
    assert fm["tldr"] == "This video is about interesting content."
    raw_meta = fm.get("raw_meta") or {}
    assert "tldr_generated_at" in raw_meta


# ---------------------------------------------------------------------------
# Test 12 — --limit N stops after N recoveries
# ---------------------------------------------------------------------------

def test_limit_stops_after_n_recoveries(tmp_path):
    vids = [f"limitvid{i:04d}" for i in range(5)]
    paths = [_make_note(tmp_path, vid) for vid in vids]
    candidates = [(p, _read_fm(p), v) for p, v in zip(paths, vids)]

    snippets = [{"text": "Content.", "start": 0.0, "duration": 1.0}]

    with patch("workers.youtube_transcript_recovery._fetch_transcript", return_value=(snippets, "en")), \
         patch("workers.youtube_transcript_recovery._generate_tldr", return_value=None), \
         patch("workers.youtube_transcript_recovery.time.sleep"):
        n = recover(candidates, caption_true=True, dry_run=False, limit=2)

    assert n == 2


# ---------------------------------------------------------------------------
# Test 13 — Triage --dry-run stamps nothing
# ---------------------------------------------------------------------------

def test_triage_dry_run_stamps_nothing(tmp_path):
    vid = "triagedryrn1"
    path = _make_note(tmp_path, vid)
    original_text = path.read_text(encoding="utf-8")

    caption_map = {vid: True}
    client = _build_yt_client(caption_map)

    triage(tmp_path / "vault", client=client, dry_run=True)

    assert path.read_text(encoding="utf-8") == original_text


# ---------------------------------------------------------------------------
# Test 14 — Block backoff does NOT permanently discard caption-true notes
# ---------------------------------------------------------------------------

def test_block_backoff_does_not_discard_caption_true_notes(tmp_path):
    from youtube_transcript_api import NoTranscriptFound  # type: ignore[import-not-found]

    vids = [f"nodiscard{i:03d}" for i in range(4)]
    paths = [_make_note(tmp_path, vid) for vid in vids]
    candidates = [(p, _read_fm(p), v) for p, v in zip(paths, vids)]

    with patch(
        "workers.youtube_transcript_recovery._fetch_transcript",
        side_effect=NoTranscriptFound("v", [], []),
    ), patch("workers.youtube_transcript_recovery.time.sleep"):
        recover(candidates, caption_true=True, dry_run=False, delay_s=0.0, jitter_s=0.0)

    # All notes must still be in recoverable state — no truly_no_captions, no recovered_at
    for path in paths:
        fm = _read_fm(path)
        raw_meta = fm.get("raw_meta") or {}
        assert "truly_no_captions" not in raw_meta
        assert "transcript_recovered_at" not in raw_meta


# ---------------------------------------------------------------------------
# Test 15 — Batch size exactly ≤50 (51 notes → 2 API calls)
# ---------------------------------------------------------------------------

def test_triage_51_notes_triggers_two_api_calls(tmp_path):
    vault = tmp_path / "vault"
    vids = [f"batch51_{i:04d}" for i in range(51)]
    for vid in vids:
        _make_note(tmp_path, vid)

    call_count = 0
    called_ids: list[list[str]] = []

    def videos_list_call(**kwargs):
        nonlocal call_count
        call_count += 1
        id_param = kwargs.get("id", "")
        ids = [i for i in id_param.split(",") if i]
        called_ids.append(ids)
        m = MagicMock()
        m.execute.return_value = {"items": []}
        return m

    client = MagicMock()
    client.videos().list.side_effect = videos_list_call

    triage(vault, client=client, dry_run=True)

    assert call_count == 2
    # Each batch must be ≤50
    for batch in called_ids:
        assert len(batch) <= _CAPTION_BATCH_SIZE


# ---------------------------------------------------------------------------
# Test 16 — Recovery skips note with no video_id in frontmatter
# ---------------------------------------------------------------------------

def test_recover_skips_note_with_no_video_id(tmp_path):
    # Create a note without video_id in raw_meta
    yt_dir = tmp_path / "vault" / "sources" / "youtube"
    yt_dir.mkdir(parents=True, exist_ok=True)
    path = yt_dir / "no_video_id.md"
    fm_data = {
        "source": "youtube",
        "title": "No ID note",
        "raw_meta": {"transcript_unavailable": True},  # no video_id key
    }
    serialized = yaml.safe_dump(fm_data, sort_keys=False, allow_unicode=True).rstrip()
    path.write_text(f"---\n{serialized}\n---\nsome body\n", encoding="utf-8")

    with patch("workers.youtube_transcript_recovery._fetch_transcript") as mock_fetch, \
         patch("workers.youtube_transcript_recovery.time.sleep"):
        recover(
            [(path, fm_data, "")],   # empty video_id
            caption_true=True,
            dry_run=False,
        )

    mock_fetch.assert_not_called()
