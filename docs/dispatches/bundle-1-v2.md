# Bundle 1 v2 — Vault enrichment dispatch spec

> **For the fresh-chat orchestrator (Haiku 4.5):** Read this file in full, then dispatch
> the AI Engineer subagent with the brief described in §10. Use `isolation: "worktree"`
> and `model: "sonnet"`. Monitor for completion, then report the PR URL to the user.
>
> **For the dispatched AI Engineer:** Implement exactly what §3-9 describe. Honour all
> hard constraints in §6. Use WIP fallback (§11) if context tightens.

---

## 1. Goal

Three vault-enrichment features in one PR off `main`:

1. **MoC (Map of Content) generator** — one beautifully-formatted markdown file per top topic, listing the best notes + a one-paragraph synthesis written by Azure OpenAI gpt-4.1.
2. **Smart title rewriter** — replaces ugly titles like `1png-attached-00000104-1png.md` with meaningful ones like `Whiteboard photo from Q3 product strategy`.
3. **2-sentence TL;DR** — prepended to every note body for instant scanability.

All three are vault-mutation features that use the same Azure OpenAI extractor pattern already proven in `connecting_dots/enrichment/ner.py`.

---

## 2. Codebase context (read these BEFORE writing anything new)

| Path | What it teaches |
|---|---|
| `connecting_dots/enrichment/ner.py` | Azure OpenAI client init, prompt caching pattern, tool-calling for structured output, error handling, `NERResult` shape |
| `connecting_dots/enrichment/tracer.py` | Local JSONL trace format — extend the same format for the new features |
| `workers/ner_backfill.py` | Async backfill pattern: discover → filter → batch → atomic write → idempotency check. **Copy this structure for the new workers.** |
| `lib/vault_writer/writer.py` | Atomic write pattern (`O_EXCL` + tmp+rename + fsync dir). Frontmatter parsing & merging. |
| `connecting_dots/enrichment/judge.py` | LLM-as-judge pattern for quality gates |
| `pyproject.toml` | Existing deps: `openai>=1.50`, `pyyaml`, `tqdm`. **No new deps needed.** |
| `.env.example` | `AZURE_OPENAI_*` variables already present |
| `vault/themes/by-topic.base` | Obsidian Bases file already exists in `vault/themes/` — your MoC files go in same folder |

**Sample real notes** (read 1-2 of each to calibrate prompts):

- Rich content: `vault/sources/linkedin/anthropic-just-dropped-24-free-claude-code-talks11-hrs-32-mins-of-free-education.md`
- Bad title: `vault/inbox/1png-attached-00000104-1png.md`, `vault/inbox/whatsapp-audio-3.md`
- Long YouTube transcript: `vault/sources/youtube/rick-astley-never-gonna-give-you-up-official-video-4k-remaster.md`
- Web article: any file under `vault/sources/web/` (1,075 files)

---

## 3. Feature 1 — MoC generator

### Files to create

- `connecting_dots/enrichment/moc.py` — extractor + synthesis prompt logic
- `workers/moc_generator.py` — one-shot CLI that walks all topics, picks top-N notes per topic, calls Azure to synthesize

### Behaviour

1. Walk the vault. Collect every `#topic/*` tag and count notes per topic.
2. Pick the top **30 topics** by note count (configurable via `--top-n`).
3. For each topic:
   - Gather up to **20 notes** with that tag, sorted by `captured_at` desc.
   - Build a prompt: instruction prefix (cached) + topic name + the 20 notes (title + first 200 chars of body each).
   - Azure OpenAI gpt-4.1 returns: a 2-3 paragraph synthesis + a list of "essential" notes (the ones worth highlighting at top).
   - Write to `vault/themes/<topic-slug>.md` with structured frontmatter and a curated body.

### Output file template

```yaml
---
type: moc
topic: ai-engineering
generated_at: '2026-05-28T...Z'
note_count: 142
model: gpt-4.1
---

# AI Engineering

[2-3 paragraph LLM synthesis explaining what your saves on this topic cover and
how they connect to each other.]

## Essential reading

- [[note title 1]] — one-line reason
- [[note title 2]] — one-line reason
- ... up to 5-7 essential notes

## All notes ({{count}})

- [[note 1]]
- [[note 2]]
- ... full list
```

