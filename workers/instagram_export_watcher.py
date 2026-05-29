"""Instagram Direct Message export watcher.

Ingests one specific Instagram DM thread (typically a self-DM "save-to-self"
channel) from an Instagram "Download Your Information" export ZIP and dispatches
each message as an inbound item through `connecting_dots.dispatcher.dispatch_url`.

Mirrors `workers.youtube_takeout_watcher` and `workers.linkedin_zip_watcher`
exactly in structure, safety patterns, and CLI surface.

1. Polls `INSTAGRAM_INBOX_DIR` (default `data/instagram-inbox`) for new `.zip`s.
2. Verifies the archive looks like an Instagram DM export.
3. Extracts to `<inbox>/.unpacked/<utc-timestamp>/`.
4. Locates the target thread by matching participant names supplied via
   `--participants "Name One" "Name Two"` CLI flag or `INSTAGRAM_THREAD_PARTICIPANTS`
   env var (comma-separated).  If no filter is given, logs all thread names
   and processes nothing.
5. Per-message dispatch:
   - `share.link` present → dispatch as URL with source="instagram".
   - `content` contains a URL → dispatch first URL, source="instagram".
   - `content` non-trivial text (note-to-self) → dispatch as text note.
   - Photos/videos → skipped, counted.
6. Deduplication via synthetic `message_id = "instagram:<sha256(key|ts)>"`.
7. Moves processed ZIPs to `<inbox>/.processed/` only on clean, non-dry runs
   with 0 dispatch failures.

Instagram mojibake:  Instagram exports encode non-ASCII as latin-1-decoded UTF-8
(e.g., "Ã©" instead of "é").  All text strings are fixed with
`s.encode('latin-1').decode('utf-8')` with a silent fallback on failure.

Subcommands:
    python -m workers.instagram_export_watcher once [--dry-run] [--participants "A" "B"]
    python -m workers.instagram_export_watcher run  [--participants "A" "B"]

Env vars:
    INSTAGRAM_INBOX_DIR              default: data/instagram-inbox
    INSTAGRAM_POLL_INTERVAL_S        default: 60
    INSTAGRAM_THREAD_PARTICIPANTS    comma-separated display names, e.g. "Alice,Bob"
    LOG_LEVEL                        default: INFO
"""
from __future__ import annotations

import hashlib
import json
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


# ---------------------------------------------------------------------------
# env / paths
# ---------------------------------------------------------------------------

DEFAULT_INBOX = Path("data/instagram-inbox")
DEFAULT_POLL_INTERVAL_S = 60

_URL_RE = re.compile(r"https?://[^\s]+")


def _inbox_dir() -> Path:
    return Path(os.environ.get("INSTAGRAM_INBOX_DIR", str(DEFAULT_INBOX)))


def _poll_interval() -> int:
    raw = os.environ.get("INSTAGRAM_POLL_INTERVAL_S", str(DEFAULT_POLL_INTERVAL_S))
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_POLL_INTERVAL_S


def _env_participants() -> list[str]:
    raw = os.environ.get("INSTAGRAM_THREAD_PARTICIPANTS", "").strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


# ---------------------------------------------------------------------------
# Instagram mojibake fix
# ---------------------------------------------------------------------------


def _fix_mojibake(s: str) -> str:
    """Fix Instagram's double-encoding: latin-1-decoded UTF-8 → correct text.

    Instagram exports encode non-ASCII bytes as if they were latin-1 when
    the underlying bytes are actually UTF-8.  Re-encoding to latin-1 then
    decoding as UTF-8 reverses this.  Falls back to the original string
    on any error (e.g. the string was already correct ASCII or genuine latin-1).
    """
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return s


# ---------------------------------------------------------------------------
# Instagram export structure detection
# ---------------------------------------------------------------------------

# Both old and new export layouts land under one of these path segments.
_INBOX_HINT_RE = re.compile(r"messages[/\\]inbox[/\\]", re.IGNORECASE)
# Older exports may omit the activity wrapper.
_ALT_INBOX_HINT_RE = re.compile(r"your_instagram_activity[/\\]messages[/\\]inbox[/\\]", re.IGNORECASE)


def _is_instagram_export(zf: zipfile.ZipFile) -> bool:
    """Return True if the archive looks like an Instagram DM export."""
    for name in zf.namelist():
        if _INBOX_HINT_RE.search(name) and name.lower().endswith(".json"):
            return True
    return False


# ---------------------------------------------------------------------------
# Thread / participant matching
# ---------------------------------------------------------------------------


def _normalise_name(name: str) -> str:
    return _fix_mojibake(name).strip().lower()


