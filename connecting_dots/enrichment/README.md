# Enrichment — NER + topic extraction (component #8)

## What it does

For every note in the vault, extract:
- **Entities** — people, organizations, products, concepts, locations, works.
  Written to YAML frontmatter `entities: [...]`.
- **Topics** — short noun-phrase tags describing what the note is about.
  Written to YAML frontmatter `topics: [...]`.

This is the foundation for the entity-overlap edge builder (#11) — the graph
edges used by `activity_relevance` and `static_profile_match` in the resurfacing
scorer (#13). Garbage in → garbage edges → garbage resurfacing. Quality matters.

## How it works

`connecting_dots.enrichment.ner.extract(...)` calls the Claude API with:

1. **Tool-use mode.** A single tool `record_extraction(entities, topics)` with
   a strict `input_schema`. The model can only respond by calling this tool —
   no free-text JSON to regex-parse.
2. **Prompt caching.** The system prompt + few-shot examples (~2k tokens) sit
   behind a `cache_control: {"type": "ephemeral"}` breakpoint on the last
   system block. Render order is `tools` → `system` → `messages`, so the tool
   definition is cached alongside the system prompt. The per-note content
   goes in `messages` *after* the breakpoint, so it never invalidates the
   cached prefix. Effective savings: ~70% across the backfill.
3. **Body truncation.** Bodies are truncated to 4000 chars to bound cost on
   long YouTube transcripts. The first 4000 chars is more than enough to
   identify entities and topics — title + intro carries most of the signal.
4. **Confidence threshold.** Entities with `confidence < 0.7` are dropped
   before frontmatter writeback. Low-confidence entries still appear in the
   trace's raw payload for debugging.

Default model: `claude-haiku-4-5`. Override via `NER_MODEL` env var. Upgrade
path to `claude-sonnet-4-6` if Haiku quality is insufficient (the judge gold
set in `tests/enrichment/test_judge.py` will tell you — F1 < 0.70 fails CI).

## Observability — local JSONL only

Every extraction call appends one JSON line to `data/ner_traces.jsonl`:

```json
{
  "timestamp": "2026-05-28T12:00:00Z",
  "vault_path": "sources/youtube/some-video.md",
  "model": "claude-haiku-4-5",
  "input_tokens": 1234,
  "output_tokens": 56,
  "cached_input_tokens": 1100,
  "cost_usd": 0.001234,
  "entities_count": 7,
  "topics_count": 3,
  "duration_ms": 812.4,
  "error": null
}
```

**No langfuse.** No cloud observability vendor is wired in. This is a
single-user personal-knowledge system; vault contents already leave the
machine for the extractor call itself, and piping the same data to a
third-party tracing service buys nothing useful for a one-user system.
`grep`, `jq`, and DuckDB are sufficient to query a JSONL file.

## Running the backfill

```bash
# Sanity check on a small batch first — costs ~$0.005 with cached prefix.
.venv/bin/python -m workers.ner_backfill --limit 5

# Inspect data/ner_traces.jsonl, eyeball a few notes' frontmatter, then:
.venv/bin/python -m workers.ner_backfill        # full sweep

# Live mode: drain the enrichment queue every poll interval.
.venv/bin/python -m workers.ner_backfill --watch
```

Idempotency: a note is skipped if it already has non-empty `entities` OR if
`raw_meta.ner_enriched_at` is set. Safe to re-run after a partial sweep.

## Expected cost

Full backfill of 1,464 notes at Haiku 4.5 pricing with prompt caching:
- First call: ~2k tokens prefix written to cache (~$0.0025) + per-note variable
  content (~500 tokens, ~$0.0005) + ~100 tokens output (~$0.0005) = ~$0.0035.
- Subsequent calls (cache hit): ~2k cached input ($0.0002) + ~500 variable
  ($0.0005) + ~100 output ($0.0005) = ~$0.0012/note.
- Total: ~$0.0035 + 1463 × $0.0012 ≈ **$1.75 - $2.50**.

A worst-case run without cache hits (every call a fresh prefix — happens if
the system prompt is edited between calls) would be ~$5-7. Watch the
`cached_input_tokens` column in `data/ner_traces.jsonl` — if it's zero, the
prompt prefix is being invalidated somewhere (see `shared/prompt-caching.md`
silent-invalidator audit).

## Inline enrichment for new notes

The vault writer (`lib/vault_writer/writer.py`) is called synchronously from
the stream consumer. Adding a 2-3s Claude call to that hot path would
back-pressure the consumer and bound ingest QPS to extractor throughput.

Instead, the consumer (optionally) appends the written path to
`data/ner_queue.txt` via `enqueue_for_enrichment(path)`. The backfill worker
in `--watch` mode drains the queue every poll interval and processes each
entry through the same extractor + frontmatter-rewrite path.

## Quality gate

`tests/enrichment/test_judge.py` runs the extractor against a 30-item gold
set of synthetic notes (`tests/enrichment/gold_set.json`) and asserts the
aggregate F1 ≥ 0.70. The gold set is hand-crafted to mirror the domain
distribution (finance, tech, personal saves) — no real user data.
