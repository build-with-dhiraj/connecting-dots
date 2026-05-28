"""Claude-based NER + topic extractor with prompt caching and tool-use.

Design notes
============

**Why Claude tool-use, not a JSON schema or regex parse?**

Tool-use forces structured output through the same code path that handles
function-calling — Claude returns a `tool_use` block with already-parsed JSON.
We declare a single tool `record_extraction(entities, topics)` with a strict
input_schema; the model can only respond by calling this tool. No "the JSON
sometimes has trailing commas / missing closing brace" failure mode.

**Why prompt caching?**

The instruction prefix + few-shot examples are ~2000 tokens. At 1,464 notes
to backfill, that's 2.9M repeated tokens. With `cache_control={"type": "ephemeral"}`
on the last system block, the first call writes the cache (~1.25x input price)
and every subsequent call reads it at ~10% of input price. Effective savings:
~70% of the prefix cost. The per-note variable content (title + body slice) is
appended *after* the cache breakpoint so it never invalidates the cached prefix.

Render order is `tools` → `system` → `messages`. We mark the last system block,
which caches both `tools` and `system` together. The tool definition is itself
stable (no per-call variation), so it benefits from the same cache entry. The
per-note user message follows and is the only thing that changes per call.

**Confidence threshold.**

The tool schema includes a per-entity `confidence` field (0.0-1.0). We retain
only entities with `confidence >= 0.7` before writing back to the vault. Low-
confidence entries still appear in the trace's raw response for debugging, but
the `NERResult.entities` list — and therefore the YAML frontmatter — only
carries high-confidence names.

**No langfuse.** See `connecting_dots/enrichment/__init__.py` rationale.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import anthropic

from .tracer import Trace, append_trace, compute_cost_usd

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5"
MAX_BODY_CHARS = 4_000  # ~1000 tokens; sufficient context, bounded cost
CONFIDENCE_THRESHOLD = 0.7
MAX_TOKENS = 1024  # output cap; entities+topics for one note never need more


# --------------------------------------------------------------------------- #
# Tool schema — single function, strict shape
# --------------------------------------------------------------------------- #
# Entity types kept deliberately broad. The downstream edge builder (#11)
# doesn't care about the type — only the surface form for graph overlap —
# but exposing the type lets us debug "why did this note get linked to that
# one" later (e.g., generic "AI" vs specific "Anthropic").
_ENTITY_TYPES = ("person", "organization", "product", "concept", "location", "work", "other")

_TOOL_DEFINITION: dict[str, Any] = {
    "name": "record_extraction",
    "description": (
        "Record the named entities and topics extracted from the note. "
        "Call this tool exactly once with the full set of entities and topics."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "entities": {
                "type": "array",
                "description": (
                    "Named entities mentioned in the note. People, organizations, "
                    "products, concepts, locations, or notable works. Use the most "
                    "specific form (e.g., 'Anthropic' not 'an AI company'). "
                    "Deduplicate aliases (e.g., 'OpenAI' not both 'OpenAI' and 'Open AI')."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Canonical surface form of the entity.",
                        },
                        "type": {
                            "type": "string",
                            "enum": list(_ENTITY_TYPES),
                            "description": "Coarse-grained entity category.",
                        },
                        "confidence": {
                            "type": "number",
                            "description": (
                                "Confidence in this extraction, 0.0 to 1.0. "
                                "Use 0.9+ for explicit named mentions, 0.7-0.9 for "
                                "clear context, <0.7 for guesses (these will be dropped)."
                            ),
                        },
                    },
                    "required": ["name", "type", "confidence"],
                },
            },
            "topics": {
                "type": "array",
                "description": (
                    "Themes the note is about, as short noun-phrase tags. "
                    "Examples: 'spaced repetition', 'macroeconomics', 'second brain', "
                    "'GPU compute'. 2-6 topics per note. Lowercase, no punctuation."
                ),
                "items": {"type": "string"},
            },
        },
        "required": ["entities", "topics"],
    },
}


# --------------------------------------------------------------------------- #
# Cached instruction prefix (system prompt + few-shot)
# --------------------------------------------------------------------------- #
# Kept frozen across all calls — any byte change invalidates the cache. NO
# timestamps, NO per-call IDs, NO unsorted dicts. The user-turn content (the
# actual note) comes in `messages` AFTER this and is the only varying part.
_SYSTEM_PROMPT = """\
You are an entity and topic extraction system for a personal knowledge vault.

Each user message contains one note — title plus body text (which may be a
WhatsApp message, web article, YouTube transcript, document scan, audio note,
or freeform thought). Your job is to extract:

1. **Entities** — named people, organizations, products, concepts, locations,
   or notable works. Be specific: "Anthropic" not "AI company"; "spaced
   repetition" not "memory technique". Deduplicate aliases. Skip generic
   common nouns ("the team", "the model").
