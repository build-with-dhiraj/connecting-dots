"""YouTube Takeout ZIP watcher.

Ingests natively-saved YouTube content (Watch Later, Liked videos, custom
playlists) from a Google Takeout export ZIP and dispatches each saved video
as a YouTube watch URL to the existing youtube handler for enrichment.

Mirrors `workers.linkedin_zip_watcher` exactly in structure, safety patterns,
and CLI surface.

1. Polls `YOUTUBE_INBOX_DIR` (default `data/youtube-inbox`) for new `.zip`s.
2. Verifies the archive looks like a YouTube Takeout export.
3. Extracts to `<inbox>/.unpacked/<utc-timestamp>/`.
4. Parses `playlists/*.csv` files (Watch Later, Liked videos, custom).
   Skips `history/watch-history.*` and `subscriptions/subscriptions.csv`.
5. Builds one `dispatch_url` call per video row with a deterministic
   `message_id = "youtube:<sha256(url|captured_at)>"` for idempotency.
6. Moves processed ZIPs to `<inbox>/.processed/`.

Subcommands:
    python -m workers.youtube_takeout_watcher          # daemon mode (default 60s)
    python -m workers.youtube_takeout_watcher once     # process once, exit
    python -m workers.youtube_takeout_watcher run      # alias of daemon mode

Env vars:
    YOUTUBE_INBOX_DIR       default: data/youtube-inbox
    YOUTUBE_POLL_INTERVAL_S default: 60
    LOG_LEVEL               default: INFO
"""
from __future__ import annotations

import csv
import hashlib
import logging
import os
import re
import shutil
import signal
import sys
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from connecting_dots.dispatcher import dispatch_url

logger = logging.getLogger(__name__)


# --- env / paths -------------------------------------------------------------

DEFAULT_INBOX = Path("data/youtube-inbox")
DEFAULT_POLL_INTERVAL_S = 60


def _inbox_dir() -> Path:
    return Path(os.environ.get("YOUTUBE_INBOX_DIR", str(DEFAULT_INBOX)))


def _poll_interval() -> int:
    raw = os.environ.get("YOUTUBE_POLL_INTERVAL_S", str(DEFAULT_POLL_INTERVAL_S))
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_POLL_INTERVAL_S


# --- YouTube Takeout export structure ----------------------------------------

# A YouTube Takeout export contains a top-level folder like:
#   "Takeout/YouTube and YouTube Music/"
# We probe the namelist for this path or for playlists/*.csv entries.

_YT_PATH_HINT = re.compile(r"youtube and youtube music", re.IGNORECASE)

# Basenames to SKIP — these are noise, not intent saves.
_SKIP_BASENAMES: frozenset[str] = frozenset({"watch-history", "watch history", "subscriptions"})

# The playlists subfolder name (case-insensitive matching done at parse time).
_PLAYLISTS_DIR_HINT = "playlists"


def _normalize_filename(name: str) -> str:
    """Lowercase stem for tolerant matching (mirrors LinkedIn watcher)."""
    stem = Path(name).stem
    return stem.lower().strip()


def _is_youtube_export(zf: zipfile.ZipFile) -> bool:
    """Return True if the archive looks like a YouTube Takeout export."""
    names = zf.namelist()
    # Check for the "YouTube and YouTube Music" top-level folder.
    for n in names:
        if _YT_PATH_HINT.search(n):
            return True
    # Fall back: any playlists/*.csv entry.
    for n in names:
        parts = Path(n).parts
        if (
            len(parts) >= 2
            and parts[-2].lower() == _PLAYLISTS_DIR_HINT
            and n.lower().endswith(".csv")
        ):
            return True
    return False


def _is_playlist_csv(name: str) -> bool:
    """True if this archive member is a playlists CSV we should ingest."""
    parts = Path(name).parts
    if not name.lower().endswith(".csv"):
        return False
    # Must be inside a 'playlists' directory (any depth).
    lower_parts = [p.lower() for p in parts]
    if _PLAYLISTS_DIR_HINT not in lower_parts:
        return False
    # Skip noise basenames.
    base = _normalize_filename(parts[-1])
    return base not in _SKIP_BASENAMES


# --- column resolution -------------------------------------------------------

# YouTube playlist CSVs have a video-ID column with varying header names.
# We detect flexibly: pick the column whose normalized header contains
# "video id" or "content id".
_VIDEO_ID_HEADER_RE = re.compile(r"video\s*id|content\s*id", re.IGNORECASE)
_TIMESTAMP_HEADER_RE = re.compile(r"time\s*added|created|date|timestamp", re.IGNORECASE)


def _find_video_id_col(fieldnames: list[str] | None) -> str | None:
    if not fieldnames:
        return None
    for h in fieldnames:
        if _VIDEO_ID_HEADER_RE.search(h):
            return h
    return None


