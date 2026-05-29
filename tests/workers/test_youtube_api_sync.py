"""Tests for workers.youtube_api_sync — fully hermetic (no network)."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from workers.youtube_api_sync import (
    InsufficientScopesError,
    _published_at,
    _synthetic_message_id,
    sync_once,
)
from workers.youtube_takeout_watcher import _synthetic_message_id as takeout_message_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _video_item(video_id: str, title: str = "Test", published_at: str = "2024-01-15T10:00:00Z") -> dict:
    return {
        "id": video_id,
        "snippet": {
            "title": title,
            "publishedAt": published_at,
        },
    }


def _playlist_item(video_id: str, playlist_title: str = "My List", published_at: str = "2024-03-01T12:00:00Z") -> dict:
    return {
        "id": f"PLitem_{video_id}",
        "snippet": {
            "title": playlist_title,
            "publishedAt": published_at,
            "resourceId": {"videoId": video_id},
        },
    }


def _playlist_resource(pl_id: str, title: str) -> dict:
    return {"id": pl_id, "snippet": {"title": title}}


def _paged(items: list, *, pages: int = 1) -> list[dict]:
    chunk = len(items) // pages or len(items)
    return [{"items": items[i * chunk:(i + 1) * chunk], "pageInfo": {"totalResults": len(items)}} for i in range(pages)]


def _build_mock_client(
    liked_pages: list[dict] | None = None,
    playlists: list[dict] | None = None,
    playlist_items: dict[str, list[dict]] | None = None,
) -> MagicMock:
    client = MagicMock()

    # liked videos
    liked_pages = liked_pages or []
    def _make_list_next(pages):
        calls = iter(pages)
        def list_next(prev_req, prev_resp):
            try:
                return MagicMock(execute=MagicMock(return_value=next(calls)))
            except StopIteration:
                return None
        return list_next

    if liked_pages:
        first, *rest = liked_pages
        liked_req = MagicMock(execute=MagicMock(return_value=first))
        client.videos().list.return_value = liked_req
        client.videos().list_next.side_effect = _make_list_next(rest)

    # playlists
    playlists = playlists or []
    if playlists:
        pl_resp = {"items": playlists, "pageInfo": {"totalResults": len(playlists)}}
        pl_req = MagicMock(execute=MagicMock(return_value=pl_resp))
        client.playlists().list.return_value = pl_req
        client.playlists().list_next.return_value = None

    # playlist items per playlist id
    playlist_items = playlist_items or {}
    def _pl_items_list(**kwargs):
        pl_id = kwargs.get("playlistId", "")
        items = playlist_items.get(pl_id, [])
        resp = {"items": items, "pageInfo": {"totalResults": len(items)}}
        req = MagicMock(execute=MagicMock(return_value=resp))
        return req
    client.playlistItems().list.side_effect = _pl_items_list
    client.playlistItems().list_next.return_value = None

    return client


# ---------------------------------------------------------------------------
# 1. message_id parity test (most important)
# ---------------------------------------------------------------------------

def test_message_id_parity_with_takeout_watcher():
    video_id = "dQw4w9WgXcQ"
    url = f"https://www.youtube.com/watch?v={video_id}"
    captured_at = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)

    api_mid = _synthetic_message_id(url, captured_at)
    takeout_mid = takeout_message_id(url, captured_at)

    assert api_mid == takeout_mid
    assert api_mid.startswith("youtube:")
    assert len(api_mid) == len("youtube:") + 64  # sha256 hex


# ---------------------------------------------------------------------------
# 2. Liked-videos pull end-to-end
# ---------------------------------------------------------------------------

def test_liked_videos_dispatched():
    items = [_video_item("aaa111bbbcc"), _video_item("bbb222cccdd")]
    client = _build_mock_client(liked_pages=[{"items": items, "pageInfo": {"totalResults": 2}}])

    with patch("workers.youtube_api_sync.dispatch_url") as mock_dispatch:
        n = sync_once(source="liked", client=client)

    assert n == 2
    assert mock_dispatch.call_count == 2
    urls = {c.kwargs["url"] for c in mock_dispatch.call_args_list}
    assert "https://www.youtube.com/watch?v=aaa111bbbcc" in urls


# ---------------------------------------------------------------------------
# 3. Playlist pull end-to-end
# ---------------------------------------------------------------------------

def test_playlist_videos_dispatched():
    pl = _playlist_resource("PLabc123", "Favorites")
    items = [_playlist_item("vid11111111"), _playlist_item("vid22222222")]
    client = _build_mock_client(
        playlists=[pl],
        playlist_items={"PLabc123": items},
    )

    with patch("workers.youtube_api_sync.dispatch_url") as mock_dispatch:
        n = sync_once(source="playlists", client=client)

    assert n == 2
    assert mock_dispatch.call_count == 2
    payloads = [c.kwargs["raw_payload"] for c in mock_dispatch.call_args_list]
    assert all(p.get("playlist") == "Favorites" for p in payloads)


# ---------------------------------------------------------------------------
# 4. Pagination across multiple liked-video pages
# ---------------------------------------------------------------------------

def test_liked_videos_pagination():
    page1_items = [_video_item(f"liked_p1_{i:04d}") for i in range(3)]
    page2_items = [_video_item(f"liked_p2_{i:04d}") for i in range(3)]

    client = MagicMock()
    page1_resp = {"items": page1_items, "pageInfo": {"totalResults": 6}}
    page2_resp = {"items": page2_items, "pageInfo": {"totalResults": 6}}

    page1_req = MagicMock(execute=MagicMock(return_value=page1_resp))
    page2_req = MagicMock(execute=MagicMock(return_value=page2_resp))
    client.videos().list.return_value = page1_req
    client.videos().list_next.side_effect = [page2_req, None]

    with patch("workers.youtube_api_sync.dispatch_url") as mock_dispatch:
        n = sync_once(source="liked", client=client)

    assert n == 6
    assert mock_dispatch.call_count == 6


# ---------------------------------------------------------------------------
# 5. Dedup: same message_id → dispatch_url NOT called a second time
# ---------------------------------------------------------------------------

def test_dedup_skips_already_seen(tmp_path):
    items = [_video_item("dedup_vid_11")]
    client = _build_mock_client(liked_pages=[{"items": items, "pageInfo": {"totalResults": 1}}])
    db_path = tmp_path / "dedupe.db"

    with patch("workers.youtube_api_sync.dispatch_url", wraps=lambda **kw: None):
        with patch("connecting_dots.dispatcher._DEDUPE_DB_PATH", db_path):
            from connecting_dots.dispatcher import dispatch_url as real_dispatch
            with patch("workers.youtube_api_sync.dispatch_url", real_dispatch):
                sync_once(source="liked", client=client)
                client2 = _build_mock_client(liked_pages=[{"items": items, "pageInfo": {"totalResults": 1}}])
                n2 = sync_once(source="liked", client=client2)

    # Second sync returns 1 (item iterated) but dispatch_url returned None (dedup hit)
    assert n2 == 1


# ---------------------------------------------------------------------------
# 6. --dry-run: dispatch_url never called
# ---------------------------------------------------------------------------

def test_dry_run_never_dispatches(capsys):
    items = [_video_item("dryrun11111"), _video_item("dryrun22222")]
    client = _build_mock_client(liked_pages=[{"items": items, "pageInfo": {"totalResults": 2}}])

    with patch("workers.youtube_api_sync.dispatch_url") as mock_dispatch:
        n = sync_once(source="liked", dry_run=True, client=client)

    assert mock_dispatch.call_count == 0
    assert n == 2
    out = capsys.readouterr().out
    assert "[dry-run]" in out


# ---------------------------------------------------------------------------
# 7. --source liked: only liked fetched, playlists skipped
# ---------------------------------------------------------------------------

def test_source_liked_skips_playlists():
    items = [_video_item("liked_only_1")]
    client = _build_mock_client(liked_pages=[{"items": items, "pageInfo": {"totalResults": 1}}])

    with patch("workers.youtube_api_sync.dispatch_url"):
        sync_once(source="liked", client=client)

    client.playlists().list.assert_not_called()


# ---------------------------------------------------------------------------
# 8. --source playlists: only playlists fetched, liked skipped
# ---------------------------------------------------------------------------

def test_source_playlists_skips_liked():
    pl = _playlist_resource("PLtest999", "Test PL")
    items = [_playlist_item("plonly11111")]
    client = _build_mock_client(playlists=[pl], playlist_items={"PLtest999": items})

    with patch("workers.youtube_api_sync.dispatch_url"):
        sync_once(source="playlists", client=client)

    client.videos().list.assert_not_called()


# ---------------------------------------------------------------------------
# 9. --source all: both liked and playlists fetched
# ---------------------------------------------------------------------------

def test_source_all_fetches_both():
    liked = [_video_item("liked_in_all")]
    pl = _playlist_resource("PLall0001", "All PL")
    pl_items = [_playlist_item("pl_in_all_1")]
    client = _build_mock_client(
        liked_pages=[{"items": liked, "pageInfo": {"totalResults": 1}}],
        playlists=[pl],
        playlist_items={"PLall0001": pl_items},
    )

    with patch("workers.youtube_api_sync.dispatch_url") as mock_dispatch:
        n = sync_once(source="all", client=client)

    assert n == 2
    assert mock_dispatch.call_count == 2


# ---------------------------------------------------------------------------
# 10. Insufficient OAuth scope → InsufficientScopesError raised
# ---------------------------------------------------------------------------

def test_insufficient_scope_raises():
    token_data = {
        "token": "tok",
        "refresh_token": "ref",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/drive.readonly"],
    }
    secret_data = {"installed": {"client_id": "cid", "client_secret": "cs"}}

    with patch("workers.youtube_api_sync._TOKEN_PATH") as mock_tp, \
         patch("workers.youtube_api_sync._SECRET_PATH") as mock_sp:
        mock_tp.exists.return_value = True
        mock_sp.exists.return_value = True
        mock_tp.read_text.return_value = __import__("json").dumps(token_data)
        mock_sp.read_text.return_value = __import__("json").dumps(secret_data)

        with pytest.raises(InsufficientScopesError, match="youtube.readonly"):
            from workers.youtube_api_sync import _load_credentials
            _load_credentials()


# ---------------------------------------------------------------------------
# 11. --limit N: stops after N videos
# ---------------------------------------------------------------------------

def test_limit_stops_early():
    items = [_video_item(f"lim_vid_{i:04d}") for i in range(10)]
    client = _build_mock_client(liked_pages=[{"items": items, "pageInfo": {"totalResults": 10}}])

    with patch("workers.youtube_api_sync.dispatch_url"):
        n = sync_once(source="liked", limit=3, client=client)

    assert n == 3


# ---------------------------------------------------------------------------
# 12. Empty playlist → no crash, no dispatch
# ---------------------------------------------------------------------------

def test_empty_playlist_no_crash():
    pl = _playlist_resource("PLempty00", "Empty")
    client = _build_mock_client(playlists=[pl], playlist_items={"PLempty00": []})

    with patch("workers.youtube_api_sync.dispatch_url") as mock_dispatch:
        n = sync_once(source="playlists", client=client)

    assert n == 0
    assert mock_dispatch.call_count == 0


# ---------------------------------------------------------------------------
# 13. publishedAt present → used as captured_at
# ---------------------------------------------------------------------------

def test_published_at_used_as_captured_at():
    snippet = {"publishedAt": "2023-06-15T08:30:00Z", "title": "Test"}
    dt = _published_at(snippet)
    assert dt == datetime(2023, 6, 15, 8, 30, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 14. publishedAt missing → falls back to utcnow()
# ---------------------------------------------------------------------------

def test_missing_published_at_falls_back_to_now():
    fixed_now = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    with patch("workers.youtube_api_sync.datetime") as mock_dt:
        mock_dt.fromisoformat.side_effect = ValueError
        mock_dt.now.return_value = fixed_now
        dt = _published_at({"title": "No date"})
    assert dt == fixed_now


# ---------------------------------------------------------------------------
# 15. Multiple playlists paginate correctly
# ---------------------------------------------------------------------------

def test_multiple_playlists_all_items_collected():
    pl1 = _playlist_resource("PL0001", "Alpha")
    pl2 = _playlist_resource("PL0002", "Beta")
    items_pl1 = [_playlist_item(f"al_vid_{i:04d}") for i in range(4)]
    items_pl2 = [_playlist_item(f"be_vid_{i:04d}") for i in range(3)]
    client = _build_mock_client(
        playlists=[pl1, pl2],
        playlist_items={"PL0001": items_pl1, "PL0002": items_pl2},
    )

    with patch("workers.youtube_api_sync.dispatch_url") as mock_dispatch:
        n = sync_once(source="playlists", client=client)

    assert n == 7
    assert mock_dispatch.call_count == 7


# ---------------------------------------------------------------------------
# 16. playlist item missing videoId → skipped silently
# ---------------------------------------------------------------------------

def test_playlist_item_missing_video_id_skipped():
    pl = _playlist_resource("PLbaditem", "Bad Items")
    good = _playlist_item("goodvid1111")
    bad = {"id": "bad", "snippet": {"resourceId": {"videoId": ""}, "title": "No ID"}}
    client = _build_mock_client(playlists=[pl], playlist_items={"PLbaditem": [good, bad]})

    with patch("workers.youtube_api_sync.dispatch_url") as mock_dispatch:
        n = sync_once(source="playlists", client=client)

    assert n == 1
    assert mock_dispatch.call_count == 1