### CLI

```bash
python -m workers.moc_generator [--top-n 30] [--min-notes 5] [--dry-run]
```

### Idempotency

If `vault/themes/<topic-slug>.md` exists and `generated_at` is within the last 7 days, skip unless `--force`.

---

## 4. Feature 2 — Smart title rewriter

### Files to create

- `connecting_dots/enrichment/title.py` — title-quality detector + rewriter
- `workers/title_backfill.py` — async backfill worker

### Behaviour

1. Walk every note in `vault/sources/` and `vault/inbox/` (skip `_failed/` and `example.md`).
2. For each note, decide if title needs rewriting using a simple rule:
   - Title is empty, < 5 chars, looks like a filename (matches `r'^\d+png|^IMG-|^AUD-|^PTT-|^VID-|^DOC-|^\.[a-z]+$'`), or starts with `http`
   - OR title contains `<attached:`, `‎` (LTR mark), or is mostly slugified-URL noise
3. For those notes, call Azure with: title + first 800 chars of body → returns a clean 5-12 word title.
4. Atomic-write the new title back to frontmatter. Stash the old one in `raw_meta.original_title` so it's recoverable.

### CLI

```bash
python -m workers.title_backfill [--limit N] [--concurrency 4] [--dry-run]
```

### Idempotency

