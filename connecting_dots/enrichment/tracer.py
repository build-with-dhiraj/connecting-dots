"""Local JSONL tracer for the NER extractor.

One JSON object per line, append-only, single `write()` per record so concurrent
writers from the asyncio backfill never interleave bytes within a line. Atomic
at the byte level on POSIX for writes up to PIPE_BUF (4 KiB on Linux/macOS) —
records here are well under that ceiling.

Schema (per line):

    {
      "timestamp": "2026-05-28T12:00:00Z",
      "vault_path": "sources/youtube/some-video.md",
      "model": "claude-haiku-4-5",
      "input_tokens": 1234,
      "output_tokens": 56,
      "cached_input_tokens": 1100,
      "cost_usd": 0.00123,
      "entities_count": 7,
      "topics_count": 3,
      "duration_ms": 812.4,
      "error": null
    }

`cost_usd` is computed from the constants table below. Update the numbers
when Anthropic's published rates change; the test suite asserts the math
rather than the absolute number so a price update only touches one place.

Deliberately NOT using `langfuse` — this is a single-user system and vault
contents already go to Anthropic for the extraction itself; piping them to
a third observability vendor adds nothing.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Default trace path. Honors env override so tests redirect cleanly.
_DEFAULT_TRACES_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "ner_traces.jsonl"


# --------------------------------------------------------------------------- #
# Pricing table — Anthropic published rates (USD per 1M tokens)
# Source: https://docs.anthropic.com/en/docs/about-claude/pricing
# Update here when rates change. The "cached" rate applies to
# cache_read_input_tokens (the cached prefix that hits on subsequent calls).
# --------------------------------------------------------------------------- #
_PRICING_PER_1M: dict[str, dict[str, float]] = {
    # Haiku 4.5
    "claude-haiku-4-5": {
        "input": 1.00,
        "output": 5.00,
        "cached_input": 0.10,  # ~10% of input price
        "cache_write": 1.25,   # ~1.25x input price for 5-min TTL writes
    },
    # Sonnet 4.6 — upgrade path if Haiku quality is insufficient
    "claude-sonnet-4-6": {
        "input": 3.00,
        "output": 15.00,
        "cached_input": 0.30,
        "cache_write": 3.75,
    },
    # Opus 4.7 — unlikely for NER but supported for symmetry
    "claude-opus-4-7": {
        "input": 5.00,
        "output": 25.00,
        "cached_input": 0.50,
        "cache_write": 6.25,
    },
}


def compute_cost_usd(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """Compute the per-call cost in USD.

    `input_tokens` is the uncached remainder (full price). `cached_input_tokens`
    are tokens served from cache (~10% price). `cache_creation_tokens` are
    tokens written to cache (~1.25x price for 5-min TTL). `output_tokens` are
    generated tokens (full output price).

    Returns 0.0 for unknown models rather than raising — the trace still gets
    written; we'd rather lose accounting fidelity than drop visibility.
    """
    rates = _PRICING_PER_1M.get(model)
    if rates is None:
        return 0.0
    total = (
        (input_tokens / 1_000_000) * rates["input"]
        + (output_tokens / 1_000_000) * rates["output"]
        + (cached_input_tokens / 1_000_000) * rates["cached_input"]
        + (cache_creation_tokens / 1_000_000) * rates["cache_write"]
    )
    return round(total, 6)


# --------------------------------------------------------------------------- #
# Trace record
# --------------------------------------------------------------------------- #
@dataclass
class Trace:
    """One row in the JSONL trace log."""

    vault_path: str
    model: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cost_usd: float
    entities_count: int
    topics_count: int
    duration_ms: float
    error: Optional[str] = None
    cache_creation_tokens: int = 0
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Append-only writer
# --------------------------------------------------------------------------- #
# Module-level lock so async/thread concurrency from the backfill worker
# never produces a partial line. Disk-level atomicity for a single write()
# call holds for line sizes under PIPE_BUF; the lock protects the
# encode-then-write sequence as a unit.
_TRACE_LOCK = threading.Lock()


def _resolve_traces_path() -> Path:
    env = os.environ.get("CONNECTING_DOTS_NER_TRACES")
    if env:
        return Path(env)
    return _DEFAULT_TRACES_PATH


def append_trace(trace: Trace, *, path: Optional[Path] = None) -> None:
    """Atomically append a single JSON line to the trace log.

    Creates the parent directory and the file if missing. Uses a module-level
    lock to serialize concurrent writers from the asyncio backfill loop.
    """
    target = path or _resolve_traces_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(trace), ensure_ascii=False, sort_keys=True) + "\n"
    encoded = line.encode("utf-8")
    with _TRACE_LOCK:
        # Open + write + close per record. Slow but bulletproof: a crash
        # mid-call leaves either a complete previous line or nothing —
        # never a torn record at end-of-file.
        with open(target, "ab") as f:
            f.write(encoded)


__all__ = ["Trace", "append_trace", "compute_cost_usd"]
