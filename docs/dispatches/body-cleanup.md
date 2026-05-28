# Body cleanup — strip ad/nav cruft from web-scraped notes

> See `docs/dispatches/README.md` for the dispatch protocol.

## 1. Goal

Walk every `vault/sources/web/` note (1,075 notes) and remove the ad copy,
navigation menus, cookie banners, "Subscribe to our newsletter" blurbs,
"Related articles" sidebars, and footer cruft that trafilatura's extraction
left behind. Output: clean readable note bodies.

**Cost:** ~$5 Azure (gpt-4.1 with caching), ~$2 Claude.

## 2. Why

Sample bad body (current state):

```
[Cookie Banner]
By clicking "Accept all cookies", you agree to the storing of cookies...
Cookie Settings | Accept All Cookies

# Real article title

Real article paragraph 1...

[Newsletter signup]
Subscribe to our newsletter!
Email: [______] [Subscribe]

Real article paragraph 2...

[Footer]
© 2026 Company. Privacy | Terms | About | Contact
```

After cleanup: only the real article content remains.

## 3. Codebase context

The TL;DR worker (`workers/tldr_backfill.py`) and title rewriter
(`workers/title_backfill.py`) already follow the same pattern. Copy that
structure. Azure OpenAI gpt-4.1 with the prompt caching pattern from
`connecting_dots/enrichment/ner.py`.

## 4. The algorithm

Per note:

1. Skip if `raw_meta.handler != "web"` (only clean web-scraped notes).
2. Skip if body is < 800 chars (too short to have cruft worth cleaning).
3. Skip if `raw_meta.body_cleaned_at` is set (idempotent).
4. Call gpt-4.1 with the body + this system prompt:

```
You are cleaning up a web-scraped article. Remove:
- Cookie banners and consent prompts
- Navigation menus and header/footer chrome
- Newsletter subscription CTAs
- "Related articles" / "You might also like" lists
- Comment forms and sections
- Author bio boilerplate
- Social share button text
- Pagination links
- Advertisement copy

Preserve:
- The article's main content (paragraphs, headings, lists, code blocks, blockquotes)
- The article's author byline if it's part of the prose, not a separate widget
- Inline links within sentences (keep markdown link syntax)
- The article's images (markdown image syntax)

Output: clean markdown with only the article content. Preserve original
headings hierarchy.
```

Use tool-calling to enforce structured output:

```python
{
    "type": "function",
    "function": {
        "name": "record_cleaned_body",
        "parameters": {
            "type": "object",
            "properties": {
                "cleaned_markdown": {"type": "string"},
                "removed_kinds": {
                    "type": "array",
                    "items": {"enum": ["cookie", "navigation", "newsletter",
                                       "related-articles", "comments", "author-bio",
                                       "social-share", "pagination", "ad", "other"]}
                },
                "removed_count": {"type": "integer"}
            },
            "required": ["cleaned_markdown"]
        }
    }
}
```

5. If `cleaned_markdown` is shorter than the original by **more than 90%**,
   skip (LLM probably hallucinated). Stamp `raw_meta.body_cleanup_skipped: "suspicious_truncation"`.
6. Otherwise: atomic-replace the body, prepend frontmatter, stamp
   `raw_meta.body_cleaned_at` and `raw_meta.body_cleaned_removed: <list>`.
7. Preserve any existing TL;DR blockquote at the top — don't let the LLM eat it.

### TL;DR preservation

If the body starts with `> **TL;DR.**`, extract those lines BEFORE sending the
rest to the LLM. Re-prepend after the LLM returns clean content. Then re-prepend
the H1 title. Final order: TL;DR > H1 > cleaned body.

## 5. CLI

```bash
python -m workers.body_cleanup_backfill [--limit N] [--concurrency 3] [--dry-run]
```

Concurrency default 3 (Azure rate-friendly).

## 6. Idempotency

Stamp `raw_meta.body_cleaned_at`. Skip on re-run.

## 7. Files to create

- `connecting_dots/enrichment/body_cleanup.py` — LLM extractor + tool schema
- `workers/body_cleanup_backfill.py` — async backfill (mirror `tldr_backfill.py`)
- `tests/enrichment/test_body_cleanup.py`
- `tests/workers/test_body_cleanup_backfill.py`

## 8. Tests (≤ 12)

- `test_extract_skips_non_web_handler`
- `test_extract_skips_short_body`
- `test_extract_skips_already_cleaned`
- `test_preserves_tldr_blockquote_at_top`
- `test_preserves_h1_title`
- `test_suspicious_truncation_skips_write`
- `test_atomic_write_preserves_frontmatter`
- `test_concurrency_default_3`
- `test_dry_run_no_mutation`
- `test_llm_response_parsing_mocked`
- `test_removed_kinds_stamped_in_raw_meta`
- `test_resumes_after_partial_run`

## 9. Hard constraints

- Model: **sonnet**, isolation: **worktree**
- ≤ 45 tool calls
- ≤ 4 new files (2 modules + 2 test files)
- ≤ 1 modified (none expected; `pyproject.toml` only if needed)
- ≤ 12 tests
- ≤ 600 LOC
- WIP fallback at 35 tool calls or 450 LOC
- Reuse existing Azure client; no new SDK init
- Concurrency=3 default (lower than NER's 4 — body cleanup is larger inputs)

## 10. Commit + PR

```
feat(enrichment): web body cleanup — strip cookie/nav/newsletter cruft via LLM
```

PR title: `Body cleanup: strip web-scrape cruft from 1,075 web notes`

## 11. Dispatch parameters

```yaml
subagent_type: "AI Engineer"
model: "sonnet"
isolation: "worktree"
description: "Body cleanup"
prompt: <full file>
```

## 12. After merge — run

```bash
python -m workers.body_cleanup_backfill --limit 5 --dry-run  # sanity check
python -m workers.body_cleanup_backfill                       # full run
```

~35 min wall time, ~$5 Azure. After: web articles read like articles, not
templated junk.
