"""Azure OpenAI NER + topic extractor with prompt-prefix caching and function calling.

Design notes
============

**Why function calling, not a JSON schema or regex parse?**

OpenAI function calling forces structured output through the same code path
that handles tool/function dispatch — the model returns a `tool_calls` list
whose `function.arguments` is JSON we parse once. We declare a single function
`record_extraction(entities, topics)` with a strict JSON Schema, and pin
`tool_choice` to force the model into calling it. No "the JSON sometimes has
trailing commas / missing closing brace" failure mode.

**Why prompt caching?**

The instruction prefix + few-shot examples are ~2000 tokens. At 1,464 notes
to backfill, that's 2.9M repeated tokens. Azure OpenAI (gpt-4.1) automatically
caches the prompt prefix when the same prefix appears across calls — there is
no `cache_control` flag to set (unlike Anthropic). The cache hit shows up as
`response.usage.prompt_tokens_details.cached_tokens` and is billed at 50% of
the normal input rate.

The CONTRACT is: keep the leading messages (system + few-shot, baked into the
system prompt here) BIT-IDENTICAL across calls. Any byte change anywhere in
the prefix invalidates the cache. Per-note variable content (title + body
slice) goes in the trailing user message, after the stable prefix, so it
never invalidates the cache.

**Confidence threshold.**

The function schema includes a per-entity `confidence` field (0.0-1.0). We
retain only entities with `confidence >= 0.7` before writing back to the
vault. Low-confidence entries still appear in the trace's raw response for
debugging, but the `NERResult.entities` list — and therefore the YAML
frontmatter — only carries high-confidence names.

**No langfuse.** See `connecting_dots/enrichment/__init__.py` rationale.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from openai import AzureOpenAI

from .tracer import Trace, append_trace, compute_cost_usd

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4.1"
MAX_BODY_CHARS = 4_000  # ~1000 tokens; sufficient context, bounded cost
CONFIDENCE_THRESHOLD = 0.7
MAX_TOKENS = 1024  # output cap; entities+topics for one note never need more
DEFAULT_API_VERSION = "2024-10-21"


# --------------------------------------------------------------------------- #
# Function schema — single function, strict shape
# --------------------------------------------------------------------------- #
# Entity types kept deliberately broad. The downstream edge builder (#11)
# doesn't care about the type — only the surface form for graph overlap —
# but exposing the type lets us debug "why did this note get linked to that
# one" later (e.g., generic "AI" vs specific "Anthropic").
_ENTITY_TYPES = ("person", "organization", "product", "concept", "location", "work", "other")

_FUNCTION_PARAMETERS: dict[str, Any] = {
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
}

# OpenAI tool/function-calling wrapper. The shape we pass to
# `client.chat.completions.create(tools=[...])`.
_TOOL_DEFINITION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "record_extraction",
        "description": (
            "Record the named entities and topics extracted from the note. "
            "Call this function exactly once with the full set of entities and topics."
        ),
        "parameters": _FUNCTION_PARAMETERS,
    },
}


# --------------------------------------------------------------------------- #
# Cached instruction prefix (system prompt + few-shot)
# --------------------------------------------------------------------------- #
# Kept frozen across all calls — any byte change invalidates Azure's automatic
# prompt-prefix cache. NO timestamps, NO per-call IDs, NO unsorted dicts. The
# user-turn content (the actual note) comes in `messages[1]` AFTER this and
# is the only varying part. Cached input tokens (50% price) are reported on
# `response.usage.prompt_tokens_details.cached_tokens` on each subsequent call.
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

Always call the `record_extraction` function exactly once with both arrays.

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
    `raw` is the full function-call argument dict (entities with type/confidence
    + topics) for the trace log and debugging.
    """

    entities: list[str]
    topics: list[str]
    raw: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_tokens: int = 0  # unused on Azure OpenAI; kept for trace-schema parity
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# Client — module-level so the HTTP connection pool is reused across calls
# --------------------------------------------------------------------------- #
_client_cache: dict[str, AzureOpenAI] = {}


def _get_client() -> AzureOpenAI:
    """Build (or reuse) an AzureOpenAI client from env.

    Cached on (endpoint, api_key, api_version) so a test that swaps env gets
    a fresh client. Requires:
      - AZURE_OPENAI_ENDPOINT
      - AZURE_OPENAI_API_KEY
    Optional:
      - AZURE_OPENAI_API_VERSION (defaults to a stable dated version)
    """
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
    api_key = os.environ.get("AZURE_OPENAI_API_KEY", "")
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", DEFAULT_API_VERSION)
    cache_key = f"{endpoint}|{api_key}|{api_version}"
    if cache_key not in _client_cache:
        _client_cache[cache_key] = AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=api_version,
        )
    return _client_cache[cache_key]