2. **Topics** — 2-6 short noun-phrase tags describing what the note is *about*.
   Lowercase, no punctuation. Think Obsidian tags: "macroeconomics",
   "second brain", "founder psychology".

Always call the `record_extraction` tool exactly once with both arrays.

Guidance:
- Personal-use vault. Recurring entities will be: people (founders, authors,
  friends), companies (startups, funds, employers), products (apps, books,
  papers), concepts (technical, philosophical, financial).
- Confidence: use 0.9+ for explicit named mentions ("Naval Ravikant said..."),
  0.7-0.9 for clear contextual mentions ("the YC partner who wrote about..."),
  below 0.7 if you're guessing. Low-confidence entities are dropped downstream.
- If the note is too short or too generic to extract anything meaningful,
  return empty arrays. Do NOT fabricate.

Examples:

Note:
Title: Naval Ravikant on Twitter — wealth vs status
Body: Seek wealth, not money or status. Wealth is having assets that earn while you sleep. Money is how we transfer time and wealth. Status is your place in the social hierarchy.

record_extraction({
  "entities": [
    {"name": "Naval Ravikant", "type": "person", "confidence": 0.98},
    {"name": "Twitter", "type": "product", "confidence": 0.85}
  ],
  "topics": ["wealth building", "personal finance", "status games"]
})

Note:
Title: Coreweave vs Lambda — GPU compute economics
Body: Looking at the AI infrastructure landscape. Coreweave has H100 capacity locked in with NVIDIA. Lambda Labs going more retail. Both burning cash to lock supply before the LLM training squeeze.

record_extraction({
  "entities": [
    {"name": "CoreWeave", "type": "organization", "confidence": 0.95},
    {"name": "Lambda Labs", "type": "organization", "confidence": 0.92},
    {"name": "NVIDIA", "type": "organization", "confidence": 0.95},
    {"name": "H100", "type": "product", "confidence": 0.95},
    {"name": "LLM training", "type": "concept", "confidence": 0.85}
  ],
  "topics": ["gpu compute", "ai infrastructure", "ai economics"]
})

Note:
Title: WhatsApp Image 42
Body: [no caption]

