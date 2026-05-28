"""Inline-enrichment queue file.

The vault writer (#10) is called synchronously from the stream consumer. Adding
a ~2-3s Claude API call to that path would back-pressure the consumer and bound
ingest throughput to the extractor's QPS. Instead, the consumer (optionally)
calls `enqueue_for_enrichment(path)` after `write_note()` returns, which appends
the vault-relative path to `data/ner_queue.txt`. The backfill worker
(`workers.ner_backfill --watch`) tails the queue and processes each entry,
removing it on success.

The queue file is a plain newline-delimited text file — durable, human-readable,
trivially recoverable. No SQLite, no Redis. Concurrent appends from multiple
writers are line-safe up to PIPE_BUF (paths here are well under).
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

_DEFAULT_QUEUE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "ner_queue.txt"

_QUEUE_LOCK = threading.Lock()


def _resolve_queue_path() -> Path:
    env = os.environ.get("CONNECTING_DOTS_NER_QUEUE")
    if env:
        return Path(env)
    return _DEFAULT_QUEUE_PATH


def enqueue_for_enrichment(path: Path | str, *, queue_path: Optional[Path] = None) -> None:
    """Append `path` to the enrichment queue.

    `path` may be absolute or vault-relative — the backfill worker resolves
    both against the active vault root. No-op if the path is empty.
    """
    p = str(path).strip()
    if not p:
        return
    target = queue_path or _resolve_queue_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    line = (p + "\n").encode("utf-8")
    with _QUEUE_LOCK:
        with open(target, "ab") as f:
            f.write(line)


def drain_queue(*, queue_path: Optional[Path] = None) -> list[str]:
    """Read all queued paths, truncate the file, return the list.

    Atomic: opens the file, reads contents, then truncates under the lock so
    a concurrent appender after the truncate appends to an empty file (its
    line survives for the next drain). A crash between read and truncate
    leaves the queue intact — the next drain re-processes (idempotent
    downstream — see `workers.ner_backfill`).
    """
    target = queue_path or _resolve_queue_path()
    if not target.exists():
        return []
    with _QUEUE_LOCK:
        with open(target, "r+", encoding="utf-8") as f:
            content = f.read()
            f.seek(0)
            f.truncate()
    return [line.strip() for line in content.splitlines() if line.strip()]


__all__ = ["enqueue_for_enrichment", "drain_queue"]