def _find_timestamp_col(fieldnames: list[str] | None) -> str | None:
    if not fieldnames:
        return None
    for h in fieldnames:
        if _TIMESTAMP_HEADER_RE.search(h):
            return h
    return None


# --- timestamp parsing -------------------------------------------------------

_ISO_DT_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _parse_youtube_datetime(value: str) -> datetime:
    """Parse a Takeout timestamp; fall back to now() on garbage."""
    s = (value or "").strip()
    if not s:
        return datetime.now(timezone.utc)
    m = _ISO_DT_RE.match(s)
    if m:
        y, mo, d, h, mi, se = (int(x) for x in m.groups())
        try:
            return datetime(y, mo, d, h, mi, se, tzinfo=timezone.utc)
        except ValueError:
            pass
    m = _ISO_DATE_RE.match(s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        try:
            return datetime(y, mo, d, tzinfo=timezone.utc)
        except ValueError:
            pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    logger.debug("[youtube-takeout] unparseable timestamp %r — falling back to now()", s)
    return datetime.now(timezone.utc)


# --- valid video ID ----------------------------------------------------------

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _is_valid_video_id(vid: str) -> bool:
    return bool(_VIDEO_ID_RE.match(vid))


# --- envelope construction ---------------------------------------------------


def _synthetic_message_id(url: str, captured_at: datetime) -> str:
    """Deterministic id so re-imports collapse via the existing dedupe DB."""
    h = hashlib.sha256()
    h.update(url.encode("utf-8"))
    h.update(b"|")
    h.update(captured_at.isoformat().encode("utf-8"))
    return f"youtube:{h.hexdigest()}"


@dataclass
class _Row:
    url: str
    captured_at: datetime
    raw_payload: dict[str, Any]


def _emit(row: _Row, *, dispatch: Callable[..., None]) -> None:
    dispatch(
        url=row.url,
        source="youtube",
        captured_at=row.captured_at,
        raw_payload=row.raw_payload,
        message_id=row.raw_payload["message_id"],
    )


# --- CSV row iterator --------------------------------------------------------


def _iter_playlist_csv(csv_path: Path, playlist_name: str) -> Iterator[_Row]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        vid_col = _find_video_id_col(reader.fieldnames)
        ts_col = _find_timestamp_col(reader.fieldnames)
        if vid_col is None:
            logger.warning(
                "[youtube-takeout] %s: no video-ID column found (headers=%s) — skipping",
                csv_path.name,
                reader.fieldnames,
            )
            return
        for row in reader:
            vid = (row.get(vid_col, "") or "").strip()
            if not vid or not _is_valid_video_id(vid):
                if vid:
                    logger.debug("[youtube-takeout] skipping invalid video id %r", vid)
                continue
            url = f"https://www.youtube.com/watch?v={vid}"
            ts_raw = (row.get(ts_col, "") or "").strip() if ts_col else ""
            captured_at = _parse_youtube_datetime(ts_raw)
            mid = _synthetic_message_id(url, captured_at)
            yield _Row(
                url=url,
                captured_at=captured_at,
                raw_payload={
                    "message_id": mid,
                    "youtube_takeout": True,
                    "playlist": playlist_name,
                    "video_id": vid,
                    "source_file": csv_path.name,
                },
            )


# --- ZIP processing ----------------------------------------------------------

_MAX_TOTAL_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
_MAX_PER_MEMBER_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
_SYMLINK_MODE_BITS = 0o120000 << 16


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract `zf` to `dest`, refusing unsafe members.

    Refuses path-traversal (zip-slip), symlinks, and zip-bombs.
    Identical safety logic to linkedin_zip_watcher._safe_extract.
    """
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)

    total_size = 0
    for member in zf.infolist():
        if (member.external_attr & _SYMLINK_MODE_BITS) == _SYMLINK_MODE_BITS:
            raise RuntimeError(f"symlink entries are not allowed: {member.filename!r}")

        if member.file_size > _MAX_PER_MEMBER_UNCOMPRESSED_BYTES:
            raise RuntimeError(
                f"member exceeds per-file size cap: {member.filename!r} "
                f"({member.file_size} bytes)"
            )
        total_size += member.file_size
        if total_size > _MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise RuntimeError(
                f"archive exceeds total uncompressed size cap "
                f"({total_size} bytes > {_MAX_TOTAL_UNCOMPRESSED_BYTES})"
            )

        member_path = (dest / member.filename).resolve()
        if not member_path.is_relative_to(dest):
            raise RuntimeError(f"unsafe path in archive: {member.filename!r}")

    zf.extractall(dest)


class UnprocessableZip(Exception):
    """Raised when a ZIP should be left in the inbox for inspection."""


def process_zip(
    zip_path: Path,
    *,
    unpacked_root: Path,
    dispatch: Callable[..., None] = dispatch_url,
) -> int:
    """Extract `zip_path` and dispatch every saved-video row.

    Returns the count of dispatched URLs.
    Raises `UnprocessableZip` for non-Takeout or adversarial archives.
    """
    if not zipfile.is_zipfile(zip_path):
        logger.warning("[youtube-takeout] %s is not a valid ZIP — leaving in place", zip_path)
        raise UnprocessableZip(f"not a zip file: {zip_path}")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = unpacked_root / f"{ts}_{zip_path.stem}"

    with zipfile.ZipFile(zip_path) as zf:
        if not _is_youtube_export(zf):
            logger.warning(
                "[youtube-takeout] %s does not look like a YouTube Takeout export — leaving in place",
                zip_path.name,
            )
            raise UnprocessableZip(f"not a YouTube export: {zip_path.name}")
        try:
            _safe_extract(zf, dest)
        except RuntimeError as exc:
            logger.error("[youtube-takeout] refusing to extract %s: %s", zip_path.name, exc)
            raise UnprocessableZip(str(exc)) from exc

    dispatched = 0
    # Only walk playlists CSVs; skip everything else (history, subscriptions).
    for csv_file in sorted(dest.rglob("*.csv")):
        # Rebuild relative path to check if inside playlists dir.
        rel_parts = csv_file.relative_to(dest).parts
        lower_parts = [p.lower() for p in rel_parts]
        if _PLAYLISTS_DIR_HINT not in lower_parts:
            continue
        base = _normalize_filename(csv_file.name)
        if base in _SKIP_BASENAMES:
            logger.info("[youtube-takeout] skipping %s (noise file)", csv_file.name)
            continue
        playlist_name = csv_file.stem  # e.g. "Watch later", "Liked videos"
        try:
            for row in _iter_playlist_csv(csv_file, playlist_name):
                try:
                    _emit(row, dispatch=dispatch)
                    dispatched += 1
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "[youtube-takeout] dispatch failed for url=%s (file=%s)",
                        row.url,
                        csv_file.name,
                    )
        except Exception:  # noqa: BLE001
            logger.exception("[youtube-takeout] failed parsing %s", csv_file.name)

    logger.info("[youtube-takeout] %s → dispatched=%d", zip_path.name, dispatched)
    return dispatched


# --- inbox sweep -------------------------------------------------------------


def _ensure_dirs(inbox: Path) -> tuple[Path, Path]:
    inbox.mkdir(parents=True, exist_ok=True)
    unpacked = inbox / ".unpacked"
    processed = inbox / ".processed"
    unpacked.mkdir(exist_ok=True)
    processed.mkdir(exist_ok=True)
    return unpacked, processed


def _list_pending_zips(inbox: Path) -> list[Path]:
    out: list[Path] = []
    for entry in os.scandir(inbox):
        if not entry.is_file():
            continue
        name = entry.name
        if name.startswith("."):
            continue
        if not name.lower().endswith(".zip"):
            continue
        out.append(Path(entry.path))
    out.sort()
    return out


def sweep_once(
    inbox: Path | None = None,
    *,
    dispatch: Callable[..., None] = dispatch_url,
) -> int:
    """Process every pending ZIP in the inbox. Returns total URLs dispatched."""
    inbox = inbox or _inbox_dir()
    unpacked, processed = _ensure_dirs(inbox)

    total = 0
    for zip_path in _list_pending_zips(inbox):
        try:
            total += process_zip(zip_path, unpacked_root=unpacked, dispatch=dispatch)
        except UnprocessableZip:
            continue
        except Exception:  # noqa: BLE001
            logger.exception("[youtube-takeout] unhandled error on %s", zip_path.name)
            continue
        target = processed / zip_path.name
        if target.exists():
            target = processed / f"{zip_path.stem}.{int(time.time())}{zip_path.suffix}"
        shutil.move(str(zip_path), str(target))
        logger.info("[youtube-takeout] moved %s -> %s", zip_path.name, target)
    return total


# --- daemon loop -------------------------------------------------------------


def run_forever() -> None:
    interval = _poll_interval()
    inbox = _inbox_dir()
    logger.info("[youtube-takeout] watching %s (interval=%ss)", inbox, interval)

    stop = False

    def _shutdown(_signum: int, _frame: Any) -> None:
        nonlocal stop
        logger.info("[youtube-takeout] shutdown signal received; finishing current cycle")
        stop = True

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while not stop:
        try:
            n = sweep_once(inbox)
            if n:
                logger.info("[youtube-takeout] sweep done; dispatched=%d", n)
        except Exception:  # noqa: BLE001
            logger.exception("[youtube-takeout] sweep failed; will retry next interval")
        for _ in range(interval):
            if stop:
                break
            time.sleep(1)
    logger.info("[youtube-takeout] exited cleanly")


# --- entrypoint --------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cmd = argv[0] if argv else "run"
    if cmd == "once":
        n = sweep_once()
        print(f"dispatched={n}")
        return 0
    if cmd in ("run", "daemon"):
        run_forever()
        return 0
    print(f"unknown command: {cmd!r}; expected one of: once, run", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
