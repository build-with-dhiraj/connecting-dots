"""Web body cleanup — strip cookie/nav/newsletter cruft from scraped notes.

Uses Azure OpenAI gpt-4.1 with tool-calling to enforce structured output.
The system prompt + function schema are kept byte-identical across calls so
Azure's automatic prompt-prefix caching cuts input token cost by ~50%.

Skip conditions (all checked before the LLM call):
  - raw_meta.handler != "web"
  - body < MIN_BODY_CHARS (800)
  - raw_meta.body_cleaned_at already set (idempotent)

Truncation guard: if cleaned_markdown is shorter than the original by more
than 90%, the call is treated as suspicious truncation and skipped.

TL;DR preservation: if the body starts with "> **TL;DR.**", those lines are
extracted before the LLM call and re-prepended after (so the LLM never sees
or eats the summary).
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from openai import AzureOpenAI

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4.1"
MAX_TOKENS = 8192  # body cleanup can return large articles
DEFAULT_API_VERSION = "2024-10-21"
MIN_BODY_CHARS = 800  # skip notes too short to have cruft
MAX_BODY_CHARS = 16_000  # bound cost on very long articles

_TLDR_MARKER = "> **TL;DR.**"

# --------------------------------------------------------------------------- #
# Removed-kind enum values
# --------------------------------------------------------------------------- #
_REMOVED_KINDS = (
    "cookie",
    "navigation",
    "newsletter",
    "related-articles",
    "comments",
    "author-bio",
    "social-share",
    "pagination",
    "ad",
    "other",
)

# --------------------------------------------------------------------------- #
# Tool schema
# --------------------------------------------------------------------------- #
_TOOL_DEFINITION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "record_cleaned_body",
        "description": (
            "Record the cleaned markdown article body after removing web cruft. "
            "Call exactly once with the full cleaned content."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "cleaned_markdown": {
                    "type": "string",
                    "description": "The article body with all cruft removed, in clean markdown.",
                },
                "removed_kinds": {
                    "type": "array",
                    "description": "Categories of content that were removed.",
                    "items": {"type": "string", "enum": list(_REMOVED_KINDS)},
                },
                "removed_count": {
                    "type": "integer",
                    "description": "Approximate number of distinct cruft blocks removed.",
                },
            },
            "required": ["cleaned_markdown"],
        },
    },
}

# --------------------------------------------------------------------------- #
# Stable system prompt (keep byte-identical for cache hits)
# --------------------------------------------------------------------------- #
_SYSTEM_PROMPT = """\
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

Always call the record_cleaned_body function exactly once."""


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #
@dataclass
class BodyCleanupResult:
    cleaned_markdown: str
    removed_kinds: list[str] = field(default_factory=list)
    removed_count: int = 0
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# Client cache (same pattern as ner.py)
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
# TL;DR extraction helpers
# --------------------------------------------------------------------------- #
def _extract_tldr_prefix(body: str) -> tuple[str, str]:
    """Split body into (tldr_block, rest).

    If body starts with "> **TL;DR.**", extract those blockquote lines.
    Returns ("", body) if no TL;DR prefix.
    """
    if not body.lstrip().startswith(_TLDR_MARKER):
        return "", body
    lines = body.split("\n")
    tldr_lines: list[str] = []
    rest_lines: list[str] = []
    in_tldr = True
    for line in lines:
        if in_tldr and (line.startswith(">") or line.strip() == ""):
            if line.strip() == "" and tldr_lines:
                # blank line after blockquote ends the TL;DR block
                in_tldr = False
                rest_lines.append(line)
            else:
                tldr_lines.append(line)
        else:
            in_tldr = False
            rest_lines.append(line)
    tldr_block = "\n".join(tldr_lines)
    rest = "\n".join(rest_lines)
    return tldr_block, rest


# --------------------------------------------------------------------------- #
# Response parsing
# --------------------------------------------------------------------------- #
def _parse_tool_call(response: Any) -> dict[str, Any]:
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
            if name != "record_cleaned_body":
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


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def clean_body(
    *,
    body: str,
    vault_path: str = "",
    model: Optional[str] = None,
    client: Optional[AzureOpenAI] = None,
) -> BodyCleanupResult:
    """Strip web cruft from a scraped article body.

    Args:
        body: The article body text (may contain cookie banners, nav menus, etc.)
        vault_path: Vault-relative path for logging only.
        model: Override Azure deployment name.
        client: Inject AzureOpenAI for testing.

    Returns:
        BodyCleanupResult. Never raises — errors in `.error`.
    """
    chosen_model = (
        model
        or os.environ.get("BODY_CLEANUP_MODEL")
        or os.environ.get("AZURE_OPENAI_DEPLOYMENT")
        or DEFAULT_MODEL
    )
    api = client or _get_client()

    truncated = (body or "")[:MAX_BODY_CHARS]
    user_content = f"Article body to clean:\n\n{truncated}"

    started = time.perf_counter()
    error: Optional[str] = None
    in_tokens = out_tokens = cached_in = 0
    cleaned = ""
    removed_kinds: list[str] = []
    removed_count = 0

    try:
        response = api.chat.completions.create(
            model=chosen_model,
            max_tokens=MAX_TOKENS,
            tools=[_TOOL_DEFINITION],
            tool_choice={"type": "function", "function": {"name": "record_cleaned_body"}},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )

        usage = getattr(response, "usage", None)
        if usage is not None:
            in_tokens = getattr(usage, "prompt_tokens", 0) or 0
            out_tokens = getattr(usage, "completion_tokens", 0) or 0
            details = getattr(usage, "prompt_tokens_details", None)
            if details is not None:
                cached_in = (
                    getattr(details, "cached_tokens", None)
                    if not isinstance(details, dict)
                    else details.get("cached_tokens")
                ) or 0

        raw = _parse_tool_call(response)
        cleaned = (raw.get("cleaned_markdown") or "").strip()
        removed_kinds = [k for k in (raw.get("removed_kinds") or []) if k in _REMOVED_KINDS]
        removed_count = int(raw.get("removed_count") or 0)

        if not cleaned:
            error = "LLM returned empty cleaned_markdown"
            log.warning("Body cleanup returned empty result for %s", vault_path)

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        log.warning("Body cleanup failed for %s: %s", vault_path, error)

    latency_ms = (time.perf_counter() - started) * 1000.0

    return BodyCleanupResult(
        cleaned_markdown=cleaned,
        removed_kinds=removed_kinds,
        removed_count=removed_count,
        latency_ms=latency_ms,
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        cached_input_tokens=cached_in,
        error=error,
    )


__all__ = [
    "BodyCleanupResult",
    "clean_body",
    "MIN_BODY_CHARS",
    "_extract_tldr_prefix",
    "_TLDR_MARKER",
]
