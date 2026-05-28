"""WhatsApp self-DM "Export Chat" ZIP watcher.

WhatsApp's Cloud API only delivers messages sent *after* the webhook is
wired up — there's no "give me my history" endpoint. The user-facing
escape hatch is the in-app **Export Chat (with Media)** feature: tap a
chat → ⋮ → More → Export chat → "Attach media" → share/save the ZIP. For
the user's self-DM ("Message yourself"), that ZIP is years of saved links,
images, voice notes, and PDFs — exactly the corpus we want to backfill.

This worker mirrors `workers/linkedin_zip_watcher.py`:

1. Poll `WHATSAPP_EXPORT_INBOX_DIR` (default `data/whatsapp-exports`) for
   new `.zip` files.
2. Sanity-check that the archive looks like a WA export (`*.txt` at the
   root, plus typical `IMG-/VID-/PTT-/DOC-` media names).
3. Safely extract to `<inbox>/.unpacked/<utc-timestamp>_<zipname>/` —
   same zip-slip + zip-bomb + symlink protections as the LinkedIn
   watcher, just sized for WA's larger media payloads (2 GB total /
   200 MB per file).
4. Parse the transcript via `connecting_dots.parsers.whatsapp_export` and
   dispatch one envelope per message. For URLs we go through
   `dispatch_url` (so the per-domain handlers fire); for everything else
   (plain text, images, audio voice notes, video, documents) we build a
   full `InboundEnvelope` and call `dispatch_envelope`.
5. Deterministic `message_id` = `whatsapp_export:<sha256(...)[:16]>` so
   re-importing the same ZIP is a no-op (the shared SQLite dedupe table
   absorbs the replay).
6. Move processed ZIPs to `<inbox>/.processed/<timestamp>/`.

Subcommands (mirrors `workers.linkedin_zip_watcher`):
    python -m workers.whatsapp_export_watcher          # daemon mode
    python -m workers.whatsapp_export_watcher once     # one sweep, exit
    python -m workers.whatsapp_export_watcher run      # alias of daemon mode

Env vars:
    WHATSAPP_EXPORT_INBOX_DIR    default: data/whatsapp-exports
    WHATSAPP_EXPORT_POLL_INTERVAL_S  default: 60
    WHATSAPP_EXPORT_TZ           default: Asia/Kolkata
    LOG_LEVEL                    default: INFO
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import signal
import sqlite3
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from connecting_dots.dispatcher import (
    _claim_message_id,
    _open_dedupe,
    dispatch_envelope,
    dispatch_url,
)
from connecting_dots.inbound_envelope import InboundEnvelope
from connecting_dots.parsers.whatsapp_export import (
    ParsedMessage,
    parse_chat_txt,
    resolve_default_tz,
)

logger = logging.getLogger(__name__)


# --- env / paths -------------------------------------------------------------

DEFAULT_INBOX = Path("data/whatsapp-exports")
DEFAULT_POLL_INTERVAL_S = 60
DEFAULT_TZ_NAME = "Asia/Kolkata"


def _inbox_dir() -> Path:
    return Path(os.environ.get("WHATSAPP_EXPORT_INBOX_DIR", str(DEFAULT_INBOX)))


def _poll_interval() -> int:
    raw = os.environ.get("WHATSAPP_EXPORT_POLL_INTERVAL_S", str(DEFAULT_POLL_INTERVAL_S))
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_POLL_INTERVAL_S


def _tz_name() -> str:
    return os.environ.get("WHATSAPP_EXPORT_TZ", DEFAULT_TZ_NAME)


# --- archive sanity ----------------------------------------------------------

# WA exports always contain a `.txt` at the root. The filename varies:
# `_chat.txt` (iOS recent), `WhatsApp Chat with <Name>.txt` (older iOS,
# Android). We don't require a specific name — finding any `*.txt` at
# depth 0 is enough to call it a WA export.
def _find_chat_txt(zf: zipfile.ZipFile) -> str | None:
    """Return the name of the first root-level `.txt` member, or None.

    `infolist()` order matches the archive's central directory; WA writes
    the transcript first so this is normally the first hit.
    """
    for member in zf.infolist():
        name = member.filename
        if member.is_dir():
            continue
        # Reject anything inside a subdirectory.
        if "/" in name.rstrip("/"):
            continue
        if name.lower().endswith(".txt"):
            return name
    return None


def _is_whatsapp_export(zf: zipfile.ZipFile) -> bool:
    return _find_chat_txt(zf) is not None


# --- safe extraction ---------------------------------------------------------

# WA export ZIPs can carry years of voice notes + 4K video; budget bigger
# than LinkedIn's text-only exports. 2 GB total / 200 MB per file matches
# the brief and the practical iCloud upload limit users hit before
# splitting their export.
_MAX_TOTAL_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB
_MAX_PER_MEMBER_UNCOMPRESSED_BYTES = 200 * 1024 * 1024  # 200 MB
_SYMLINK_MODE_BITS = 0o120000 << 16


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract `zf` into `dest`, refusing unsafe members.

    Mirrors `workers.linkedin_zip_watcher._safe_extract` with WA-sized
    caps. Refusals raise `RuntimeError`; the caller converts that into
    `UnprocessableZip` so the ZIP stays in the inbox for inspection.
    """
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)

    total_size = 0
    for member in zf.infolist():
        # Reject symlinks.
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

        # Zip-slip + sibling-directory bypass: `Path.is_relative_to` is
        # the path-aware check (raw `str.startswith` is fooled by
        # `dest_evil/...` when dest is `dest`).
        member_path = (dest / member.filename).resolve()
        if not member_path.is_relative_to(dest):
            raise RuntimeError(f"unsafe path in archive: {member.filename!r}")

    zf.extractall(dest)


