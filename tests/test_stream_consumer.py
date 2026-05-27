"""Coverage tests for `workers.stream_consumer`.

These tests exercise the pure-Python pieces of the Upstash Redis Stream bridge
(XREAD response parsing, offset checkpointing, DLQ append, per-entry processing
with dedupe + dispatch, the `run_once` loop, and SQLite WAL concurrency) without
ever touching the network. `dispatch_url` is patched and the SQLite dedupe DB is
redirected to a `tmp_path`.

Tests must be deterministic and < 1s each.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from workers import stream_consumer


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point all on-disk state at a unique tmpdir per test."""
    offset = tmp_path / "stream_offset.txt"
    dedupe = tmp_path / "dedupe.db"
    dlq = tmp_path / "dlq.jsonl"
    monkeypatch.setattr(stream_consumer, "OFFSET_FILE", offset)
    monkeypatch.setattr(stream_consumer, "DEDUPE_DB", dedupe)
    monkeypatch.setattr(stream_consumer, "DLQ_FILE", dlq)
    return {"offset": offset, "dedupe": dedupe, "dlq": dlq, "root": tmp_path}


@pytest.fixture
def dedupe_conn(tmp_paths):
    conn = stream_consumer._open_dedupe_db()
    yield conn
    conn.close()


def _make_envelope(
    *,
    message_id: str = "wamid.test1",
    url: str = "https://example.com/post",
    source: str = "whatsapp",
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "url": url,
        "source": source,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "raw_payload": {"from": "+1555", "id": message_id},
    }


# --------------------------------------------------------------------------- #
# _parse_xread_response
# --------------------------------------------------------------------------- #
class TestParseXreadResponse:
    def test_empty_none(self):
        assert stream_consumer._parse_xread_response(None) == []

    def test_empty_list(self):
        assert stream_consumer._parse_xread_response([]) == []

    def test_flat_list_format(self):
        """Redis canonical format: [[key, [[id, [k1,v1,k2,v2]], ...]]]."""
        resp = [
            [
                "inbound-stream",
                [
                    ["1700000000000-0", ["envelope", '{"a":1}', "extra", "x"]],
                    ["1700000000001-0", ["envelope", '{"b":2}']],
                ],
            ]
        ]
        out = stream_consumer._parse_xread_response(resp)
        assert out == [
            ("1700000000000-0", {"envelope": '{"a":1}', "extra": "x"}),
            ("1700000000001-0", {"envelope": '{"b":2}'}),
        ]

    def test_dict_kv_format(self):
        """Some Upstash client versions return the kv pairs already as a dict."""
        resp = [["inbound-stream", [["1-0", {"envelope": '{"a":1}'}]]]]
        out = stream_consumer._parse_xread_response(resp)
        assert out == [("1-0", {"envelope": '{"a":1}'})]

    def test_odd_length_kv_truncates_cleanly(self):
        """A dangling key with no value must not crash; it's silently dropped."""
        resp = [["inbound-stream", [["1-0", ["envelope", '{"a":1}', "orphan"]]]]]
        out = stream_consumer._parse_xread_response(resp)
        # 'orphan' has no value — only the complete pair survives.
        assert out == [("1-0", {"envelope": '{"a":1}'})]

    def test_malformed_stream_entry_skipped(self):
        resp = [
            "garbage",  # not a list
            ["only-one-element"],  # too short
            [
                "inbound-stream",
                [
                    "bad-entry",  # not a list
                    ["1-0"],  # entry too short
                    ["2-0", ["envelope", "ok"]],  # good
                ],
            ],
        ]
        out = stream_consumer._parse_xread_response(resp)
        assert out == [("2-0", {"envelope": "ok"})]

    def test_empty_entries_list(self):
        resp = [["inbound-stream", []]]
        assert stream_consumer._parse_xread_response(resp) == []


