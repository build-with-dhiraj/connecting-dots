# Bundle 3 — Cross-source wikilinks (graph view transformation)

> See `docs/dispatches/README.md` for the dispatch protocol. Hard caps in §7.

## 1. Goal

Walk every vault note, find related notes via **entity overlap**, and write
`[[wikilink]]` references into a "Related notes" section at the bottom of each
note's body. Result: Obsidian's graph view (tags off) shows real semantic
clusters instead of a dot cloud.

**Cost:** ~$0 Azure (no LLM calls — pure entity-overlap math), ~$1.50-2.50 Claude.

## 2. Codebase context (do NOT Read these — content inlined below)

### Existing note frontmatter shape (from `lib/vault_writer/writer.py`)

Every note already has:

```yaml
---
source: whatsapp
handler: youtube
captured_at: '2026-05-28T...Z'
url: https://...
title: 'Clean noun-phrase title'
entities:
- Anthropic
- Claude
- Foundry
topics:
- ai engineering
- claude code tutorials
labels: []
tags:
- '#source/linkedin'
- '#ingest/whatsapp'
- '#entity/anthropic'
- '#topic/ai-engineering'
raw_meta:
  ner_enriched_at: '2026-05-28T...Z'
  ner_model: gpt-4.1
  ...
---

# Clean title

[body...]
```

Entities and topics arrays are populated on **1,464 / 1,464 notes** (Bundle 1
completed). You can rely on them.

### Existing atomic-write pattern (from `lib/vault_writer/writer.py:_write_note_atomic`)

The canonical pattern for vault file mutation:

```python
def _write_note_atomic(path: Path, fm: dict, body: str) -> None:
    """Atomic frontmatter+body write using tmp+rename."""
    new_text = "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n" + body
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(path)
```

Copy this pattern. Don't import from vault_writer if the worker is in `workers/` —
copy inline to avoid coupling.

### Vault note discovery (from `workers/ner_backfill.py:_iter_vault_notes`)

```python
def _iter_vault_notes(vault_root: Path) -> Iterable[Path]:
    """Yield every candidate .md under sources/ and inbox/, in stable order."""
    roots = [vault_root / "sources", vault_root / "inbox"]
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.md")):
            rel = path.relative_to(vault_root).as_posix()
            if rel in {"inbox/example.md"} or any(rel.startswith(p) for p in ("inbox/_failed/", "_failed/", ".trash/")):
                continue
            yield path
```

### Frontmatter parsing (use pyyaml; already a dep)

```python
def _split_frontmatter(text: str) -> tuple[dict | None, str]:
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text
    return yaml.safe_load(text[4:end]) or {}, text[end + 5:]
```

## 3. The algorithm

For each note `A`:

1. Read its `entities` array (list of strings).
2. Build a **co-occurrence index**: for each entity `e` in `A.entities`, find the
   set of OTHER notes that also have `e` in their entities array.
3. For each candidate note `B` (any note that shares ≥1 entity with `A`),
   compute the **Jaccard overlap**: `|A.entities ∩ B.entities| / |A.entities ∪ B.entities|`.
4. Keep candidates where Jaccard ≥ 0.15 (configurable via `--threshold`).
5. Sort by Jaccard desc.
6. Take **top-K** (default K=7, configurable via `--top-k`).
7. Write a "Related notes" section at the bottom of `A`'s body:

```markdown
## Related notes

- [[note title B1]] — 3 shared entities: Anthropic, Claude, Foundry
- [[note title B2]] — 2 shared entities: Anthropic, Claude
- [[note title B3]] — 2 shared entities: Anthropic, Foundry
- ...
```

The wikilink target is the note's **filename without `.md`** (Obsidian convention).
Don't use the title; use the slug filename. So if the file is
`vault/sources/linkedin/anthropic-just-dropped-...md`, the wikilink is
`[[anthropic-just-dropped-...]]`.

### Idempotency

Before writing, check if the body already has a `\n## Related notes\n` section
(case-sensitive). If so:

- **Default behaviour**: skip — don't re-compute or overwrite.
- **With `--force` flag**: replace the existing section with freshly-computed top-K.

This makes re-runs safe and cheap.

### Stamp in raw_meta

After writing, stamp `raw_meta.wikilinks_at = <ISO-now>` and `raw_meta.wikilinks_count = K`
so future workers can find re-link candidates if needed.

### Performance