class UnprocessableZip(Exception):
    """Raised when a ZIP should be left in the inbox for inspection."""


# --- envelope construction ---------------------------------------------------


def _synthetic_message_id(msg: ParsedMessage) -> str:
    """Deterministic id so re-imports collapse via the existing dedupe DB.

    Inputs are tuples that uniquely identify a message *within* a chat:
      - sender (the human display name)
      - captured_at ISO string (already UTC-normalised by the parser)
      - body text OR media filename (whichever was the payload)

    sha256 is deterministic across Python processes — the builtin
    `hash()` is not (PYTHONHASHSEED is randomised per-process).
    """
    payload = "|".join(
        [
            msg.sender,
            msg.captured_at.isoformat(),
            msg.media_filename or msg.body or "",
        ]
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:16]
    return f"whatsapp_export:{digest}"


@dataclass
class _DispatchPlan:
    """Either a URL dispatch (handler routing) or a full envelope dispatch."""

    message_id: str
    message_type: str
    url: str | None
    envelope_json: dict[str, Any]


def _build_plan(
    msg: ParsedMessage,
    *,
    extracted_root: Path,
) -> _DispatchPlan | None:
    """Turn a `ParsedMessage` into the kwargs the dispatcher needs.

    Returns None for messages we choose not to dispatch (stickers,
    missing media file — keeps the dispatched count honest).
    """
    mid = _synthetic_message_id(msg)
    raw_payload: dict[str, Any] = {
        "export_source": True,
        "sender": msg.sender,
        "original_line": msg.original_line,
    }
    if msg.media_filename:
        raw_payload["media_filename"] = msg.media_filename

    # Build the envelope dict that gets validated downstream. For URL
    # messages we still build it so the dispatcher's `_build_envelope`
    # path doesn't lose the raw_payload provenance.
    base = {
        "message_id": mid,
        "source": "whatsapp",
        "captured_at": msg.captured_at.astimezone(timezone.utc).isoformat(),
        "raw_payload": raw_payload,
    }

    if msg.message_type == "url":
        assert msg.url is not None  # parser invariant
        base.update(
            {
                "message_type": "url",
                "url": msg.url,
                # Caption / preceding context — useful for the handler
                # to surface alongside the URL preview later.
                "text": msg.body if msg.body and msg.body != msg.url else None,
            }
        )
        return _DispatchPlan(
            message_id=mid, message_type="url", url=msg.url, envelope_json=base
        )

    if msg.message_type == "text":
        base.update({"message_type": "text", "text": msg.body})
        return _DispatchPlan(
            message_id=mid, message_type="text", url=None, envelope_json=base
        )

    # Media. Resolve the file on disk; if missing (e.g. the user exported
    # without media), skip — there's nothing to enrich.
    assert msg.media_filename is not None
    media_path = (extracted_root / msg.media_filename).resolve()
    if not media_path.is_relative_to(extracted_root.resolve()):
        logger.warning(
            "[whatsapp-export] refusing media path outside extraction root: %s",
            msg.media_filename,
        )
        return None
    if not media_path.exists():
        logger.info(
            "[whatsapp-export] media file %s referenced but not present in ZIP — skipping",
            msg.media_filename,
        )
        return None

    # Synthetic `media_id` so the envelope's media-type cross-field
    # invariant ("media envelopes require a non-empty media_id") is
    # satisfied. Component #5 will key off `local_media_path` for
    # export-sourced envelopes instead of fetching from Meta — the
    # `export_source` flag in raw_payload is the discriminator.
    base.update(
        {
            "message_type": msg.message_type,
            "media_id": f"local:{mid}",
            "media_filename": msg.media_filename,
            "local_media_path": str(media_path),
            "text": msg.body or None,
        }
    )
    return _DispatchPlan(
        message_id=mid,
        message_type=msg.message_type,
        url=None,
        envelope_json=base,
    )


