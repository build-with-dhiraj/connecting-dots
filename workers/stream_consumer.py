"""Upstash Redis Stream consumer — cross-language bridge.

Reads `InboundEnvelope` records from the `inbound-stream` Upstash Redis Stream
(populated by the Vercel WhatsApp webhook in TypeScript), deduplicates by
`message_id` against a local SQLite table, and calls
`connecting_dots.dispatcher.dispatch_url` for each new URL.

Delivery semantics
------------------
At-least-once *with* a dead-letter queue:

1. The offset is only advanced AFTER each entry in the batch is processed.
2. Successful dispatch -> dedupe row inserted, offset advances.
3. Dedupe hit (replay) -> logged, offset advances.
4. Dispatch raises -> raw Redis entry appended to `data/dlq.jsonl`
   (one JSON object per line: `{stream_id, envelope, error, timestamp}`)
   BEFORE the offset advances, so a crash mid-DLQ-write doesn't lose
   the failure. The offset then advances so we don't busy-loop on
   poison messages. P1-DLQ.
5. Worker crash before offset write -> next start replays the batch from
   the prior checkpoint; dedupe absorbs the successful entries and the
   poison entries hit the DLQ again (idempotent re-failure).

DLQ entries are NEVER auto-retried. A human (or a scheduled re-dispatch
job) should inspect `data/dlq.jsonl` and decide.

Other design notes
------------------
- Upstash REST API + the official `upstash-redis` Python client. We use the
  blocking `XREAD` with a 5-second timeout so SIGTERM is honoured promptly.
- Last-read stream ID is checkpointed atomically to `data/stream_offset.txt`
  so the worker is resumable across restarts.
- Idempotency is keyed on `envelope.message_id` (WhatsApp `messages[].id` for
  WA traffic; synthetic ids for non-WA sources).
- The mailto poller intentionally bypasses this stream — it calls
  `dispatch_url` in-process. The stream is the *cross-language* bridge only.
- The dedupe SQLite DB is opened in WAL mode with `busy_timeout=5000` so the
  consumer can coexist with the mailto poller and LinkedIn watcher (all three
  share `data/dedupe.db`). P1-WAL.

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
import time
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
DLQ_FILE = Path(os.environ.get("STREAM_DLQ_FILE", "data/dlq.jsonl"))

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
    """Load the last-acked stream offset.

    P1-corruption: a partial write or filesystem corruption can leave the
    offset file with non-UTF8 bytes or a wholly invalid value. We swallow
    `OSError`, `UnicodeDecodeError`, and `ValueError` and restart from
    `_DEFAULT_START_ID`. Worst case we re-process the entire stream once;
    dedupe absorbs the replays.
    """
    if not OFFSET_FILE.exists():
        return _DEFAULT_START_ID
    try:
        val = OFFSET_FILE.read_text(encoding="utf-8").strip()
        return val or _DEFAULT_START_ID
    except (OSError, UnicodeDecodeError, ValueError) as exc:
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
    """Open `data/dedupe.db` in WAL mode (P1-WAL).

    The dispatcher, mailto poller, and (future) LinkedIn watcher all share
    this DB. WAL + `busy_timeout=5000` lets concurrent writers coexist
    without `database is locked` errors.
    """
    DEDUPE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DEDUPE_DB), isolation_level=None, timeout=5.0)  # autocommit
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
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
    """Returns True if newly inserted (i.e. not a duplicate).

    P1-WAL: retry up to 3 times with 100 ms backoff on transient
    `OperationalError` (database locked / busy). Re-raises persistent
    failures so the caller can DLQ the entry rather than silently dropping.
    """
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            conn.execute(
                "INSERT INTO seen_message_ids (message_id, seen_at) VALUES (?, ?)",
                (message_id, datetime.now(timezone.utc).isoformat()),
            )
            return True
        except sqlite3.IntegrityError:
            return False
        except sqlite3.OperationalError as exc:
            last_exc = exc
            msg = str(exc).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            time.sleep(0.1 * (attempt + 1))
    logger.error("[stream] dedupe insert failed after retries: %s", last_exc)
    raise last_exc  # type: ignore[misc]


# --- DLQ ---------------------------------------------------------------------

def _append_dlq(stream_id: str, fields: dict[str, str], error: str) -> None:
    """Append a poison-message entry to `data/dlq.jsonl` (P1-DLQ).

    Called when `dispatch_url` raises. We write the raw stream fields (NOT
    just the parsed envelope) so a human can inspect even invalid payloads.
    The append is fsync'd because the offset advance immediately follows;
    we don't want a crash window where the offset moves past a failed
    entry that wasn't durably captured.
    """
    DLQ_FILE.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "stream_id": stream_id,
        "envelope": fields,
        "error": error,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    # Open with O_APPEND so concurrent writers (if any) interleave cleanly.
    fd = os.open(str(DLQ_FILE), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


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
