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

`connecting_dots.enrichment.ner.extract(...)` calls the Azure OpenAI Chat
Completions API (model: `gpt-4.1`) with:

1. **Function calling.** A single function `record_extraction(entities, topics)`
   with a strict JSON Schema. `tool_choice` is pinned to that function so the
   model can only respond by calling it — no free-text JSON to regex-parse.
2. **Automatic prompt-prefix caching.** Azure OpenAI caches the leading
   messages automatically when the same prefix repeats across calls. There is
   no `cache_control` flag to set (unlike Anthropic). The contract is simply
   that `messages[0]` (the system prompt + few-shot examples, ~2k tokens) is
   byte-identical across every call. Per-note variable content sits in
   `messages[1]` (the user message), after the stable prefix, so it never
   invalidates the cache. Cache hits surface as
   `response.usage.prompt_tokens_details.cached_tokens` and are billed at 50%
   of the normal input rate.
3. **Body truncation.** Bodies are truncated to 4000 chars to bound cost on
   long YouTube transcripts. The first 4000 chars is more than enough to
   identify entities and topics — title + intro carries most of the signal.
4. **Confidence threshold.** Entities with `confidence < 0.7` are dropped
   before frontmatter writeback. Low-confidence entries still appear in the
   trace's raw payload for debugging.

Default deployment name: `gpt-4.1`. Override via `NER_MODEL` env var (or
`AZURE_OPENAI_DEPLOYMENT`). The judge gold set in
`tests/enrichment/test_judge.py` will tell you if a model swap drops quality
below the F1 ≥ 0.70 floor.

## Environment

```
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_DEPLOYMENT=gpt-4.1
AZURE_OPENAI_API_VERSION=preview      # or a dated version like 2024-12-01-preview
NER_MODEL=gpt-4.1
NER_CONCURRENCY=4
```

The deployment must exist in Azure Portal. `AZURE_OPENAI_API_VERSION=preview`
follows the rolling v1 preview surface; pin to a dated version if you want
strict reproducibility.

## Observability — local JSONL only

Every extraction call appends one JSON line to `data/ner_traces.jsonl`:

```json
{
  "timestamp": "2026-05-28T12:00:00Z",
  "vault_path": "sources/youtube/some-video.md",
  "model": "gpt-4.1",
  "input_tokens": 2300,
  "output_tokens": 56,
  "cached_input_tokens": 1800,
  "cost_usd": 0.001234,
  "entities_count": 7,
  "topics_count": 3,
  "duration_ms": 812.4,
  "error": null
}
```

Note: on OpenAI/Azure the `input_tokens` field is the **total** prompt token
count (cached + uncached), which mirrors `response.usage.prompt_tokens`. The
`cached_input_tokens` subset comes from `prompt_tokens_details.cached_tokens`.
The pricing math in `tracer.py` charges `(input - cached) * input_rate +
cached * cached_rate + output * output_rate`.

**No langfuse.** No cloud observability vendor is wired in. This is a
single-user personal-knowledge system; vault contents already leave the
machine for the extractor call itself, and piping the same data to a
third-party tracing service buys nothing useful for a one-user system.
`grep`, `jq`, and DuckDB are sufficient to query a JSONL file.

## Running the backfill

```bash
# Sanity check on a small batch first — costs ~$0.01 with cached prefix.
.venv/bin/python -m workers.ner_backfill --limit 5

# Inspect data/ner_traces.jsonl, eyeball a few notes' frontmatter, then:
.venv/bin/python -m workers.ner_backfill        # full sweep

# Live mode: drain the enrichment queue every poll interval.
.venv/bin/python -m workers.ner_backfill --watch
```

Idempotency: a note is skipped if it already has non-empty `entities` OR if
`raw_meta.ner_enriched_at` is set. Safe to re-run after a partial sweep.

## Expected cost

Full backfill of 1,464 notes at Azure gpt-4.1 pricing
(input $2.00 / 1M, cached input $1.00 / 1M, output $8.00 / 1M) with automatic
prompt-prefix caching:

- First call: ~2000 tokens prefix uncached + ~500 variable + ~100 output
  ≈ (2500 × 2.00 + 100 × 8.00) / 1M ≈ $0.0058.
- Subsequent calls (cache hit on ~2000-token prefix): 2000 cached at $1.00/1M
  + ~500 fresh at $2.00/1M + ~100 output at $8.00/1M
  ≈ (2000 × 1.00 + 500 × 2.00 + 100 × 8.00) / 1M ≈ $0.0038/note.
- Total: ~$0.006 + 1463 × $0.0038 ≈ **$3.50 – $4.00**.

A worst-case run without cache hits (every call a fresh prefix — happens if
the system prompt is edited between calls) would be ~$6-7. Watch the
`cached_input_tokens` column in `data/ner_traces.jsonl` — if it's zero on
calls 2..N, the prompt prefix is being invalidated somewhere (see
`shared/prompt-caching.md` silent-invalidator audit).

## Inline enrichment for new notes

The vault writer (`lib/vault_writer/writer.py`) is called synchronously from
the stream consumer. Adding a 2-3s Azure OpenAI call to that hot path would
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