For 1,464 notes × 5.9 avg entities ≈ 8,600 entity-note pairs. The co-occurrence
index is a single pass over all notes (build `entity → set[note_slug]` dict).
Then each note's candidate set is the union of `index[e]` over its entities.
Total work: O(N × avg_entities × avg_candidates_per_entity). Easily under 30s
for the whole vault on a laptop. No concurrency needed.

## 4. Files to create

- `connecting_dots/enrichment/edges.py` — pure-function module:
  - `build_entity_index(notes: list[ParsedNote]) -> dict[str, set[str]]`
  - `jaccard(a: set, b: set) -> float`
  - `find_related(note: ParsedNote, index, all_notes, threshold, top_k) -> list[Related]`
  - `Related = NamedTuple("Related", [("slug", str), ("title", str), ("score", float), ("shared", list[str])])`
- `workers/wikilink_builder.py` — orchestration:
  - CLI: `python -m workers.wikilink_builder [--threshold 0.15] [--top-k 7] [--limit N] [--force] [--dry-run]`
  - Loads all notes (parse frontmatter only — no body needed for the index pass)
  - Builds the entity index
  - For each note: computes top-K, mutates body, atomic-writes
  - Stamps `raw_meta.wikilinks_at`
  - Logs to stdout with tqdm progress bar
  - Final summary line: `Done. updated=X skipped=Y no_entities=Z`

## 5. Tests (≤ 12)

Use `pytest` with `tmp_path`. Synthesise small fake vaults.

- `tests/enrichment/test_edges.py`:
  - `test_jaccard_basic` — known sets
  - `test_jaccard_empty` — guards
  - `test_build_entity_index_groups_correctly`
  - `test_find_related_respects_threshold`
  - `test_find_related_top_k_truncation`
  - `test_find_related_sorted_by_score`
- `tests/workers/test_wikilink_builder.py`:
  - `test_writes_related_section`
  - `test_idempotent_skip_when_section_exists`
  - `test_force_replaces_existing_section`
  - `test_dry_run_no_mutation`
  - `test_skips_notes_with_no_entities`
  - `test_no_self_link`

## 6. Verification

```bash
.venv/bin/python -m pytest tests/ -v   # 377 → ~389 passing
.venv/bin/python -m ruff check connecting_dots/ workers/ tests/
.venv/bin/python -m workers.wikilink_builder --limit 5 --dry-run
```

Dry-run smoke output should list 5 notes with their would-be related notes.

## 7. Hard constraints

- Model: **sonnet** (not Opus)
- Isolation: **worktree**
- ≤ 45 tool calls
- ≤ 4 new files (2 modules + 2 test files)
- ≤ 1 modified file (`pyproject.toml` only if a new dep needed — but none should be)
- ≤ 12 tests
- ≤ 500 LOC
- WIP fallback at 35 tool calls or 400 LOC
- **No LLM calls anywhere.** This is pure Python set math.
- No new Python deps.

## 8. Commit + PR

Commit:
```
feat(enrichment): cross-source wikilinks via entity overlap (graph view clusters)
```

PR title:
```
Vault wikilinks: entity-overlap edges between related notes
```

PR body:
```markdown
## Summary

Builds a "Related notes" section at the bottom of each note via Jaccard
similarity over `entities` arrays. Default threshold 0.15, top-K 7. Idempotent;
re-runs skip unless `--force`.

## Algorithm

For note A, find notes B where Jaccard(A.entities, B.entities) ≥ threshold,
sorted by score, top-K. Write `[[note-slug]]` wikilinks with shared-entity
reasoning in the body's "Related notes" section.

## Why

Bundle 1 enriched notes with entities/topics but Obsidian's graph view doesn't
render those arrays as edges — only `[[wikilinks]]` and `tags:`. After this PR,
the graph view (tags-off) shows real semantic clusters for the first time.

## Tests

- 12 tests added
- Full suite: ~389 passing
- Ruff clean
- Dry-run smoke against vault passes

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

## 9. WIP fallback (§11 of README.md applies)

If at 35 tool calls or 400 LOC: commit + push as `WIP: Bundle 3 cross-source wikilinks`,
open the PR with title-prefix `WIP:`, list what's done vs missing in body.

## 10. Dispatch parameters

```yaml
subagent_type: "AI Engineer"
model: "sonnet"
isolation: "worktree"
description: "Bundle 3 — cross-source wikilinks"
prompt: <full contents of this file from §1 through §11>
```

## 11. After merge — run the worker

User runs (no LLM cost):
```bash
.venv/bin/python -m workers.wikilink_builder
```
~30s on the laptop. After this, open Obsidian, refresh graph view, marvel.