Skip notes where `raw_meta.original_title` is already set (means we've already rewritten).

### Quality bar

- The new title should be a noun phrase, 5-12 words
- Should NOT include the word "WhatsApp" or "Note" or other channel-name noise
- Should hint at the subject matter

### Tool/function-call schema

Use Azure OpenAI tool-use exactly like `ner.py` does. The tool:

```json
{
  "name": "record_title",
  "description": "Record the rewritten title.",
  "parameters": {
    "type": "object",
    "properties": {
      "title": {"type": "string", "description": "Clean 5-12 word noun phrase title."},
      "reason": {"type": "string", "description": "One short sentence on why this title."}
    },
    "required": ["title"]
  }
}
```

---

## 5. Feature 3 — 2-sentence TL;DR

### Files to create

- `connecting_dots/enrichment/tldr.py` — TL;DR extractor
- `workers/tldr_backfill.py` — async backfill worker

### Behaviour

1. Walk every note.
2. Skip if `raw_meta.tldr_at` is set (idempotent).
3. Skip if body length < 200 chars (no point summarising short notes).
4. Call Azure with the body (truncated to 4000 chars) → returns exactly 2 sentences.
5. Prepend the TL;DR to the body in this format:

```markdown
---
[unchanged frontmatter]
---

> **TL;DR.** Sentence one. Sentence two.

# [original title]

[original body]
```

6. Atomic-write. Stamp `raw_meta.tldr_at` + `raw_meta.tldr_model`.

### Quality bar

- Exactly 2 sentences
- First sentence: what is this about
- Second sentence: the key insight / takeaway / actionable signal

### Tool/function-call schema

```json
{
  "name": "record_tldr",
  "description": "Record the 2-sentence TL;DR.",
  "parameters": {
    "type": "object",
    "properties": {
      "sentence_1": {"type": "string"},
      "sentence_2": {"type": "string"}
    },
    "required": ["sentence_1", "sentence_2"]
  }
}
```

### CLI

```bash
python -m workers.tldr_backfill [--limit N] [--concurrency 4] [--dry-run]
```

---

## 6. Hard constraints (DO NOT EXCEED)

| Constraint | Limit |
|---|---|
| New files | ≤ 7 |
| Modified files | ≤ 2 (`pyproject.toml` if needed, `connecting_dots/enrichment/__init__.py` to re-export) |
| New tests | ≤ 25 total across all three features |
| Tool calls during dispatch | ≤ 45 |
| Lines of code (excluding tests) | ≤ 800 |
| New Python dependencies | **0** — use existing `openai`, `pyyaml`, `tqdm` |
| Cloud observability | **NONE.** No langfuse. Local JSONL traces only (extend `tracer.py`). |
| Schema changes outside frontmatter | **0** — do not touch `schemas/inbound_envelope.schema.json` |
| Code outside `connecting_dots/enrichment/` and `workers/` | **0** |

If any limit feels tight: drop test coverage first, then defer Feature 3 (TL;DR) for a follow-up PR. **DO NOT skip Features 1 or 2 — they're the user-visible wins.**

---

## 7. Test budget allocation

- MoC: 8-10 tests (matching, prompt construction, idempotency, dry-run, single-topic end-to-end)
- Smart titles: 8-10 tests (regex detector, prompt construction, frontmatter mutation, idempotency, dry-run)
- TL;DR: 5-8 tests (sentence count, prepend logic, length skip, idempotency, dry-run)

Use mocks for the Azure SDK. **No live API calls in tests.** Pattern: `tests/enrichment/test_ner.py`.

---

## 8. Verification (run before commit)

```bash
.venv/bin/python -m pytest tests/ -v  # all green, was 335 baseline
.venv/bin/python -m ruff check connecting_dots/ workers/ tests/
npx tsc --noEmit  # should remain clean (no TS changes expected)
```

Then a SMOKE TEST (dry-run, no actual writes):
```bash
.venv/bin/python -m workers.moc_generator --top-n 3 --dry-run
.venv/bin/python -m workers.title_backfill --limit 5 --dry-run
.venv/bin/python -m workers.tldr_backfill --limit 5 --dry-run
```

All three should print what they would do, without mutating any vault file. If dry-run smoke passes, the PR is good to merge.

---

## 9. Commit + PR format

**Commit:** single conventional commit
```
feat(enrichment): MoC generator + smart titles + TL;DR (vault gets pretty)
```

**PR title:**
```
Vault enrichment: MoC pages, smart titles, 2-sentence TL;DRs
```

**PR body template:**
```markdown
## Summary

- MoC generator writes `vault/themes/<topic>.md` for top 30 topics — LLM synthesis + curated note list
- Smart title rewriter detects garbage titles (regex) and replaces with clean noun phrases
- 2-sentence TL;DR prepended to every note body > 200 chars

All three use Azure OpenAI gpt-4.1 via the existing tool-use pattern from `connecting_dots/enrichment/ner.py`. Local JSONL traces only — no cloud observability.

## Tests

- N tests added (X NER + Y title + Z tldr)
- Full suite: ~360 passing
- Ruff clean
- Dry-run smoke for all three workers passes

## Cost forecast

- MoC: 30 topics × ~$0.05 = ~$1.50
- Smart titles: ~500 garbage titles × ~$0.003 = ~$1.50
- TL;DR: ~1,400 notes × ~$0.005 = ~$7
- Total Azure: ~$10 for full backfill

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

---

## 10. Dispatch parameters (for the orchestrator)

```
subagent_type: "AI Engineer"
model: "sonnet"           # NOT opus — anti-pattern flagged by token-coach
isolation: "worktree"     # clean rollback if session dies
description: "Bundle 1 v2 — vault enrichment"
prompt: <contents of this file from §1 through §11>
```

After dispatch, the orchestrator's job is just to:
1. Wait for completion notification
2. Read the agent's final report (don't tail the JSONL transcript — it overflows context)
3. Report the PR URL + a 3-line summary to the user

---

## 11. WIP fallback (for the dispatched agent)

If at ANY point during your work you've used **35 tool calls** or written **600 lines**, treat that as a hard signal. Stop. Do this:

1. Run `pytest` quickly to see what passes
2. Stage everything that's complete (whole features only — don't ship half a feature)
3. Commit with prefix `WIP: ` on the conventional commit
4. Push the branch
5. Open the PR with title prefixed `WIP:` and clearly list what's done vs. pending in the body
6. Return your report. Even a single feature fully shipped is a win.

Do not try to "just finish one more thing" past your budget. The orchestrator can dispatch a small follow-up to complete missing features in a clean session.

---

## 12. What success looks like

After the user merges this PR and runs the three backfill workers (Azure cost ~$10):

- `vault/themes/` has 30 beautiful MoC pages, one per top topic
- ~500 previously-ugly titles are now meaningful noun phrases
- Every long note opens with a 2-sentence TL;DR at the top
- Obsidian becomes a curated knowledge base instead of a raw scrape dump

That's the visual transformation. **Bundle 2 (daily digest) follows; Bundle 3 (embeddings + edges) after that.**
