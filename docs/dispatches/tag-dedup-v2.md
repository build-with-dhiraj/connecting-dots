# Tag dedup v2 — embedding-based candidate generation + entity canonicalization

> See `docs/dispatches/README.md` for the dispatch protocol. Hard caps in §9.

## 1. Goal

The v1 tag dedup (already merged) only achieved ~9% reduction because its
candidate-pair generation only compared tags sharing a **word prefix**. So
`#entity/ai` and `#entity/artificial-intelligence` were never even compared.

Fix it with **embedding-based candidate generation** so semantically-near tags
get caught regardless of spelling, AND apply the resulting canonical map to the
`entities:` frontmatter arrays (not just `tags:`) so the wikilink builder
benefits too.

**Cost:** ~$0.50-2 Azure (embeddings are cheap; LLM judging on tighter
clusters), unlimited budget available. ~$2 Claude for the dispatch.

## 2. What exists (do NOT re-Read — inlined)

`connecting_dots/enrichment/tag_dedup.py` already has:

- `phase_a_normalize()` — groups by `(namespace, normalized_key)`, picks
  shortest+lex-smallest canonical. KEEP THIS.
- `_pairs_sharing_word_prefix()` (line ~157) — the WEAK candidate generator.
  **REPLACE with embedding-based clustering.**
- `phase_b_llm_judge()` — judges candidate pairs via gpt-4.1 tool-calling.
  KEEP, but feed it the new embedding-derived candidates.
- `phase_c` — combines A+B, caches to `data/tag_canonical_map.json`. KEEP.
- `phase_d` (apply) — rewrites vault `tags:` frontmatter. EXTEND to also rewrite
  `entities:` arrays (see §4).

The Azure client + tool-calling pattern is in
`connecting_dots/enrichment/ner.py:_get_client()`. The embeddings deployment:
use the same Azure endpoint; the embedding model deployment name should be read
from a new env var `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` (the user will add it;
default to `text-embedding-3-small` if unset). If the user's Azure resource
doesn't have an embeddings deployment, FALL BACK to a broadened lexical
candidate generator (see §3 fallback).

## 3. New candidate generation

Replace `_pairs_sharing_word_prefix` with `_embedding_candidates`:

1. Collect all unique tag **values** (strip the `#namespace/` prefix — embed
   only the human-readable part, e.g. `artificial-intelligence`).
2. Batch-embed them via Azure embeddings (`text-embedding-3-small`, 1536-dim,
   batches of ~200). Cache embeddings to `data/tag_embeddings.json` so re-runs
   skip the embed cost.
3. Within each **namespace** (`entity`, `topic` — don't cross-merge entity with
   topic), compute pairwise cosine similarity.
4. Emit candidate pairs where cosine ≥ 0.85 (configurable `--embed-threshold`).
5. Feed those candidates to the existing `phase_b_llm_judge` for final
   yes/no + canonical selection. The LLM is the safety net against false
   merges (e.g. `india`↔`indian` may be cosine-near but the judge vetoes).

### Fallback (no embeddings deployment available)

If `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` is unset AND a probe call fails, broaden
the lexical candidate generator instead:
- Compare tags sharing ANY word (not just prefix)
- Add acronym matching: if tag A is an acronym of tag B's words (`ml` ↔
  `machine learning`), pair them
- Add substring containment (`ai` ⊂ `ai engineering`? No — that's a different
  concept; only pair if one is fully contained AND short, e.g. `nse` ⊂
  `nseindia`)

The LLM judge still confirms. This is weaker than embeddings but better than v1.

## 4. Extend phase_d to canonicalize entities arrays

Currently `phase_d` rewrites `tags:`. ALSO rewrite the `entities:` array:

- Build an **entity canonical map** by stripping `#entity/` from the tag
  canonical map → maps raw entity strings to canonical entity strings.
  Example: if `#entity/artificial-intelligence → #entity/ai`, then the entity
  string `"Artificial Intelligence"` (or `"artificial-intelligence"`) →
  canonical `"AI"` (use the canonical's display form — title-case the slug
  sensibly, or keep a display-name lookup).
- For each note: map each entity in `entities:` through this map, dedupe,
  preserve order of first occurrence.
- Stamp `raw_meta.entities_canonicalized_at`.

This is the part that improves wikilinks: after canonicalization, more notes
share identical canonical entities → higher Jaccard overlap → denser wikilinks
when the wikilink builder re-runs.

**Idempotent:** skip notes already stamped `entities_canonicalized_at` matching
the current map version (store a map hash).

## 5. CLI

```bash
python -m workers.tag_dedup_backfill build-map --force-rebuild   # rebuild with embeddings
python -m workers.tag_dedup_backfill apply --reuse-map           # apply to tags AND entities
```

Add flags: `--embed-threshold 0.85`, `--no-embeddings` (force lexical fallback).

## 6. Tests (≤ 16)

- `test_embedding_candidates_pairs_semantic_near` (mock embeddings → known vectors)
- `test_embedding_candidates_respects_namespace_boundary`
- `test_embedding_candidates_threshold`
- `test_embeddings_cached_to_disk`
- `test_fallback_lexical_when_no_embedding_deployment`
- `test_acronym_matching_in_fallback`
- `test_phase_b_judge_vetoes_false_merge` (india/indian stays split)
- `test_phase_d_rewrites_tags`
- `test_phase_d_rewrites_entities_array`  ← key new test
- `test_phase_d_entity_dedup_preserves_order`
- `test_phase_d_idempotent_via_map_hash`
- `test_apply_skips_already_canonicalized`
- `test_dry_run_no_mutation`
- `test_build_map_reuses_embedding_cache`

## 7. Verification

```bash
.venv/bin/python -m pytest tests/ -v
.venv/bin/python -m ruff check connecting_dots/ workers/ tests/
.venv/bin/python -m workers.tag_dedup_backfill build-map --force-rebuild --dry-run | tail -20
```

Dry-run should show MANY more proposed merges than v1 (target: collapsing
~5,300 entity tags toward ~1,500-2,500).

## 8. After merge — the user/orchestrator runs

```bash
echo "AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-small" >> .env   # if they have it
set -a && source .env && set +a
python -m workers.tag_dedup_backfill build-map --force-rebuild   # ~3-5 min
python -m workers.tag_dedup_backfill apply --reuse-map           # rewrites tags + entities
python -m workers.wikilink_builder --threshold 0.08 --force      # Option B — denser links
```

## 9. Hard constraints

- Model: **sonnet**, isolation: **worktree**
- ≤ 50 tool calls (slightly higher — embeddings integration)
- ≤ 3 new files (1 embed helper maybe, 2 test files) — prefer extending
  existing `tag_dedup.py` over new modules
- ≤ 3 modified files (`tag_dedup.py`, `tag_dedup_backfill.py`, `.env.example`)
- ≤ 16 tests
- ≤ 600 LOC
- WIP fallback at 40 tool calls or 450 LOC
- Reuse existing Azure client; add embeddings call alongside
- No new Python deps (openai SDK already supports embeddings)

## 10. Commit + PR

```
feat(enrichment): embedding-based tag dedup + entity-array canonicalization
```

PR title: `Tag dedup v2: embedding candidates + canonicalize entities (denser wikilinks)`

## 11. Dispatch parameters

```yaml
subagent_type: "AI Engineer"
model: "sonnet"
isolation: "worktree"
description: "Tag dedup v2 — embeddings"
prompt: <full contents of this file>
```