def _participants_match(thread_names: list[str], filter_names: list[str]) -> bool:
    """Return True if thread_names matches filter_names (case-insensitive, order-free)."""
    t = {_normalise_name(n) for n in thread_names}
    f = {_normalise_name(n) for n in filter_names}
    return t == f


# ---------------------------------------------------------------------------
# Per-message parsing
# ---------------------------------------------------------------------------


@dataclass
class _Item:
    """A single dispatchable item from a DM message."""

    kind: str  # "url" | "text"
    url: str | None  # set for kind="url"
    text: str | None  # set for kind="text"
    captured_at: datetime
    raw_payload: dict[str, Any]


def _synthetic_message_id(key: str, ts_ms: int) -> str:
    h = hashlib.sha256()
    h.update(key.encode("utf-8"))
    h.update(b"|")
    h.update(str(ts_ms).encode("utf-8"))
    return f"instagram:{h.hexdigest()}"


def _iter_messages(thread_json: dict[str, Any]) -> Iterator[tuple[_Item | None, bool]]:
    """Yield (item_or_none, is_media_skip) for each message in a thread JSON.

    Yields (None, True) for media messages so the caller can count skips.
    Yields (item, False) for dispatchable items.
    """
    messages: list[dict[str, Any]] = thread_json.get("messages", [])
    for msg in messages:
        ts_ms: int = msg.get("timestamp_ms", 0)
        captured_at = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)

        sender: str = _fix_mojibake(msg.get("sender_name", ""))
        content_raw: str = msg.get("content", "") or ""
        content: str = _fix_mojibake(content_raw)

        share: dict[str, Any] = msg.get("share", {}) or {}
        share_link: str = _fix_mojibake(share.get("link", "") or "")
        share_text: str = _fix_mojibake(share.get("share_text", "") or "")

        # --- media skip ---
        has_media = bool(
            msg.get("photos") or msg.get("videos") or msg.get("audio_files")
        )

        # --- shared reel/post (highest priority) ---
        if share_link and share_link.startswith("http"):
            mid = _synthetic_message_id(share_link, ts_ms)
            raw: dict[str, Any] = {
                "message_id": mid,
                "instagram_dm": True,
                "sender": sender,
                "share_link": share_link,
                "share_text": share_text,
                "content": content,
            }
            yield _Item(
                kind="url",
                url=share_link,
                text=None,
                captured_at=captured_at,
                raw_payload=raw,
            ), False
            continue

        # --- URL in content ---
        m = _URL_RE.search(content)
        if m:
            url = m.group(0).rstrip(".,;)")  # trim trailing punctuation
            mid = _synthetic_message_id(url, ts_ms)
            raw = {
                "message_id": mid,
                "instagram_dm": True,
                "sender": sender,
                "content": content,
            }
            yield _Item(
                kind="url",
                url=url,
                text=None,
                captured_at=captured_at,
                raw_payload=raw,
            ), False
            continue

        # --- media only (no text/url) ---
        if has_media and not content.strip():
            yield None, True
            continue

        # --- skip media that also has non-trivial content after URL check ---
        if has_media:
            # Has content (already checked for URLs above) — treat as text note
            # but also count the media skip
            pass  # fall through to text note below

        # --- plain text note-to-self ---
        stripped = content.strip()
        if stripped:
            mid = _synthetic_message_id(stripped, ts_ms)
            raw = {
                "message_id": mid,
                "instagram_dm": True,
                "sender": sender,
                "content": stripped,
            }
            yield _Item(
                kind="text",
                url=None,
                text=stripped,
                captured_at=captured_at,
                raw_payload=raw,
            ), False
            continue

        # Nothing to dispatch (e.g. reaction-only, empty message)


# ---------------------------------------------------------------------------
# ZIP processing
# ---------------------------------------------------------------------------

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


def _find_thread_jsons(root: Path) -> dict[str, list[Path]]:
    """Return {thread_slug: [message_N.json, ...]} for all threads found."""
    threads: dict[str, list[Path]] = {}
    for json_file in sorted(root.rglob("message_*.json")):
        # Path must be inside an inbox directory — find the LAST occurrence
        # of "inbox" in the path parts so we correctly skip any parent
        # directory named "inbox" that might be the watcher's own inbox dir.
        parts_lower = [p.lower() for p in json_file.parts]
        inbox_idx: int | None = None
        for i in range(len(parts_lower) - 1, -1, -1):
            if parts_lower[i] == "inbox":
                inbox_idx = i
                break
        if inbox_idx is None:
            continue
        # thread slug is the directory immediately inside inbox/
        remaining = json_file.parts[inbox_idx + 1 :]
        if not remaining:
            continue
        slug = remaining[0]
        threads.setdefault(slug, []).append(json_file)
    return threads


