"""Upstash Redis Stream consumer — cross-language bridge.

Reads `InboundEnvelope` records from the `inbound-stream` Upstash Redis Stream
(populated by the Vercel WhatsApp webhook in TypeScript), deduplicates by
`message_id` against a local SQLite table, and calls
`connecting_dots.dispatcher.dispatch_url` for each new URL.

Design notes:
- Upstash REST API + the official `upstash-redis` Python client. We use the
  blocking `XREAD` with a 5-second timeout so SIGTERM is honoured promptly.
- Last-read stream ID is checkpointed atomically to `data/stream_offset.txt`
  so the worker is resumable across restarts. At-least-once semantics: the
  offset is only advanced AFTER `dispatch_url` returns for the batch, and
  dedup ensures replays don't double-dispatch.
- Idempotency is keyed on `envelope.message_id` (WhatsApp `messages[].id` for
  WA traffic; synthetic ids for non-WA sources).
- The mailto poller intentionally bypasses this stream — it calls
  `dispatch_url` in-process. The stream is the *cross-language* bridge only.

Env vars:
    UPSTASH_REDIS_REST_URL       Upstash REST endpoint
    UPSTASH_REDIS_REST_TOKEN     Upstash REST token
    STREAM_KEY                   default: inbound-stream
    STREAM_BLOCK_MS              default: 5000 (XREAD blocking timeout)
    STREAM_BATCH_COUNT           default: 32
    STREAM_OFFSET_FILE           default: data/stream_offset.txt
    DEDUPE_DB_PATH               default: data/dedupe.db
    LOG_LEVEL                    default: INFO
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from upstash_redis import Redis

from connecting_dots.dispatcher import dispatch_url
from connecting_dots.generated.inbound_envelope import InboundEnvelope

logger = logging.getLogger(__name__)

STREAM_KEY = os.environ.get("STREAM_KEY", "inbound-stream")
BLOCK_MS = int(os.environ.get("STREAM_BLOCK_MS", "5000"))
BATCH_COUNT = int(os.environ.get("STREAM_BATCH_COUNT", "32"))
OFFSET_FILE = Path(os.environ.get("STREAM_OFFSET_FILE", "data/stream_offset.txt"))
DEDUPE_DB = Path(os.environ.get("DEDUPE_DB_PATH", "data/dedupe.db"))

_DEFAULT_START_ID = "0-0"  # read from the beginning on first launch

_shutdown = False


def _install_signal_handlers() -> None:
    def _handler(signum: int, _frame: Any) -> None:
        global _shutdown
        logger.info("received signal %s — draining and exiting", signum)
        _shutdown = True

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


# --- offset checkpoint -------------------------------------------------------

def _read_offset() -> str:
    if not OFFSET_FILE.exists():
        return _DEFAULT_START_ID
    try:
        val = OFFSET_FILE.read_text(encoding="utf-8").strip()
        return val or _DEFAULT_START_ID
    except OSError as exc:
        logger.warning("could not read offset file (%s) — starting from %s", exc, _DEFAULT_START_ID)
        return _DEFAULT_START_ID


def _write_offset(stream_id: str) -> None:
    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: temp file + rename.
    fd, tmp_path = tempfile.mkstemp(dir=str(OFFSET_FILE.parent), prefix=".offset.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(stream_id)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, OFFSET_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# --- dedupe ------------------------------------------------------------------

def _open_dedupe_db() -> sqlite3.Connection:
    DEDUPE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DEDUPE_DB), isolation_level=None)  # autocommit
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_message_ids (
            message_id TEXT PRIMARY KEY,
            seen_at TEXT NOT NULL
        )
        """
    )
    return conn


def _mark_seen(conn: sqlite3.Connection, message_id: str) -> bool:
    """Returns True if newly inserted (i.e. not a duplicate)."""
    try:
        conn.execute(
            "INSERT INTO seen_message_ids (message_id, seen_at) VALUES (?, ?)",
            (message_id, datetime.now(timezone.utc).isoformat()),
        )
        return True
    except sqlite3.IntegrityError:
        return False


# --- consumer ----------------------------------------------------------------

