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

Also includes:
- `moc` -- Map of Content synthesis via Azure OpenAI (workers.moc_generator)
- `title` -- Smart title rewriter for garbage filenames (workers.title_backfill)
- `tldr` -- 2-sentence TL;DR prepend (workers.tldr_backfill)

Observability is **local JSONL only** (`data/ner_traces.jsonl`, etc.). No cloud
observability vendor is wired in -- this is a single-user system and vault
contents already go to Azure OpenAI for the extraction itself; piping them
to a third observability vendor adds nothing.
"""
from __future__ import annotations

from .moc import MoCResult, synthesize
from .ner import NERResult, extract
from .queue import enqueue_for_enrichment
from .title import TitleResult, needs_rewrite
from .title import rewrite as rewrite_title
from .tldr import TLDRResult
from .tldr import extract as extract_tldr
from .tracer import Trace, append_trace

__all__ = [
    "MoCResult",
    "NERResult",
    "Trace",
    "TLDRResult",
    "TitleResult",
    "append_trace",
    "enqueue_for_enrichment",
    "extract",
    "extract_tldr",
    "needs_rewrite",
    "rewrite_title",
    "synthesize",
]