# --------------------------------------------------------------------------- #
# _read_offset / _write_offset
# --------------------------------------------------------------------------- #
class TestOffsetCheckpoint:
    def test_missing_file_returns_default(self, tmp_paths):
        assert not tmp_paths["offset"].exists()
        assert stream_consumer._read_offset() == "0-0"

    def test_valid_file(self, tmp_paths):
        tmp_paths["offset"].parent.mkdir(parents=True, exist_ok=True)
        tmp_paths["offset"].write_text("1700000000000-42")
        assert stream_consumer._read_offset() == "1700000000000-42"

    def test_empty_file_returns_default(self, tmp_paths):
        tmp_paths["offset"].parent.mkdir(parents=True, exist_ok=True)
        tmp_paths["offset"].write_text("   \n")
        assert stream_consumer._read_offset() == "0-0"

    def test_corrupted_non_utf8(self, tmp_paths):
        tmp_paths["offset"].parent.mkdir(parents=True, exist_ok=True)
        tmp_paths["offset"].write_bytes(b"\xff\xfe\x00bad bytes")
        assert stream_consumer._read_offset() == "0-0"

    def test_partial_write_truncated(self, tmp_paths):
        """A truncated/empty file should fall back to default, not raise."""
        tmp_paths["offset"].parent.mkdir(parents=True, exist_ok=True)
        tmp_paths["offset"].write_text("")
        assert stream_consumer._read_offset() == "0-0"

    def test_write_offset_atomic(self, tmp_paths):
        """_write_offset writes to a tempfile then renames — no `.offset.*` left behind."""
        stream_consumer._write_offset("1700000000000-7")
        assert tmp_paths["offset"].read_text() == "1700000000000-7"
        # No leftover temp files in the parent dir.
        leftovers = [p for p in tmp_paths["root"].iterdir() if p.name.startswith(".offset.")]
        assert leftovers == []

    def test_write_offset_overwrites(self, tmp_paths):
        stream_consumer._write_offset("1-0")
        stream_consumer._write_offset("2-0")
        assert tmp_paths["offset"].read_text() == "2-0"


# --------------------------------------------------------------------------- #
# _append_dlq
# --------------------------------------------------------------------------- #
class TestAppendDlq:
    def test_writes_jsonl_record(self, tmp_paths):
        stream_consumer._append_dlq("1-0", {"envelope": "{}"}, "boom")
        lines = tmp_paths["dlq"].read_text().strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["stream_id"] == "1-0"
        assert rec["envelope"] == {"envelope": "{}"}
        assert rec["error"] == "boom"
        assert "timestamp" in rec

    def test_appends_subsequent_records(self, tmp_paths):
        stream_consumer._append_dlq("1-0", {"a": "1"}, "err1")
        stream_consumer._append_dlq("2-0", {"a": "2"}, "err2")
        lines = tmp_paths["dlq"].read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["stream_id"] == "1-0"
        assert json.loads(lines[1])["stream_id"] == "2-0"


# --------------------------------------------------------------------------- #
# _process_entry
# --------------------------------------------------------------------------- #
class TestProcessEntry:
    def test_happy_path_dispatches_and_dedupes(self, tmp_paths, dedupe_conn):
        env = _make_envelope(message_id="msg-happy")
        fields = {"envelope": json.dumps(env)}
        with mock.patch.object(stream_consumer, "dispatch_url") as mock_dispatch:
            stream_consumer._process_entry("1-0", fields, dedupe_conn)
            assert mock_dispatch.call_count == 1
            kwargs = mock_dispatch.call_args.kwargs
            assert kwargs["url"] == "https://example.com/post"
            assert kwargs["source"] == "whatsapp"
            # Dedupe row should now exist.
            row = dedupe_conn.execute(
                "SELECT message_id FROM seen_message_ids WHERE message_id=?",
                ("msg-happy",),
            ).fetchone()
            assert row is not None

    def test_dedupe_collision_skips_dispatch(self, tmp_paths, dedupe_conn):
        env = _make_envelope(message_id="msg-dup")
        fields = {"envelope": json.dumps(env)}
        with mock.patch.object(stream_consumer, "dispatch_url") as mock_dispatch:
            stream_consumer._process_entry("1-0", fields, dedupe_conn)
            stream_consumer._process_entry("2-0", fields, dedupe_conn)
            assert mock_dispatch.call_count == 1  # second call deduped

    def test_missing_envelope_field_skipped(self, tmp_paths, dedupe_conn):
        with mock.patch.object(stream_consumer, "dispatch_url") as mock_dispatch:
            stream_consumer._process_entry("1-0", {"not_envelope": "x"}, dedupe_conn)
            mock_dispatch.assert_not_called()

    def test_malformed_envelope_skipped(self, tmp_paths, dedupe_conn):
        fields = {"envelope": "not json"}
        with mock.patch.object(stream_consumer, "dispatch_url") as mock_dispatch:
            stream_consumer._process_entry("1-0", fields, dedupe_conn)
            mock_dispatch.assert_not_called()

    def test_schema_violation_envelope_skipped(self, tmp_paths, dedupe_conn):
        """Valid JSON but missing required fields -> InboundEnvelope rejects."""
        fields = {"envelope": json.dumps({"message_id": "x"})}  # missing url/source/...
        with mock.patch.object(stream_consumer, "dispatch_url") as mock_dispatch:
            stream_consumer._process_entry("1-0", fields, dedupe_conn)
            mock_dispatch.assert_not_called()

    def test_dispatch_failure_does_not_raise(self, tmp_paths, dedupe_conn):
        """A failed dispatch is logged but must not propagate — offset still advances."""
        env = _make_envelope(message_id="msg-fail")
        fields = {"envelope": json.dumps(env)}
        with mock.patch.object(
            stream_consumer, "dispatch_url", side_effect=RuntimeError("boom")
        ):
            # Should not raise.
            stream_consumer._process_entry("1-0", fields, dedupe_conn)
        # Message was still claimed in dedupe (at-least-once: we don't retry).
        row = dedupe_conn.execute(
            "SELECT message_id FROM seen_message_ids WHERE message_id=?",
            ("msg-fail",),
        ).fetchone()
        assert row is not None