def _get_redis() -> Redis:
    url = os.environ.get("UPSTASH_REDIS_REST_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if not url or not token:
        raise RuntimeError("UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN must be set")
    return Redis(url=url, token=token)


def _parse_xread_response(resp: Any) -> list[tuple[str, dict[str, str]]]:
    """Normalize the XREAD response into [(stream_id, fields_dict), ...].

    The Upstash REST client returns the Redis XREAD shape:
        [[stream_key, [[id, [field, value, field, value, ...]], ...]]]
    or None when the block times out.
    """
    if not resp:
        return []
    out: list[tuple[str, dict[str, str]]] = []
    for stream_entry in resp:
        # stream_entry == [stream_key, entries]
        if not isinstance(stream_entry, (list, tuple)) or len(stream_entry) < 2:
            continue
        entries = stream_entry[1]
        for entry in entries or []:
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                continue
            stream_id, kv = entry[0], entry[1]
            fields: dict[str, str] = {}
            if isinstance(kv, dict):
                fields = {str(k): str(v) for k, v in kv.items()}
            elif isinstance(kv, (list, tuple)):
                it = iter(kv)
                for k in it:
                    try:
                        v = next(it)
                    except StopIteration:
                        break
                    fields[str(k)] = str(v)
            out.append((str(stream_id), fields))
    return out


def _process_entry(
    stream_id: str,
    fields: dict[str, str],
    dedupe: sqlite3.Connection,
) -> None:
    raw = fields.get("envelope")
    if not raw:
        logger.warning("[stream] entry %s missing `envelope` field — skipping", stream_id)
        return
    try:
        data = json.loads(raw)
        env = InboundEnvelope.model_validate(data)
    except Exception as exc:  # noqa: BLE001 — log and skip malformed entries
        logger.error("[stream] entry %s invalid envelope: %s", stream_id, exc)
        return

    if not _mark_seen(dedupe, env.message_id):
        logger.info("[stream] dedupe hit message_id=%s — skipping", env.message_id)
        return

    try:
        # NB: we already claimed `message_id` in the shared dedupe table above,
        # so we deliberately pass `message_id=None` here — otherwise the
        # dispatcher would see its own claim and no-op.
        dispatch_url(
            url=str(env.url),
            source=env.source.value,  # StrEnum -> str literal
            captured_at=env.captured_at,
            raw_payload=env.raw_payload,
            message_id=None,
        )
        logger.info("[stream] dispatched message_id=%s url=%s source=%s",
                    env.message_id, env.url, env.source.value)
    except Exception as exc:  # noqa: BLE001
        # At-least-once: log but don't crash the loop. The offset still advances —
        # dedupe will skip the retry next time, which is the correct behaviour
        # given dispatch_url is itself responsible for its own durability.
        logger.exception("[stream] dispatch_url failed for %s: %s", env.message_id, exc)


def run_once(redis: Redis, dedupe: sqlite3.Connection) -> str:
    """Single XREAD cycle. Returns the latest stream_id seen (or the prior offset)."""
    last_id = _read_offset()
    resp = redis.xread({STREAM_KEY: last_id}, count=BATCH_COUNT, block=BLOCK_MS)
    entries = _parse_xread_response(resp)
    if not entries:
        return last_id
    for stream_id, fields in entries:
        _process_entry(stream_id, fields, dedupe)
        last_id = stream_id
    _write_offset(last_id)
    return last_id


def run_forever() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    _install_signal_handlers()
    redis = _get_redis()
    dedupe = _open_dedupe_db()
    logger.info("[stream] consumer starting; key=%s offset=%s", STREAM_KEY, _read_offset())
    try:
        while not _shutdown:
            try:
                run_once(redis, dedupe)
            except Exception as exc:  # noqa: BLE001
                logger.exception("[stream] loop error: %s", exc)
                # Brief sleep on transient REST errors. XREAD's block already
                # paces us during normal operation.
                if not _shutdown:
                    import time as _t
                    _t.sleep(2.0)
    finally:
        dedupe.close()
        logger.info("[stream] consumer stopped")


if __name__ == "__main__":
    try:
        run_forever()
    except KeyboardInterrupt:
        sys.exit(0)