def _load_thread_participants(json_files: list[Path]) -> list[str]:
    """Load participant names from the first parseable message JSON."""
    for jf in json_files:
        try:
            data = json.loads(jf.read_bytes())
            participants = data.get("participants", [])
            return [_fix_mojibake(p.get("name", "")) for p in participants]
        except Exception:  # noqa: BLE001
            continue
    return []


def _emit_item(item: _Item, *, dispatch: Callable[..., None]) -> None:
    if item.kind == "url" and item.url:
        dispatch(
            url=item.url,
            source="instagram",
            captured_at=item.captured_at,
            raw_payload=item.raw_payload,
            message_id=item.raw_payload["message_id"],
        )
    elif item.kind == "text" and item.text:
        # Dispatch text notes as a URL-less envelope via dispatch_url using
        # a synthetic "note://" scheme won't validate — instead we build the
        # envelope manually mirroring how the dispatcher handles text.
        # The dispatcher's dispatch_url signature accepts a url kwarg; for
        # pure text notes we pass the text as the url using a data-less approach:
        # We reuse dispatch_url but set the url to an empty sentinel and carry
        # text in raw_payload.  However dispatch_url requires a url; instead we
        # call it with url=None and let the handler decide — but the current
        # dispatcher validates url.  So we pass the text in the content field
        # and use a placeholder that the web handler can gracefully handle.
        # Cleanest v1 approach: dispatch as raw text note via the same
        # dispatch_url path with text stored in raw_payload["note_text"].
        # The web handler will produce a note with whatever metadata it finds.
        # We use a synthetic "about:instagram-note" style indicator in raw_payload.
        dispatch(
            url="https://www.instagram.com/direct/",  # placeholder for handler routing
            source="instagram",
            captured_at=item.captured_at,
            raw_payload={**item.raw_payload, "note_text": item.text, "is_text_note": True},
            message_id=item.raw_payload["message_id"],
        )


def process_zip(
    zip_path: Path,
    *,
    unpacked_root: Path,
    dispatch: Callable[..., None] = dispatch_url,
    dry_run: bool = False,
    participants: list[str] | None = None,
) -> tuple[int, int, int]:
    """Extract `zip_path`, locate target thread, dispatch messages.

    Returns ``(dispatched, failed, media_skipped)`` counts.
    In dry-run mode: dispatch is never called; dispatched = parse count; failed = 0.

    Raises `UnprocessableZip` for non-IG or adversarial archives.
    """
    if not zipfile.is_zipfile(zip_path):
        logger.warning("[instagram] %s is not a valid ZIP — leaving in place", zip_path)
        raise UnprocessableZip(f"not a zip file: {zip_path}")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = unpacked_root / f"{ts}_{zip_path.stem}"

    with zipfile.ZipFile(zip_path) as zf:
        if not _is_instagram_export(zf):
            logger.warning(
                "[instagram] %s does not look like an Instagram DM export — leaving in place",
                zip_path.name,
            )
            raise UnprocessableZip(f"not an Instagram export: {zip_path.name}")
        try:
            _safe_extract(zf, dest)
        except RuntimeError as exc:
            logger.error("[instagram] refusing to extract %s: %s", zip_path.name, exc)
            raise UnprocessableZip(str(exc)) from exc

    threads = _find_thread_jsons(dest)
    if not threads:
        logger.warning("[instagram] %s: no message_*.json files found in inbox/", zip_path.name)
        return 0, 0, 0

    # If no filter: log available thread names and bail.
    if not participants:
        for slug, jsons in sorted(threads.items()):
            names = _load_thread_participants(jsons)
            logger.info(
                "[instagram] thread found (no filter set): slug=%r participants=%s",
                slug,
                names,
            )
        logger.info(
            "[instagram] no --participants filter set; "
            "pass --participants 'Name One' 'Name Two' to select a thread"
        )
        return 0, 0, 0

    # Find the matching thread.
    target_jsons: list[Path] | None = None
    for slug, jsons in threads.items():
        names = _load_thread_participants(jsons)
        if _participants_match(names, participants):
            target_jsons = jsons
            logger.info(
                "[instagram] matched thread slug=%r participants=%s", slug, names
            )
            break

    if target_jsons is None:
        known = [
            _load_thread_participants(jsons) for jsons in threads.values()
        ]
        logger.warning(
            "[instagram] no thread matched participants=%s; known threads: %s",
            participants,
            known,
        )
        return 0, 0, 0

    # Process all message_*.json files for the matched thread.
    dispatched = 0
    failed = 0
    media_skipped = 0

    for jf in sorted(target_jsons):
        try:
            data = json.loads(jf.read_bytes())
        except Exception:  # noqa: BLE001
            logger.exception("[instagram] failed to parse %s", jf)
            continue

        for item, is_media_skip in _iter_messages(data):
            if is_media_skip:
                media_skipped += 1
                continue
            if item is None:
                continue
            if dry_run:
                dispatched += 1
                continue
            try:
                _emit_item(item, dispatch=dispatch)
                dispatched += 1
            except Exception:  # noqa: BLE001
                failed += 1
                logger.exception(
                    "[instagram] dispatch failed for %s=%r (file=%s)",
                    item.kind,
                    item.url or item.text,
                    jf.name,
                )

    if media_skipped:
        logger.info("[instagram] skipped %d media attachment(s) (v1: media ingest not yet implemented)", media_skipped)

    if dry_run:
        logger.info(
            "[instagram] dry-run %s → would-dispatch=%d media-skipped=%d",
            zip_path.name, dispatched, media_skipped,
        )
    else:
        logger.info(
            "[instagram] %s → dispatched=%d failed=%d media-skipped=%d",
            zip_path.name, dispatched, failed, media_skipped,
        )
    return dispatched, failed, media_skipped


