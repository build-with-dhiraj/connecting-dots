# Vault README — curated welcome page + sidebar convention

> See `docs/dispatches/README.md` for the dispatch protocol.

## 1. Goal

Create a beautifully-formatted `vault/README.md` that opens by default in
Obsidian and serves as a navigation hub. Update folder structure subtly so
Obsidian's sidebar reads as a curated knowledge base, not a dump.

**Cost:** ~$0.50 Azure (gpt-4.1 to synthesise the overview prose), ~$1 Claude.

## 2. Why

Right now opening the vault in Obsidian shows an alphabetical folder list with
zero explanation of what's where. A great README answers:

- What is this vault?
- How is it organised?
- Where do I start?
- What's been processed and what's pending?

## 3. Codebase context

The current `vault/README.md` exists but is a stub (probably ~20 lines from the
Bundle 1 vault scaffolding). Replace it.

The vault structure as of now:

```
vault/
├── README.md                ← rewrite this
├── inbox/                   ← 307 notes (mixed, awaiting routing)
├── sources/
│   ├── youtube/             ← 37 notes
│   ├── linkedin/            ← 44 notes
│   ├── instagram/           ← 2 notes (URL-only — IG anon block)
│   ├── web/                 ← 1,075 notes (articles, blogs, X threads, GitHub, finance sites)
│   └── whatsapp/            ← 0 notes (would catch raw WA conversational text — currently routed to inbox/)
├── themes/                  ← 30 LLM-curated MoC pages + by-topic.base
├── digests/                 ← empty for now (Bundle 2 will populate)
└── .lancedb/                ← embeddings index (Track B PR B will populate)
```

Tag taxonomy (post tag-dedup if that's merged):
- `#source/<domain>` — where the link came from
- `#ingest/<channel>` — how it entered (whatsapp / mailto / linkedin-zip)
- `#entity/<name>` — entities NER extracted
- `#topic/<theme>` — topics NER extracted

## 4. The README structure

Generate `vault/README.md` with these sections:

```markdown
# Connecting Dots — Personal Knowledge Vault

> Last refreshed by `vault-readme` worker on YYYY-MM-DD.

## What this is

[2-3 sentences explaining: personal second-brain ingesting saves from WhatsApp /
YouTube / LinkedIn / Instagram / web; auto-enriched with entities, topics,
TL;DRs; curated into theme pages.]

## Where to start

- **Browse by theme** → [[by-topic.base]] — table view of every note, filterable
- **Best of finance** → [[financial-performance]], [[dividend-payout]], [[indian-stock-market]]
- **Best of product** → [[product-management]], [[design-systems]], [[hiring]]
- **Recent saves** → use Obsidian's "Files" sort by "Modified time desc"

## Vault size at a glance

| Metric | Count |
|---|---|
| Total notes | 1,464 |
| Web articles | 1,075 |
| WhatsApp messages | 307 |
| LinkedIn posts | 44 |
| YouTube transcripts | 37 |
| MoC theme pages | 30 |
| Unique entities tagged | ~5,800 |
| Unique topics tagged | ~2,900 |

## Folder map

[Inline the tree above, with one-line explanations]

## Tag conventions

[Inline the taxonomy above with examples]

## What's still pending

[Honest list — Bundle 2 digest not yet running, cross-source wikilinks coming,
real embeddings backfill pending, etc.]

## How content flows in

[Brief description of the ingest paths — WA test number, mailto fallback,
LinkedIn ZIP watcher, WhatsApp export watcher]
```

Use the LLM only for the "What this is" paragraph and any synthesis. The rest
is templated.

## 5. Files to create / modify

- Modify: `vault/README.md` — full rewrite
- New: `connecting_dots/enrichment/vault_readme_synth.py` — minimal helper
  that calls gpt-4.1 for the "What this is" paragraph (so the worker can
  regenerate it later without redesign)
- New: `workers/vault_readme_refresh.py` — CLI that regenerates the README:
  - Counts notes per folder
  - Reads recent stats from data/ner_traces.jsonl (optional)
  - Calls LLM for the synthesis paragraph
  - Templates the rest
- New: `tests/workers/test_vault_readme_refresh.py` — happy path + idempotency

## 6. CLI

```bash
python -m workers.vault_readme_refresh                  # regenerate
python -m workers.vault_readme_refresh --no-llm         # use cached / templated paragraph
python -m workers.vault_readme_refresh --dry-run        # preview, no write
```

## 7. Idempotency

The README is fully regenerated each run — no incremental state. Cheap because
it's one LLM call (~$0.001) plus templating.

## 8. Tests (≤ 8)

- `test_counts_notes_per_folder_correctly`
- `test_templates_metric_table`
- `test_llm_call_mocked_for_synthesis`
- `test_no_llm_flag_uses_static_paragraph`
- `test_dry_run_writes_nothing`
- `test_atomic_write`
- `test_preserves_obsidian_friendly_wikilink_targets`
- `test_handles_empty_folder_gracefully`

## 9. Hard constraints

- Model: **sonnet**, isolation: **worktree**
- ≤ 45 tool calls
- ≤ 4 new files (1 module + 1 worker + 1 test file + 1 modified README)
- ≤ 1 modified file (vault/README.md)
- ≤ 8 tests
- ≤ 350 LOC
- WIP fallback at 30 tool calls or 280 LOC
- Reuse existing Azure client
- No new Python deps

## 10. Commit + PR

```
feat(vault): curated README + refresh worker (sidebar gets a homepage)
```

PR title: `Vault README: curated welcome page + folder map + tag conventions`

## 11. Dispatch parameters

```yaml
subagent_type: "AI Engineer"
model: "sonnet"
isolation: "worktree"
description: "Vault README"
prompt: <full file>
```

## 12. After merge — run

```bash
python -m workers.vault_readme_refresh
```

Open Obsidian, click `vault/README.md` (or set it as the default startup file
in Obsidian settings → Files & links → Default location for new attachments
isn't relevant, but in Obsidian settings → Editor → "Default new tab location"
+ Workspace presets, configure README as homepage).
