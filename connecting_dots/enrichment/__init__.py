"""NER + topic extraction pipeline (component #8).

Extracts named entities (people, products, organizations, concepts, etc.) and
topics from vault notes, writing the result back into the note's YAML
frontmatter (`entities`, `topics`, `raw_meta.ner_enriched_at`).

Designed as a side process that runs on top of the vault writer (#10) without
back-pressuring the hot write path:

- `lib.vault_writer.write_note(...)` returns immediately after the atomic write.
- Stream consumer calls `enqueue_for_enrichment(path)` to append the path to a
  durable queue file (`data/ner_queue.txt`).
- `workers.ner_backfill --watch` drains the queue (and `--limit N` runs a
  one-shot sweep over the existing vault — used for the initial backfill).

Observability is **local JSONL only** (`data/ner_traces.jsonl`). No cloud
observability vendor is wired in — this is a single-user pipeline and the
user does not want vault text leaving the box outside the extractor call
itself.
"""
from __future__ import annotations

from .ner import NERResult, extract
from .queue import enqueue_for_enrichment
from .tracer import Trace, append_trace

__all__ = [
    "NERResult",
    "Trace",
    "append_trace",
    "enqueue_for_enrichment",
    "extract",
]