record_extraction({
  "entities": [],
  "topics": []
})
"""


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #
@dataclass
class NERResult:
    """The post-threshold output of one extraction call.

    `entities` is a sorted, deduplicated list of names that survived the
    confidence threshold — this is what gets written to the YAML frontmatter.
    `raw` is the full tool-input dict (entities with type/confidence + topics)
    for the trace log and debugging.
    """

    entities: list[str]
    topics: list[str]
    raw: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_tokens: int = 0
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# Client — module-level so the HTTP connection pool is reused across calls
# --------------------------------------------------------------------------- #
_client_cache: dict[str, anthropic.Anthropic] = {}


def _get_client() -> anthropic.Anthropic:
    # Cached on api_key so a test that swaps the env var gets a fresh client.
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key not in _client_cache:
        _client_cache[key] = anthropic.Anthropic()
    return _client_cache[key]


# --------------------------------------------------------------------------- #
# Public extractor
# --------------------------------------------------------------------------- #
def extract(
    *,
    title: str,
    body: str,
    vault_path: str = "",
    model: Optional[str] = None,
    client: Optional[anthropic.Anthropic] = None,
    trace: bool = True,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
) -> NERResult:
    """Extract entities and topics for a single note.

    Args:
        title: Note title (frontmatter `title`).
        body: Note body. Truncated to `MAX_BODY_CHARS` to bound token cost on
            long YouTube transcripts.
        vault_path: Vault-relative path for trace logs only.
        model: Override the default model. Reads `NER_MODEL` env var if unset,
            falls back to `claude-haiku-4-5`.
        client: Inject an `anthropic.Anthropic` instance for testing.
        trace: Emit a JSONL trace record on completion (incl. errors).
        confidence_threshold: Drop entities with confidence below this.

    Returns:
        `NERResult`. On any extraction failure (API error, parse error,
        timeout), returns an empty result — never raises — so the backfill
        worker can mark the note `ner_error` and move on without crashing.
    """
    chosen_model = model or os.environ.get("NER_MODEL") or DEFAULT_MODEL
    api = client or _get_client()

    # Truncate the body to bound prompt cost. YouTube transcripts can be 50K+
    # chars; the first 4K is more than enough to identify entities + topics.
    truncated_body = (body or "")[:MAX_BODY_CHARS]
    user_content = f"Title: {title or '(no title)'}\nBody: {truncated_body or '(empty)'}"

    started = time.perf_counter()
    error: Optional[str] = None
    raw_input: dict[str, Any] = {}
    in_tokens = out_tokens = cached_in = cache_create = 0

    try:
        # Prompt-caching placement:
        #   - `tools` renders at position 0 — implicitly cached as part of the
        #     prefix once any system block is marked.
        #   - `system` is a list of content blocks; we put cache_control on
        #     the last one so tools+system are cached together.
        #   - `messages` (per-note content) follows the breakpoint, so it
        #     never invalidates the cached prefix.
        response = api.messages.create(
            model=chosen_model,
            max_tokens=MAX_TOKENS,
            tools=[_TOOL_DEFINITION],
            tool_choice={"type": "tool", "name": "record_extraction"},
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        )

        # Usage accounting. `cache_read_input_tokens` are served from cache;
        # `cache_creation_input_tokens` are the prefix that was just written.
        usage = getattr(response, "usage", None)
        if usage is not None:
            in_tokens = getattr(usage, "input_tokens", 0) or 0
            out_tokens = getattr(usage, "output_tokens", 0) or 0
            cached_in = getattr(usage, "cache_read_input_tokens", 0) or 0
            cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0

        raw_input = _extract_tool_input(response)
        entities_kept, topics = _apply_threshold(raw_input, confidence_threshold)

    except Exception as e:
        # Never propagate — backfill must continue across thousands of notes.
        # The trace + error get recorded; the caller marks the note ner_error.
        error = f"{type(e).__name__}: {e}"
        log.warning("NER extraction failed for %s: %s", vault_path, error)
        entities_kept, topics = [], []

    latency_ms = (time.perf_counter() - started) * 1000.0

    if trace:
        try:
            append_trace(
                Trace(
                    vault_path=vault_path,
                    model=chosen_model,
                    input_tokens=in_tokens,
                    output_tokens=out_tokens,
                    cached_input_tokens=cached_in,
                    cache_creation_tokens=cache_create,
                    cost_usd=compute_cost_usd(
                        model=chosen_model,
                        input_tokens=in_tokens,
                        output_tokens=out_tokens,
                        cached_input_tokens=cached_in,
                        cache_creation_tokens=cache_create,
                    ),
                    entities_count=len(entities_kept),
                    topics_count=len(topics),
                    duration_ms=round(latency_ms, 2),
                    error=error,
                )
            )
        except Exception:  # pragma: no cover — trace must never break extraction
            log.exception("Failed to write NER trace")

    return NERResult(
        entities=entities_kept,
        topics=topics,
        raw=raw_input,
        latency_ms=latency_ms,
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        cached_input_tokens=cached_in,
        cache_creation_tokens=cache_create,
        error=error,
    )


# --------------------------------------------------------------------------- #
# Response parsing
# --------------------------------------------------------------------------- #
def _extract_tool_input(response: Any) -> dict[str, Any]:
    """Pull the `record_extraction` tool_use input dict out of the response.

    Returns `{}` if no tool_use block was found — caller treats that as zero
    entities / zero topics.
    """
    content = getattr(response, "content", None) or []
    for block in content:
        # The SDK returns rich block objects with `.type` / `.input`; in tests
        # we also accept dict-shaped blocks for mocking convenience.
        btype = getattr(block, "type", None) or (
            block.get("type") if isinstance(block, dict) else None
        )
        if btype != "tool_use":
            continue
        bname = getattr(block, "name", None) or (
            block.get("name") if isinstance(block, dict) else None
        )
        if bname != "record_extraction":
            continue
        binput = getattr(block, "input", None) or (
            block.get("input") if isinstance(block, dict) else None
        )
        if isinstance(binput, dict):
            return binput
    return {}


def _apply_threshold(
    raw: dict[str, Any], threshold: float
) -> tuple[list[str], list[str]]:
    """Filter entities by confidence and return (entity_names, topics).

    - Entities below `threshold` are dropped.
    - Names are deduplicated case-insensitively, preserving the casing of the
      first occurrence (Claude is consistent here so this rarely matters).
    - Both lists are sorted for deterministic frontmatter — same note → same
      diff bytes, which keeps git history clean.
    """
    raw_entities = raw.get("entities") or []
    raw_topics = raw.get("topics") or []

    seen_lower: set[str] = set()
    kept_names: list[str] = []
    for ent in raw_entities:
        if not isinstance(ent, dict):
            continue
        name = (ent.get("name") or "").strip()
        try:
            confidence = float(ent.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue
        if not name or confidence < threshold:
            continue
        key = name.lower()
        if key in seen_lower:
            continue
        seen_lower.add(key)
        kept_names.append(name)

    # Topics: dedupe lowercased, drop empties.
    seen_topics: set[str] = set()
    kept_topics: list[str] = []
    for t in raw_topics:
        if not isinstance(t, str):
            continue
        s = t.strip().lower()
        if not s or s in seen_topics:
            continue
        seen_topics.add(s)
        kept_topics.append(s)

    return sorted(kept_names), sorted(kept_topics)


__all__ = [
    "NERResult",
    "extract",
    "DEFAULT_MODEL",
    "CONFIDENCE_THRESHOLD",
    "MAX_BODY_CHARS",
]
