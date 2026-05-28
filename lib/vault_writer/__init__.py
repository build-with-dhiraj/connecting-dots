"""Vault writer — the only sanctioned path for creating notes in vault/.

Exposes `write_note(...)` and `stable_id(...)`. See `writer.py` for the
implementation. Routing is driven by `handler` (content-type), not `source`
(ingest channel) — see `writer._route_subdir`.

`enqueue_for_enrichment(path)` is re-exported from `connecting_dots.enrichment`
for the stream consumer's convenience: after a successful `write_note()`, the
consumer appends the path to `data/ner_queue.txt`, and the
`workers.ner_backfill --watch` worker picks it up out-of-band. We deliberately
do NOT call the NER extractor inline — a ~2-3s Claude API call would
back-pressure the consumer's hot path. See
`connecting_dots/enrichment/README.md` for the design rationale.
"""
from .writer import stable_id, write_note


def enqueue_for_enrichment(path):  # type: ignore[no-untyped-def]
    """Append `path` to the NER enrichment queue.

    Lazy import keeps the enrichment package (and its `openai` dep) out of
    the vault_writer's import graph — callers that don't enable enrichment
    pay zero startup cost.
    """
    from connecting_dots.enrichment import enqueue_for_enrichment as _enq

    return _enq(path)


__all__ = ["write_note", "stable_id", "enqueue_for_enrichment"]
