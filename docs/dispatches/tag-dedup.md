# Tag dedup — collapse semantic-duplicate tags

> See `docs/dispatches/README.md` for the dispatch protocol.

## 1. Goal

Reduce **5,838 unique entity tags** and **2,960 unique topic tags** to a clean
canonical set by collapsing semantic duplicates. Examples:

- `#entity/ai`, `#entity/AI`, `#entity/artificial-intelligence`, `#entity/a-i`
  → `#entity/ai`
- `#topic/ai-engineering`, `#topic/AI-engineering`, `#topic/ai engineering`,
  `#topic/ai-eng`
  → `#topic/ai-engineering`
- `#entity/india`, `#entity/India`, `#entity/india-`
  → `#entity/india`

**Cost:** ~$1-2 Azure (LLM as equivalence judge), ~$1.50 Claude.

## 2. Why

Current state: `#*ai*` has 134 variants. `#*indian*` has 79. This bloats the
graph view and makes filtering by tag useless. After dedup: probably ~1,500
unique entity tags and ~800 unique topic tags. Much cleaner.

## 3. Codebase context (no need to Read; inlined)

Same vault structure as bundle-3. Tags live in frontmatter as a `tags:` list:

```yaml
tags:
- '#source/linkedin'
- '#ingest/whatsapp'
- '#entity/ai'
- '#entity/artificial-intelligence'   # duplicate
- '#topic/ai-engineering'
- '#topic/AI Engineering'              # duplicate
```

The Azure OpenAI client is at `connecting_dots/enrichment/ner.py:_get_client()` —
import or copy it. Same `AZURE_OPENAI_*` env vars.

## 4. The algorithm

### Phase A — group candidates by slug similarity (no LLM)

1. Collect every unique tag across the vault.
2. For each tag, compute a **normalized key**: lowercase, strip non-alphanumeric,
   collapse whitespace, sort the words. Example:
   - `#entity/AI`, `#entity/ai`, `#entity/a-i` → key `ai`
   - `#topic/AI Engineering`, `#topic/ai-engineering`, `#topic/ai eng` → key
     `engineering ai` (sorted-words) — close enough to merge with `engineering ai`
3. Group tags by normalized key.
4. Within each group, pick the **shortest-lexicographically-smallest** as canonical.

For obvious cases (case differences, hyphen vs space, plural/singular `s`),
this resolves without an LLM.

### Phase B — LLM-judge for ambiguous merges (Azure gpt-4.1)

For pairs of tags that share **partial words** but not the exact normalized key
(e.g., `#entity/india` vs `#entity/indian`, `#entity/anthropic` vs
`#entity/anthropic-pbc`), batch them and ask gpt-4.1 in groups of ~30:

```
Given these tag pairs, tell me which pairs are semantic duplicates
(referring to the same entity/concept) and which are NOT.

Pairs:
1. india ↔ indian
2. anthropic ↔ anthropic-pbc
3. nse ↔ nseindia
4. ...

Return a JSON list: [{"pair_index": 1, "duplicate": true, "canonical": "india"}, ...]
```

Use OpenAI tool-calling for structured output. Same pattern as
`connecting_dots/enrichment/ner.py`.

### Phase C — build a mapping table

Combine Phase A + B into a single mapping `dict[str, str]` from raw tag → canonical
tag. Save to `data/tag_canonical_map.json` so re-runs don't redo LLM calls.

### Phase D — rewrite vault frontmatter

For each note: read `tags:`, map each through the canonical table, dedupe, sort,
write back atomically.

## 5. Files to create

- `connecting_dots/enrichment/tag_dedup.py` — phases A + B + C as pure functions
- `workers/tag_dedup_backfill.py` — orchestration CLI
  - Subcommands: `build-map` (phases A-C), `apply` (phase D), `all` (default)
  - Flags: `--limit N`, `--dry-run`, `--reuse-map` (skip LLM if `data/tag_canonical_map.json` exists)
- `data/tag_canonical_map.json` (generated, gitignored) — cached mapping
- `tests/enrichment/test_tag_dedup.py` — phases A & B & D
- `tests/workers/test_tag_dedup_backfill.py` — end-to-end on tmp vault

## 6. CLI behaviour

```bash
# Build the canonical map (Phase A + B), cache to disk
python -m workers.tag_dedup_backfill build-map

# Inspect the proposed map before applying
python -m workers.tag_dedup_backfill build-map --dry-run | head

# Apply the cached map to vault
python -m workers.tag_dedup_backfill apply

# Or in one shot, reusing cache if present
python -m workers.tag_dedup_backfill all --reuse-map
```

## 7. Idempotency

- The `data/tag_canonical_map.json` cache means LLM calls only happen once
  (until you delete the file or pass `--force-rebuild`).
- Vault frontmatter rewrite is naturally idempotent: applying the same map
  twice produces the same output.

## 8. Tests (≤ 14)

- `test_normalize_key_strips_case_and_punct`
- `test_normalize_key_sorts_words`
- `test_phase_a_groups_obvious_duplicates`
- `test_phase_a_picks_canonical_consistently`
- `test_phase_b_llm_call_mocked` (mock Azure)
- `test_phase_b_returns_pairs_decision`
- `test_phase_c_combines_phases`
- `test_phase_c_caches_to_disk`
- `test_phase_d_rewrites_tags`
- `test_phase_d_idempotent_on_rerun`
- `test_apply_skips_notes_with_no_tags`
- `test_dry_run_no_mutation`

## 9. Hard constraints

- Model: **sonnet**, isolation: **worktree**
- ≤ 45 tool calls, ≤ 5 new files, ≤ 1 modified (`.gitignore` only if needed)
- ≤ 14 tests
- ≤ 600 LOC
- WIP fallback at 35 tool calls or 450 LOC
- No new Python deps — use existing `openai`, `pyyaml`
- Cap LLM call cost: emit a warning if proposed Phase-B calls exceed 100 batches
  (~$2 of Azure)

## 10. Commit + PR

```
feat(enrichment): tag dedup via slug-merge + LLM-judge for ambiguous pairs
```

PR title: `Tag dedup: collapse 8,798 unique tags to canonical set`

PR body:
```markdown
## Summary

Two-phase tag dedup over the vault:
- Phase A: deterministic merge of case/punctuation/word-order variants
- Phase B: Azure gpt-4.1 judges ambiguous pairs (india ↔ indian, etc.)
- Phase C: cached mapping at `data/tag_canonical_map.json`
- Phase D: atomic frontmatter rewrite

## Expected reduction

- Entity tags: 5,838 → ~1,500
- Topic tags: 2,960 → ~800

## Cost

~$1-2 Azure for Phase B (one-time, then cached).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

## 11. Dispatch parameters

```yaml
subagent_type: "AI Engineer"
model: "sonnet"
isolation: "worktree"
description: "Tag dedup"
prompt: <full contents of this file>
```

## 12. After merge — run

```bash
python -m workers.tag_dedup_backfill build-map        # ~3 min, ~$1.50 Azure
python -m workers.tag_dedup_backfill apply --reuse-map  # ~10 sec, no LLM
```

Refresh Obsidian. Tag panel collapses dramatically; graph view becomes legible.
