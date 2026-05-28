"""Per-item "why you should re-look at this today" reason generator.

Calls gpt-4.1 (via Azure OpenAI) to produce a single sentence explaining
why the algorithm picked this note. Uses function calling for structured output.

The reason incorporates:
- The note's title and topics
- A human-readable hint about *which* algorithm component drove the score
  (e.g., "high activity_relevance because you reacted thumbs_up to 3 Anthropic notes")
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

from openai import AzureOpenAI

from .resurface import DigestItem

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4.1"
MAX_TOKENS = 128
DEFAULT_API_VERSION = "2024-10-21"

# --------------------------------------------------------------------------- #
# Tool schema
# --------------------------------------------------------------------------- #

_WHY_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "record_reason",
        "description": "Record the one-sentence 'why you should re-look at this today' reason.",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "One sentence (max 20 words) explaining why this note is worth "
                        "revisiting today. Be concrete, reference the note title or topics."
                    ),
                },
            },
            "required": ["reason"],
        },
    },
}

# Stable system prompt (kept bit-identical across calls for prompt-prefix caching)
_SYSTEM_PROMPT = """\
You are a personal knowledge assistant helping someone rediscover content they saved.

Your job: write a single sentence (under 20 words) explaining why this saved note is worth revisiting today.

Rules:
- Be specific — reference the note title, a topic, or the algorithm hint
- Start with action verbs: "Revisit...", "Reconnect...", "Resurface...", "Revisit..."
- Do NOT start with "This note..."
- Do NOT be generic ("This might be interesting")
- If a hint is provided, use it to personalise the reason
- Always call record_reason exactly once
"""


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #

@dataclass
class WhyResult:
    slug: str
    reason: str
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# Client cache
# --------------------------------------------------------------------------- #

_client_cache: dict[str, AzureOpenAI] = {}


def _get_client() -> AzureOpenAI:
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
# Hint generation
# --------------------------------------------------------------------------- #

def _build_hint(item: DigestItem, score: float) -> str:
    """Build a human-readable hint for the LLM about why this item was selected."""
    hints = []
    if score > 0.7:
        hints.append("high overall relevance score")
    elif score > 0.4:
        hints.append("moderate relevance score")
    else:
        hints.append("selected for diversity")
    return "; ".join(hints) if hints else "hybrid algorithm"


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def generate_reason(
    item: DigestItem,
    *,
    topics: Optional[list[str]] = None,
    hint: Optional[str] = None,
    model: Optional[str] = None,
    client: Optional[AzureOpenAI] = None,
) -> WhyResult:
    """Generate a one-sentence reason for a single DigestItem.

    Args:
        item: The selected DigestItem.
        topics: List of topics for the note (from frontmatter).
        hint: Human-readable hint about algorithm component (e.g. "high activity_relevance").
        model: Override deployment name.
        client: Inject AzureOpenAI for testing.

    Returns:
        WhyResult. Never raises — errors returned in .error.
    """
    chosen_model = (
        model
        or os.environ.get("DIGEST_MODEL")
        or os.environ.get("AZURE_OPENAI_DEPLOYMENT")
        or DEFAULT_MODEL
    )
    api = client or _get_client()

    if hint is None:
        hint = _build_hint(item, item.score)

    topics_str = ", ".join(topics or []) or "untagged"
    user_content = (
        f"Note title: {item.title}\n"
        f"Topics: {topics_str}\n"
        f"Algorithm hint: {hint}\n"
        f"Generate one concise reason to revisit this note today."
    )

    started = time.perf_counter()
    error: Optional[str] = None
    reason = ""
    in_tokens = out_tokens = 0

    try:
        response = api.chat.completions.create(
            model=chosen_model,
            max_tokens=MAX_TOKENS,
            tools=[_WHY_TOOL],
            tool_choice={"type": "function", "function": {"name": "record_reason"}},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )

        usage = getattr(response, "usage", None)
        if usage is not None:
            in_tokens = getattr(usage, "prompt_tokens", 0) or 0
            out_tokens = getattr(usage, "completion_tokens", 0) or 0

        raw = _parse_tool_call(response, "record_reason")
        reason = (raw.get("reason") or "").strip()

        if not reason:
            error = "Empty reason returned"
            log.warning("Empty reason for item %s", item.slug)
            reason = f"Revisit: {item.title}"

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        log.warning("Why-reason generation failed for %s: %s", item.slug, error)
        reason = f"Revisit: {item.title}"

    latency_ms = (time.perf_counter() - started) * 1000.0

    return WhyResult(
        slug=item.slug,
        reason=reason,
        latency_ms=latency_ms,
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        error=error,
    )


def generate_reasons(
    items: list[DigestItem],
    *,
    notes_by_slug: Optional[dict] = None,
    model: Optional[str] = None,
    client: Optional[AzureOpenAI] = None,
) -> list[DigestItem]:
    """Generate reasons for all items and return new DigestItems with reasons filled in.

    Args:
        items: Selected items from select_digest_items().
        notes_by_slug: Mapping slug → note dict (for topics lookup).
        model: Override deployment name.
        client: Inject AzureOpenAI for testing.

    Returns:
        New list of DigestItems with reason field populated.
    """
    result = []
    for item in items:
        topics: list[str] = []
        if notes_by_slug:
            note = notes_by_slug.get(item.slug)
            if note:
                topics = note.get("topics", [])

        why = generate_reason(
            item,
            topics=topics,
            hint=_build_hint(item, item.score),
            model=model,
            client=client,
        )
        result.append(DigestItem(
            slug=item.slug,
            title=item.title,
            score=item.score,
            reason=why.reason,
            url=item.url,
        ))
    return result


# --------------------------------------------------------------------------- #
# Response parsing
# --------------------------------------------------------------------------- #

def _parse_tool_call(response: Any, function_name: str) -> dict[str, Any]:
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
            if name != function_name:
                continue
            arguments = getattr(fn, "arguments", None) or (
                fn.get("arguments") if isinstance(fn, dict) else None
            )
            if isinstance(arguments, dict):
                return arguments
            if isinstance(arguments, str):
                try:
                    parsed = json.loads(arguments)
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    return {}
    return {}


__all__ = ["WhyResult", "generate_reason", "generate_reasons"]
