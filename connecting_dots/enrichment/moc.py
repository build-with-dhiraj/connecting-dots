"""MoC (Map of Content) synthesis extractor.

Builds a 2-3 paragraph synthesis + essential-notes list for a topic by calling
Azure OpenAI gpt-4.1. Uses the same prompt-prefix-caching and tool-calling
pattern as `ner.py`.

Design notes
============

**Prompt caching contract** — the system prompt is kept byte-identical across
all topic calls. Only the trailing user message (topic name + note snippets)
varies. This amortises the ~1 000-token prefix across 30+ topic calls.

**Tool-calling** — `record_moc_synthesis` returns structured JSON so we never
parse free-text. The caller writes it into the MoC template.

**No cloud observability** — local JSONL traces only, extending the same
`data/ner_traces.jsonl`-style format. A separate `data/moc_traces.jsonl` file
is used so NER and MoC costs stay separable.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from openai import AzureOpenAI

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4.1"
MAX_TOKENS = 1500
DEFAULT_API_VERSION = "2024-10-21"
MAX_BODY_PREVIEW_CHARS = 200  # per note for synthesis prompt

# --------------------------------------------------------------------------- #
# Tool schema
# --------------------------------------------------------------------------- #
_MOC_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "record_moc_synthesis",
        "description": (
            "Record the synthesis and essential notes list for a Map of Content page."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "synthesis": {
                    "type": "string",
                    "description": (
                        "2-3 paragraphs synthesising what the saved notes on this topic "
                        "cover, how they connect, and what patterns emerge. Written in "
                        "second person ('Your saves on X reveal...'). Plain markdown, "
                        "no headers."
                    ),
                },
                "essential_notes": {
                    "type": "array",
                    "description": (
                        "3-7 notes worth highlighting at the top of the MoC page. "
                        "Pick the most insightful or foundational ones."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Exact note title."},
                            "reason": {
                                "type": "string",
                                "description": "One short sentence on why this note is essential.",
                            },
                        },
                        "required": ["title", "reason"],
                    },
                },
            },
            "required": ["synthesis", "essential_notes"],
        },
    },
}

# --------------------------------------------------------------------------- #
# Stable system prompt — must stay byte-identical across calls for cache hits
# --------------------------------------------------------------------------- #
_SYSTEM_PROMPT = """\
You are a knowledge curator building Map of Content (MoC) pages for a personal \
knowledge vault in Obsidian. A MoC page is an overview of everything the user has \
saved on a specific topic.

You will receive a topic name and a list of note titles + first-200-character previews. \
Your job is to:

1. Write a 2-3 paragraph synthesis (plain markdown, no headers) that explains:
   - What this body of saves is fundamentally about
   - Patterns, tensions, or recurring themes you notice
   - How the notes connect to each other

2. Select 3-7 "essential" notes — the ones most worth reading first for someone \
exploring this topic. For each, give a one-sentence reason.

Guidelines:
- Write in second person: "Your saves on X show...", "You've collected..."
- Be specific and analytical, not vague
- Essential notes should be diverse (not 5 notes from the same source)
- If the note list is very short (< 5), a shorter synthesis is fine
- Always call record_moc_synthesis exactly once
"""


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #
@dataclass
class MoCResult:
    synthesis: str
    essential_notes: list[dict[str, str]]  # [{"title": ..., "reason": ...}]
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# Trace
# --------------------------------------------------------------------------- #
_DEFAULT_MOC_TRACES_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "moc_traces.jsonl"
)
_TRACE_LOCK = threading.Lock()


def _resolve_moc_traces_path() -> Path:
    env = os.environ.get("CONNECTING_DOTS_MOC_TRACES")
    if env:
        return Path(env)
    return _DEFAULT_MOC_TRACES_PATH


def _append_moc_trace(record: dict[str, Any], *, path: Optional[Path] = None) -> None:
    target = path or _resolve_moc_traces_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    encoded = line.encode("utf-8")
    with _TRACE_LOCK:
        with open(target, "ab") as f:
            f.write(encoded)


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
# Slug helper
# --------------------------------------------------------------------------- #
def _topic_slug(topic: str) -> str:
    """Convert a topic tag like '#topic/ai-engineering' to 'ai-engineering'."""
    # Strip leading #topic/ or #entity/ prefix if present
    s = re.sub(r"^#?(?:topic|entity)/", "", topic.strip())
    # Replace spaces with dashes, lowercase
    s = re.sub(r"\s+", "-", s.strip().lower())
    # Keep only safe chars
    s = re.sub(r"[^a-z0-9\-]", "", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "unknown"


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def synthesize(
    *,
    topic: str,
    notes: list[dict[str, str]],  # [{"title": ..., "body_preview": ...}]
    model: Optional[str] = None,
    client: Optional[AzureOpenAI] = None,
    trace: bool = True,
) -> MoCResult:
    """Synthesise a topic across the provided note snippets.

    Args:
        topic: Human-readable topic name, e.g. "ai engineering".
        notes: List of dicts with 'title' and 'body_preview' (up to 200 chars each).
        model: Override deployment name.
        client: Inject AzureOpenAI for testing.
        trace: Write to moc_traces.jsonl.

    Returns:
        MoCResult. Never raises — errors are returned in `.error`.
    """
    chosen_model = (
        model
        or os.environ.get("MOC_MODEL")
        or os.environ.get("AZURE_OPENAI_DEPLOYMENT")
        or DEFAULT_MODEL
    )
    api = client or _get_client()

    # Build user content — variable part only
    note_lines = []
    for n in notes:
        title = (n.get("title") or "").strip() or "(no title)"
        preview = (n.get("body_preview") or "")[:MAX_BODY_PREVIEW_CHARS].strip()
        note_lines.append(f"- **{title}**: {preview}")

    user_content = (
        f"Topic: {topic}\n\n"
        f"Notes ({len(notes)} total):\n"
        + "\n".join(note_lines)
    )

    started = time.perf_counter()
    error: Optional[str] = None
    in_tokens = out_tokens = cached_in = 0
    synthesis = ""
    essential: list[dict[str, str]] = []

    try:
        response = api.chat.completions.create(
            model=chosen_model,
            max_tokens=MAX_TOKENS,
            tools=[_MOC_TOOL],
            tool_choice={"type": "function", "function": {"name": "record_moc_synthesis"}},
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

        raw = _parse_tool_call(response, "record_moc_synthesis")
        synthesis = (raw.get("synthesis") or "").strip()
        essential_raw = raw.get("essential_notes") or []
        essential = [
            {"title": e.get("title", ""), "reason": e.get("reason", "")}
            for e in essential_raw
            if isinstance(e, dict)
        ]

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        log.warning("MoC synthesis failed for topic '%s': %s", topic, error)

    latency_ms = (time.perf_counter() - started) * 1000.0

    if trace:
        try:
            _append_moc_trace(
                {
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "topic": topic,
                    "model": chosen_model,
                    "note_count": len(notes),
                    "input_tokens": in_tokens,
                    "output_tokens": out_tokens,
                    "cached_input_tokens": cached_in,
                    "duration_ms": round(latency_ms, 2),
                    "error": error,
                }
            )
        except Exception:
            log.exception("Failed to write MoC trace")

    return MoCResult(
        synthesis=synthesis,
        essential_notes=essential,
        latency_ms=latency_ms,
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        cached_input_tokens=cached_in,
        error=error,
    )


def _parse_tool_call(response: Any, function_name: str) -> dict[str, Any]:
    """Extract function-call arguments from a chat completion response."""
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


__all__ = ["MoCResult", "synthesize", "_topic_slug"]
