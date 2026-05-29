"""YouTube Data API v3 sync worker.

Pulls Liked Videos and custom playlists via OAuth and dispatches each
video as a watch URL to the existing youtube handler.

Usage:
    python -m workers.youtube_api_sync once [--dry-run] [--source liked|playlists|all] [--limit N]

Auth:
    ~/.youtube-mcp/client_secret.json  — Desktop OAuth client
    ~/.youtube-mcp/token.json          — stored refresh token (refreshed in place)

Dedup parity:
    message_id is produced by the SAME formula as youtube_takeout_watcher._synthetic_message_id
    so already-ingested takeout videos are skipped via the shared dedupe.db.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from connecting_dots.dispatcher import dispatch_url
from workers.youtube_takeout_watcher import _synthetic_message_id

logger = logging.getLogger(__name__)

_TOKEN_PATH = Path.home() / ".youtube-mcp" / "token.json"
_SECRET_PATH = Path.home() / ".youtube-mcp" / "client_secret.json"
_SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class InsufficientScopesError(RuntimeError):
    """Raised when the stored token lacks the required OAuth scopes."""


def _load_credentials():
    """Load and (if needed) refresh OAuth2 credentials.

    Raises:
        FileNotFoundError: if token.json or client_secret.json are missing.
        InsufficientScopesError: if stored token lacks youtube.readonly scope.
    """
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    if not _TOKEN_PATH.exists():
        raise FileNotFoundError(
            f"Token not found at {_TOKEN_PATH}. "
            "Run the YouTube MCP OAuth flow to generate a token first."
        )
    if not _SECRET_PATH.exists():
        raise FileNotFoundError(
            f"Client secret not found at {_SECRET_PATH}. "
            "Download it from Google Cloud Console (Desktop OAuth app)."
        )

    token_data = json.loads(_TOKEN_PATH.read_text())
    secret_data = json.loads(_SECRET_PATH.read_text())
    installed = secret_data.get("installed") or secret_data.get("web") or {}

    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=token_data.get("client_id") or installed.get("client_id"),
        client_secret=token_data.get("client_secret") or installed.get("client_secret"),
        scopes=token_data.get("scopes"),
    )

    # Verify scope before refreshing so we don't silently return empty results.
    stored_scopes = token_data.get("scopes") or []
    if stored_scopes and not any("youtube" in s for s in stored_scopes):
        raise InsufficientScopesError(
            f"Stored token scopes {stored_scopes!r} do not include youtube.readonly. "
            "Delete ~/.youtube-mcp/token.json and re-authenticate with the "
            "https://www.googleapis.com/auth/youtube.readonly scope."
        )

    if creds.expired and creds.refresh_token:
        logger.info("[youtube-api] refreshing expired token")
        creds.refresh(Request())
        _persist_token(creds, token_data)

    return creds


def _persist_token(creds, original_data: dict) -> None:
    updated = {**original_data, "token": creds.token}
    _TOKEN_PATH.write_text(json.dumps(updated, indent=2))
    logger.info("[youtube-api] token refreshed and written back to %s", _TOKEN_PATH)


def _build_client(creds):
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=creds)


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def _iter_liked_videos(client, limit: int | None = None) -> list[dict[str, Any]]:
    """Return liked videos via videos.list(myRating='like'), stopping at limit."""
    results: list[dict[str, Any]] = []
    request = client.videos().list(
        myRating="like",
        part="snippet",
        maxResults=50,
    )
    page = 1
    while request is not None:
        response = request.execute()
        items = response.get("items", [])
        results.extend(items)
        total = response.get("pageInfo", {}).get("totalResults", "?")
        logger.info("[youtube-api] liked %d/%s (page %d)", len(results), total, page)
        if limit is not None and len(results) >= limit:
            break
        request = client.videos().list_next(request, response)
        page += 1
    return results


def _iter_playlist_videos(client, playlist_id: str, playlist_name: str) -> list[dict[str, Any]]:
    """Return all playlistItems for one playlist."""
    results: list[dict[str, Any]] = []
    request = client.playlistItems().list(
        playlistId=playlist_id,
        part="snippet",
        maxResults=50,
    )
    while request is not None:
        response = request.execute()
        results.extend(response.get("items", []))
        request = client.playlistItems().list_next(request, response)
    logger.info("[youtube-api] playlist %r → %d videos", playlist_name, len(results))
    return results


def _iter_playlists(client) -> list[dict[str, Any]]:
    """Return all user playlists (mine=True)."""
    results: list[dict[str, Any]] = []
    request = client.playlists().list(
        mine=True,
        part="snippet",
        maxResults=50,
    )
    while request is not None:
        response = request.execute()
        results.extend(response.get("items", []))
        request = client.playlists().list_next(request, response)
    return results


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _published_at(snippet: dict[str, Any]) -> datetime:
    raw = snippet.get("publishedAt") or ""
    if raw:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _dispatch_item(
    *,
    video_id: str,
    snippet: dict[str, Any],
    extra_payload: dict[str, Any],
    dry_run: bool,
) -> bool:
    """Build and (unless dry_run) call dispatch_url. Returns True if dispatched."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    captured_at = _published_at(snippet)
    message_id = _synthetic_message_id(url, captured_at)

    if dry_run:
        title = snippet.get("title", url)
        print(f"[dry-run] would-dispatch: {title!r}  ({url})  message_id={message_id}")
        return True

    dispatch_url(
        url=url,
        source="youtube",
        captured_at=captured_at,
        raw_payload={"video_id": video_id, **extra_payload},
        message_id=message_id,
    )
    return True


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def sync_once(
    *,
    source: str = "all",
    dry_run: bool = False,
    limit: int | None = None,
    client=None,
) -> int:
    """Run one full sync. Returns count of dispatched (or would-dispatch) items.

    Args:
        source: "liked", "playlists", or "all".
        dry_run: If True, print what would be dispatched but call nothing.
        limit: Stop after this many videos total (for testing).
        client: Pre-built YouTube API client (injected in tests).
    """
    if client is None:
        creds = _load_credentials()
        client = _build_client(creds)

    total = 0

    if source in ("liked", "all"):
        for item in _iter_liked_videos(client, limit=limit):
            if limit is not None and total >= limit:
                break
            video_id = item.get("id", "")
            if not video_id:
                continue
            snippet = item.get("snippet", {})
            _dispatch_item(
                video_id=video_id,
                snippet=snippet,
                extra_payload={"liked": True},
                dry_run=dry_run,
            )
            total += 1

    if source in ("playlists", "all"):
        for pl in _iter_playlists(client):
            if limit is not None and total >= limit:
                break
            pl_id = pl.get("id", "")
            pl_name = pl.get("snippet", {}).get("title", pl_id)
            for item in _iter_playlist_videos(client, pl_id, pl_name):
                if limit is not None and total >= limit:
                    break
                snippet = item.get("snippet", {})
                resource = snippet.get("resourceId", {})
                video_id = resource.get("videoId", "")
                if not video_id:
                    continue
                _dispatch_item(
                    video_id=video_id,
                    snippet=snippet,
                    extra_payload={"playlist": pl_name},
                    dry_run=dry_run,
                )
                total += 1

    label = "would-dispatch" if dry_run else "dispatched"
    logger.info("[youtube-api] sync complete %s=%d", label, total)
    return total


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not argv or argv[0] != "once":
        print("usage: python -m workers.youtube_api_sync once [--dry-run] [--source liked|playlists|all] [--limit N]", file=sys.stderr)
        return 2

    dry_run = "--dry-run" in argv
    source = "all"
    limit: int | None = None

    for i, arg in enumerate(argv):
        if arg == "--source" and i + 1 < len(argv):
            source = argv[i + 1]
        if arg == "--limit" and i + 1 < len(argv):
            try:
                limit = int(argv[i + 1])
            except ValueError:
                print(f"--limit must be an integer, got {argv[i+1]!r}", file=sys.stderr)
                return 2

    if source not in ("liked", "playlists", "all"):
        print(f"--source must be liked, playlists, or all; got {source!r}", file=sys.stderr)
        return 2

    try:
        n = sync_once(source=source, dry_run=dry_run, limit=limit)
    except (FileNotFoundError, InsufficientScopesError) as exc:
        print(f"AUTH ERROR: {exc}", file=sys.stderr)
        return 1

    label = "would-dispatch" if dry_run else "dispatched"
    print(f"{label}={n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