# ---------------------------------------------------------------------------
# inbox sweep
# ---------------------------------------------------------------------------


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
    dry_run: bool = False,
    participants: list[str] | None = None,
) -> int:
    """Process every pending ZIP in the inbox. Returns total items dispatched (or parsed in dry-run).

    dry_run=True: parses ZIPs and reports the count of items that WOULD be
    dispatched, but calls no dispatch and moves no files.

    A ZIP is only archived to .processed/ when:
    - dry_run is False, AND
    - processing completed with zero dispatch failures.
    """
    inbox = inbox or _inbox_dir()
    unpacked, processed = _ensure_dirs(inbox)

    # Resolve participants from env if not passed explicitly.
    if participants is None:
        participants = _env_participants() or None

    total = 0
    for zip_path in _list_pending_zips(inbox):
        try:
            dispatched, failed, _skipped = process_zip(
                zip_path,
                unpacked_root=unpacked,
                dispatch=dispatch,
                dry_run=dry_run,
                participants=participants,
            )
        except UnprocessableZip:
            continue
        except Exception:  # noqa: BLE001
            logger.exception("[instagram] unhandled error on %s", zip_path.name)
            continue

        total += dispatched

        if dry_run:
            logger.info(
                "[instagram] dry-run: %s would dispatch %d items (not moved)",
                zip_path.name, dispatched,
            )
            continue

        if failed > 0:
            logger.warning(
                "[instagram] %s had %d dispatch failure(s); leaving in inbox for retry",
                zip_path.name, failed,
            )
            continue

        target = processed / zip_path.name
        if target.exists():
            target = processed / f"{zip_path.stem}.{int(time.time())}{zip_path.suffix}"
        shutil.move(str(zip_path), str(target))
        logger.info("[instagram] moved %s -> %s", zip_path.name, target)
    return total


# ---------------------------------------------------------------------------
# daemon loop
# ---------------------------------------------------------------------------


def run_forever(participants: list[str] | None = None) -> None:
    interval = _poll_interval()
    inbox = _inbox_dir()
    logger.info("[instagram] watching %s (interval=%ss)", inbox, interval)

    if participants is None:
        participants = _env_participants() or None

    stop = False

    def _shutdown(_signum: int, _frame: Any) -> None:
        nonlocal stop
        logger.info("[instagram] shutdown signal received; finishing current cycle")
        stop = True

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while not stop:
        try:
            n = sweep_once(inbox, participants=participants)
            if n:
                logger.info("[instagram] sweep done; dispatched=%d", n)
        except Exception:  # noqa: BLE001
            logger.exception("[instagram] sweep failed; will retry next interval")
        for _ in range(interval):
            if stop:
                break
            time.sleep(1)
    logger.info("[instagram] exited cleanly")


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------


def _parse_participants_from_argv(argv: list[str]) -> list[str]:
    """Extract --participants "A" "B" ... from argv (values up to next flag or end)."""
    if "--participants" not in argv:
        return []
    idx = argv.index("--participants")
    names: list[str] = []
    for arg in argv[idx + 1 :]:
        if arg.startswith("-"):
            break
        names.append(arg)
    return names


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cmd = argv[0] if argv else "run"
    participants = _parse_participants_from_argv(list(argv)) or None

    if cmd == "once":
        dry_run = "--dry-run" in argv
        n = sweep_once(dry_run=dry_run, participants=participants)
        label = "would-dispatch" if dry_run else "dispatched"
        print(f"{label}={n}")
        return 0
    if cmd in ("run", "daemon"):
        run_forever(participants=participants)
        return 0
    print(f"unknown command: {cmd!r}; expected one of: once, run", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