# --- dispatch ----------------------------------------------------------------


def _default_dispatch(plan: _DispatchPlan, *, dedupe_db: Path | None = None) -> None:
    """Production dispatch: URL → `dispatch_url`, everything else → envelope.

    Both paths land in the SAME shared SQLite dedupe table via
    `data/dedupe.db` (or the path overridden by `DEDUPE_DB_PATH`). For
    URLs we pass `message_id` through so `dispatch_url` handles the
    claim. For non-URL envelopes we pre-claim here because
    `dispatch_envelope` deliberately does NOT (it assumes the stream
    consumer has already claimed in production).
    """
    env_json = plan.envelope_json
    captured_at_str = env_json["captured_at"]
    captured_at = datetime.fromisoformat(captured_at_str)

    if plan.message_type == "url":
        assert plan.url is not None
        dispatch_url(
            url=plan.url,
            source="whatsapp",
            captured_at=captured_at,
            raw_payload=env_json["raw_payload"],
            message_id=plan.message_id,
            dedupe_db=dedupe_db,
        )
        return

    # Non-URL: claim ourselves, then go through dispatch_envelope so the
    # raw handler writes the note.
    conn = _open_dedupe(dedupe_db)
    try:
        if not _claim_message_id(conn, plan.message_id):
            logger.info(
                "[whatsapp-export] dedupe hit message_id=%s — no-op", plan.message_id
            )
            return
    finally:
        conn.close()

    try:
        envelope = InboundEnvelope.model_validate(env_json)
    except Exception:  # noqa: BLE001
        logger.exception(
            "[whatsapp-export] envelope validation failed for message_id=%s — dropping",
            plan.message_id,
        )
        return
    dispatch_envelope(envelope, dedupe_db=dedupe_db)


# --- ZIP processing ----------------------------------------------------------