# --------------------------------------------------------------------------- #
# run_once
# --------------------------------------------------------------------------- #
class _FakeRedis:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: list[tuple[dict, int, int]] = []

    def xread(self, streams: dict, count: int, block: int) -> Any:
        self.calls.append((dict(streams), count, block))
        return self._response


class TestRunOnce:
    def test_no_entries_returns_prior_offset(self, tmp_paths, dedupe_conn):
        stream_consumer._write_offset("100-0")
        fake = _FakeRedis(response=None)
        last = stream_consumer.run_once(fake, dedupe_conn)
        assert last == "100-0"
        # Offset file unchanged.
        assert tmp_paths["offset"].read_text() == "100-0"

    def test_single_entry_advances_offset(self, tmp_paths, dedupe_conn):
        env = _make_envelope(message_id="msg-single")
        resp = [
            [
                stream_consumer.STREAM_KEY,
                [["1700-0", ["envelope", json.dumps(env)]]],
            ]
        ]
        fake = _FakeRedis(response=resp)
        with mock.patch.object(stream_consumer, "dispatch_url") as mock_dispatch:
            last = stream_consumer.run_once(fake, dedupe_conn)
        assert last == "1700-0"
        assert tmp_paths["offset"].read_text() == "1700-0"
        assert mock_dispatch.call_count == 1

    def test_batch_advances_to_last_id(self, tmp_paths, dedupe_conn):
        entries = []
        for i in range(5):
            env = _make_envelope(message_id=f"msg-{i}")
            entries.append([f"170{i}-0", ["envelope", json.dumps(env)]])
        resp = [[stream_consumer.STREAM_KEY, entries]]
        fake = _FakeRedis(response=resp)
        with mock.patch.object(stream_consumer, "dispatch_url") as mock_dispatch:
            last = stream_consumer.run_once(fake, dedupe_conn)
        assert last == "1704-0"
        assert tmp_paths["offset"].read_text() == "1704-0"
        assert mock_dispatch.call_count == 5

    def test_dispatch_failure_midbatch_still_advances(self, tmp_paths, dedupe_conn):
        entries = []
        for i in range(3):
            env = _make_envelope(message_id=f"msg-{i}")
            entries.append([f"170{i}-0", ["envelope", json.dumps(env)]])
        resp = [[stream_consumer.STREAM_KEY, entries]]
        fake = _FakeRedis(response=resp)
        # Middle dispatch fails — but per current implementation the loop
        # continues and the offset still advances past all entries.
        with mock.patch.object(
            stream_consumer,
            "dispatch_url",
            side_effect=[None, RuntimeError("middle"), None],
        ):
            last = stream_consumer.run_once(fake, dedupe_conn)
        assert last == "1702-0"
        assert tmp_paths["offset"].read_text() == "1702-0"


# --------------------------------------------------------------------------- #
# SQLite WAL concurrency
# --------------------------------------------------------------------------- #
class TestSqliteConcurrency:
    def test_two_writer_threads_no_locked_error(self, tmp_paths):
        """Two threads writing to the same WAL-mode DB must not hit
        `OperationalError: database is locked`."""
        # Seed the schema once.
        seed = stream_consumer._open_dedupe_db()
        seed.close()

        errors: list[Exception] = []

        def worker(prefix: str, n: int) -> None:
            try:
                conn = sqlite3.connect(
                    str(tmp_paths["dedupe"]), isolation_level=None, timeout=5.0
                )
                conn.execute("PRAGMA busy_timeout=5000")
                for i in range(n):
                    stream_consumer._mark_seen(conn, f"{prefix}-{i}")
                conn.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=worker, args=("A", 50))
        t2 = threading.Thread(target=worker, args=("B", 50))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"concurrent writes raised: {errors!r}"

        # All 100 rows present.
        conn = sqlite3.connect(str(tmp_paths["dedupe"]))
        count = conn.execute("SELECT COUNT(*) FROM seen_message_ids").fetchone()[0]
        conn.close()
        assert count == 100
