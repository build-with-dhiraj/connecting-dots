# Untitled Note fix — smarter fallback for attachment-only notes

> See `docs/dispatches/README.md` for the dispatch protocol.

## 1. Goal

Re-title the **352 notes currently named "Untitled Note"** using a smarter
fallback chain. These are notes where the body was just an attachment marker
(`<attached: IMG-...>`, voice note references, etc.) so the title rewriter had
nothing to work with.

Replace "Untitled Note" with one of:

1. **Filename-derived title** if `raw_meta.media_filename` exists (e.g.,
   `Image: IMG-20250115-WA0001` → cleaned to `Image from January 15`)
2. **Entity/topic-derived title** if entities/topics exist on the note
   (e.g., `Note about #anthropic #ai-engineering`)
3. **Date+source fallback** if neither (e.g., `WhatsApp note from 2025-01-15`)

**Cost:** ~$0.50-1 Azure (gpt-4.1 to pick a clean phrase), ~$1.50 Claude.

## 2. Why

352 "Untitled Note" entries clutter the Obsidian sidebar and tag browser. We
have enough metadata on each note (filename, entities, captured_at) to do
better than the LLM's empty-body default.

## 3. Codebase context

The existing title rewriter lives at:

- `connecting_dots/enrichment/title.py` — has the LLM call + tool schema
- `workers/title_backfill.py` — orchestration worker

You'll extend `title.py` with a new function `rewrite_untitled(...)` and add a
new CLI mode to the worker. **Do NOT redesign the existing title flow** — only
add the fallback path for notes where the original rewrite produced "Untitled Note".

### Existing tool schema (in `connecting_dots/enrichment/title.py`)

```python
_TITLE_TOOL = {
    "type": "function",
    "function": {
        "name": "record_title",
        "description": "Record the rewritten title.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Clean 5-12 word noun phrase title."},
                "reason": {"type": "string"}
            },
            "required": ["title"]
        }
    }
}
```

Reuse this. Same model, same caching, same atomic write.

## 4. The smarter fallback chain

For each note where `title == "Untitled Note"` and `raw_meta.original_title`
is set (meaning the rewriter already touched it):

```python
def derive_better_title(fm: dict) -> str:
    """Return a more meaningful title than 'Untitled Note'."""

    # Layer 1: filename signal
    media_filename = (fm.get("raw_meta") or {}).get("media_filename")
    media_type = ((fm.get("raw_meta") or {}).get("message_type") or "")
    if media_filename:
        # e.g. "IMG-20250115-WA0001.jpg" → "Image from 15 Jan 2025"
        parsed = parse_wa_media_filename(media_filename)
        if parsed:
            return f"{parsed['kind']} from {parsed['date_human']}"

    # Layer 2: entity/topic signal — call LLM with a short prompt
    ents = fm.get("entities") or []
    tops = fm.get("topics") or []
    if ents or tops:
        # Call gpt-4.1 with the entities + topics, ask for a 5-8 word title
        return call_llm_for_title(entities=ents, topics=tops, source=fm.get("source"))

    # Layer 3: captured_at + source
    captured = fm.get("captured_at", "")
    source = fm.get("source", "note")
    return f"{source.title()} note from {captured[:10]}"
```

### `parse_wa_media_filename` helper

WhatsApp media files follow predictable patterns:

- `IMG-20250115-WA0001.jpg` → `{kind: "Image", date_human: "15 Jan 2025"}`
- `AUD-2025-10-31-22-18-59.mp3` → `{kind: "Audio", date_human: "31 Oct 2025"}`
- `PTT-20260227-WA0042.opus` → `{kind: "Voice note", date_human: "27 Feb 2026"}`
- `VID-20250715-WA0008.mp4` → `{kind: "Video", date_human: "15 Jul 2025"}`
- `DOC-...filename.pdf` → `{kind: "Document", date_human: ... if extractable else "" }`
- `00000337-Resume_Saksham.pdf` → `{kind: "Document", date_human: "" }` (no date in name)

Implement as a regex with fallback to "" for date.

### Layer 2 LLM call

Use the existing `connecting_dots/enrichment/ner.py` client pattern. Prompt:

```
You are titling a saved note that has no readable body. Generate a clean
5-8 word noun phrase title using the entities and topics below.

Entities: {entities}
Topics: {topics}
Source platform: {source}

Examples of good titles:
- "Anthropic Claude Code free course notes"
- "Bharat Electronics quarterly results brief"
- "Stripe pricing page reference"

Examples of bad titles:
- "Note about Anthropic" (lazy)
- "Untitled Note" (forbidden)
- "WhatsApp message" (uninformative)

Tool: `record_title(title: string)`.
```

## 5. CLI

Add a flag to existing `workers/title_backfill.py`:

```bash
# Default behaviour (unchanged): rewrite garbage titles using body content
python -m workers.title_backfill

# NEW: re-title only the "Untitled Note" residue with smarter fallback
python -m workers.title_backfill --fix-untitled [--limit N] [--dry-run]
```

## 6. Idempotency

After rewrite, stamp `raw_meta.title_v2_at` and `raw_meta.title_v2_source`
(values like `"filename"`, `"entity-llm"`, `"date-fallback"`) so re-runs skip
notes already fixed.

## 7. Files to create / modify

- Modify: `connecting_dots/enrichment/title.py` — add `derive_better_title()`,
  `parse_wa_media_filename()`, helper functions
- Modify: `workers/title_backfill.py` — add `--fix-untitled` mode + flag handling
- New: `tests/enrichment/test_untitled_fix.py` — fallback chain coverage

That's **2 modified, 1 new** — well under the cap.

## 8. Tests (≤ 12)

- `test_parse_image_filename`
- `test_parse_voice_note_filename`
- `test_parse_document_filename_no_date`
- `test_parse_unknown_filename_returns_none`
- `test_filename_layer_used_when_media_filename_present`
- `test_entity_topic_layer_calls_llm_with_correct_prompt` (mocked)
- `test_date_fallback_when_no_entities`
- `test_fix_untitled_skips_when_already_v2_stamped`
- `test_fix_untitled_skips_non_untitled_notes`
- `test_dry_run_no_mutation`
- `test_atomic_write_preserves_other_frontmatter`
- `test_cli_fix_untitled_flag_routes_correctly`

## 9. Hard constraints

- Model: **sonnet**, isolation: **worktree**
- ≤ 45 tool calls
- ≤ 1 new file (test only)
- ≤ 2 modified files
- ≤ 12 tests
- ≤ 400 LOC
- WIP fallback at 35 tool calls
- No new Python deps
- Reuse existing Azure client; no new SDK init

## 10. Commit + PR

```
feat(enrichment): smarter Untitled Note fallback via filename + entity LLM
```

PR title: `Fix 352 "Untitled Note" entries with filename + entity fallback`

## 11. Dispatch parameters

```yaml
subagent_type: "AI Engineer"
model: "sonnet"
isolation: "worktree"
description: "Untitled Note fix"
prompt: <full file>
```

## 12. After merge — run

```bash
python -m workers.title_backfill --fix-untitled
```
~5 min, ~$0.50 Azure. Sidebar gets meaningful titles instead of 352 "Untitled Note".