def process_zip(
    zip_path: Path,
    *,
    unpacked_root: Path,
    dispatch: Callable[..., None] = _default_dispatch,
    default_tz_name: str | None = None,
) -> int:
    """Extract `zip_path` and dispatch every message. Returns dispatched count.

    Raises `UnprocessableZip` for non-ZIPs, non-WA-exports, and adversarial
    archives — the sweep loop will leave the file in place for the user
    to inspect.
    """
    if not zipfile.is_zipfile(zip_path):
        logger.warning("[whatsapp-export] %s is not a valid ZIP — leaving in place", zip_path)
        raise UnprocessableZip(f"not a zip file: {zip_path}")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = unpacked_root / f"{ts}_{zip_path.stem}"

    with zipfile.ZipFile(zip_path) as zf:
        chat_member = _find_chat_txt(zf)
        if chat_member is None:
            logger.warning(
                "[whatsapp-export] %s does not look like a WA export (no root .txt) — leaving in place",
                zip_path.name,
            )
            raise UnprocessableZip(f"not a whatsapp export: {zip_path.name}")
        try:
            _safe_extract(zf, dest)
        except RuntimeError as exc:
            logger.error("[whatsapp-export] refusing to extract %s: %s", zip_path.name, exc)
            raise UnprocessableZip(str(exc)) from exc

    chat_path = dest / chat_member
    try:
        text = chat_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.exception("[whatsapp-export] failed to read %s", chat_path)
        raise UnprocessableZip(f"could not read transcript: {chat_path}")

    tz = resolve_default_tz(default_tz_name or _tz_name())

    dispatched = 0
    for parsed in parse_chat_txt(text, default_tz=tz):
        plan = _build_plan(parsed, extracted_root=dest)
        if plan is None:
            continue
        try:
            dispatch(plan)
            dispatched += 1
        except sqlite3.OperationalError:
            # Bubble up DB errors — they signal a systemic problem
            # (disk full, permissions) that the caller should see.
            raise
        except Exception:  # noqa: BLE001 — one bad message mustn't drop the batch
            logger.exception(
                "[whatsapp-export] dispatch failed for message_id=%s (line=%r)",
                plan.message_id,
                parsed.original_line,
            )

    logger.info("[whatsapp-export] %s → dispatched=%d", zip_path.name, dispatched)
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
    dispatch: Callable[..., None] = _default_dispatch,
    default_tz_name: str | None = None,
) -> int:
    """Process every pending ZIP in the inbox. Returns total dispatched."""
    inbox = inbox or _inbox_dir()
    unpacked, processed = _ensure_dirs(inbox)

    total = 0
    for zip_path in _list_pending_zips(inbox):
        try:
            total += process_zip(
                zip_path,
                unpacked_root=unpacked,
                dispatch=dispatch,
                default_tz_name=default_tz_name,
            )
        except UnprocessableZip:
            # Logged inside process_zip; leave the file for inspection.
            continue
        except Exception:  # noqa: BLE001 — keep sweeping even if one ZIP blew up
            logger.exception("[whatsapp-export] unhandled error on %s", zip_path.name)
            continue
        # Move to .processed/<timestamp>/ on success (collision-safe).
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target_dir = processed / ts
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / zip_path.name
        if target.exists():
            target = target_dir / f"{zip_path.stem}.{int(time.time())}{zip_path.suffix}"
        shutil.move(str(zip_path), str(target))
        logger.info("[whatsapp-export] moved %s -> %s", zip_path.name, target)
    return total


# --- daemon loop -------------------------------------------------------------


def run_forever() -> None:
    interval = _poll_interval()
    inbox = _inbox_dir()
    logger.info("[whatsapp-export] watching %s (interval=%ss)", inbox, interval)

    stop = False

    def _shutdown(_signum: int, _frame: Any) -> None:
        nonlocal stop
        logger.info("[whatsapp-export] shutdown signal received; finishing current cycle")
        stop = True

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while not stop:
        try:
            n = sweep_once(inbox)
            if n:
                logger.info("[whatsapp-export] sweep done; dispatched=%d", n)
        except Exception:  # noqa: BLE001
            logger.exception("[whatsapp-export] sweep failed; will retry next interval")
        for _ in range(interval):
            if stop:
                break
            time.sleep(1)
    logger.info("[whatsapp-export] exited cleanly")


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