# --------------------------------------------------------------------------- #
# Public extractor
# --------------------------------------------------------------------------- #
def extract(
    *,
    title: str,
    body: str,
    vault_path: str = "",
    model: Optional[str] = None,
    client: Optional[AzureOpenAI] = None,
    trace: bool = True,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
) -> NERResult:
    """Extract entities and topics for a single note.

    Args:
        title: Note title (frontmatter `title`).
        body: Note body. Truncated to `MAX_BODY_CHARS` to bound token cost on
            long YouTube transcripts.
        vault_path: Vault-relative path for trace logs only.
        model: Override the default deployment name. Reads `NER_MODEL` then
            `AZURE_OPENAI_DEPLOYMENT` env vars if unset, falls back to "gpt-4.1".
        client: Inject an `AzureOpenAI` instance for testing.
        trace: Emit a JSONL trace record on completion (incl. errors).
        confidence_threshold: Drop entities with confidence below this.

    Returns:
        `NERResult`. On any extraction failure (API error, parse error,
        timeout), returns an empty result with `.error` populated — never
        raises — so the backfill worker can mark the note `ner_error` and
        move on without crashing.
    """
    chosen_model = (
        model
        or os.environ.get("NER_MODEL")
        or os.environ.get("AZURE_OPENAI_DEPLOYMENT")
        or DEFAULT_MODEL
    )
    api = client or _get_client()

    # Truncate the body to bound prompt cost. YouTube transcripts can be 50K+
    # chars; the first 4K is more than enough to identify entities + topics.
    truncated_body = (body or "")[:MAX_BODY_CHARS]
    user_content = f"Title: {title or '(no title)'}\nBody: {truncated_body or '(empty)'}"

    started = time.perf_counter()
    error: Optional[str] = None
    raw_input: dict[str, Any] = {}
    in_tokens = out_tokens = cached_in = 0

    try:
        # Prompt-prefix caching: Azure OpenAI auto-caches the leading messages
        # when the same prefix repeats across calls. We keep `messages[0]`
        # (system) byte-identical across calls; only `messages[1]` (user)
        # varies. No flag to set — the cache is transparent.
        response = api.chat.completions.create(
            model=chosen_model,
            max_tokens=MAX_TOKENS,
            tools=[_TOOL_DEFINITION],
            tool_choice={
                "type": "function",
                "function": {"name": "record_extraction"},
            },
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )

        # Usage accounting. Azure surfaces cached prefix tokens under
        # `prompt_tokens_details.cached_tokens` in the new v1 API.
        usage = getattr(response, "usage", None)
        if usage is not None:
            in_tokens = getattr(usage, "prompt_tokens", 0) or 0
            out_tokens = getattr(usage, "completion_tokens", 0) or 0
            details = getattr(usage, "prompt_tokens_details", None)
            if details is not None:
                # Pydantic model on real SDK; dict-shaped in test mocks.
                cached_in = (
                    getattr(details, "cached_tokens", None)
                    if not isinstance(details, dict)
                    else details.get("cached_tokens")
                ) or 0

        raw_input = _extract_function_call(response)
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
                    cache_creation_tokens=0,
                    cost_usd=compute_cost_usd(
                        model=chosen_model,
                        input_tokens=in_tokens,
                        output_tokens=out_tokens,
                        cached_input_tokens=cached_in,
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
        cache_creation_tokens=0,
        error=error,
    )


# --------------------------------------------------------------------------- #
# Response parsing
# --------------------------------------------------------------------------- #
def _extract_function_call(response: Any) -> dict[str, Any]:
    """Pull the `record_extraction` function-call arguments dict out of the
    response.

    OpenAI returns `response.choices[0].message.tool_calls`, a list where each
    entry has `.function.name` and `.function.arguments` (a JSON string).
    Returns `{}` if no matching tool_call was found — caller treats that as
    zero entities / zero topics.
    """
    choices = getattr(response, "choices", None) or []
    for choice in choices:
        message = getattr(choice, "message", None) or (
            choice.get("message") if isinstance(choice, dict) else None
        )
        if message is None:
            continue
        tool_calls = getattr(message, "tool_calls", None) or (
            message.get("tool_calls") if isinstance(message, dict) else None
        )
        if not tool_calls:
            continue
        for call in tool_calls:
            fn = getattr(call, "function", None) or (
                call.get("function") if isinstance(call, dict) else None
            )
            if fn is None:
                continue
            name = getattr(fn, "name", None) or (
                fn.get("name") if isinstance(fn, dict) else None
            )
            if name != "record_extraction":
                continue
            arguments = getattr(fn, "arguments", None) or (
                fn.get("arguments") if isinstance(fn, dict) else None
            )
            if isinstance(arguments, dict):
                return arguments
            if isinstance(arguments, str):
                try:
                    parsed = json.loads(arguments)
                except json.JSONDecodeError:
                    return {}
                if isinstance(parsed, dict):
                    return parsed
    return {}


def _apply_threshold(
    raw: dict[str, Any], threshold: float
) -> tuple[list[str], list[str]]:
    """Filter entities by confidence and return (entity_names, topics).

    - Entities below `threshold` are dropped.
    - Names are deduplicated case-insensitively, preserving the casing of the
      first occurrence (the model is usually consistent here so this rarely
      matters).
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
