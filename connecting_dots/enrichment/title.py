"""Smart title rewriter — detects garbage titles and replaces them via Azure OpenAI.

Uses the same prompt-prefix-caching and tool-calling pattern as `ner.py`.
Writes the old title to `raw_meta.original_title` for recovery; stamps
`raw_meta.title_rewritten_at` + `raw_meta.title_model` on success.

Idempotency: skip notes where `raw_meta.original_title` is already set.

Title quality detector
----------------------
A title is considered "garbage" if it:
- Is empty, None, or < 5 chars
- Matches `_BAD_TITLE_RE` (WhatsApp filename patterns, IMG/AUD/PTT/VID/DOC prefixes,
  bare file extension, etc.)
- Starts with "http"
- Contains the LTR mark (U+200E), `<attached:`, or is mostly slugified-URL noise

Tool schema
-----------
The `record_title` function returns {title, reason} as a strict JSON structure.
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
MAX_TOKENS = 256
DEFAULT_API_VERSION = "2024-10-21"
MAX_BODY_CHARS = 800  # enough context for a title, bounded cost

# --------------------------------------------------------------------------- #
# Garbage-title detector
# --------------------------------------------------------------------------- #
# WhatsApp / iOS file naming patterns + common noise patterns.
_BAD_TITLE_RE = re.compile(
    r"""
    ^\d+png                     # e.g. 1png-attached-00000104-1png
    | ^IMG-
    | ^AUD-
    | ^PTT-
    | ^VID-
    | ^DOC-
    | ^\.[a-z]{2,5}$            # bare extension like .jpg
    | ^whatsapp\s+(audio|image|video|document)
    | ^audio\s+message
    | ^image\s+\d+
    | ^voice\s+message
    | ^file\s+attached
    """,
    re.IGNORECASE | re.VERBOSE,
)

_URL_NOISE_RE = re.compile(
    r"^https?://|"
    r"<attached:|"
    r"‎"  # LTR mark
)


def needs_rewrite(title: Optional[str]) -> bool:
    """Return True if this title should be rewritten by the LLM.

    Keeps the check cheap and deterministic — no LLM call.
    """
    if not title:
        return True
    t = title.strip()
    if len(t) < 5:
        return True
    if _BAD_TITLE_RE.search(t):
        return True
    if _URL_NOISE_RE.search(t):
        return True
    return False


# --------------------------------------------------------------------------- #
# Tool schema
# --------------------------------------------------------------------------- #
_TITLE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "record_title",
        "description": "Record the rewritten title.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": (
                        "Clean 5-12 word noun phrase title. Must describe the actual "
                        "subject matter. Do NOT include channel names like WhatsApp, "
                        "Note, or Audio. Should read like a book or article title."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "One short sentence explaining why this title.",
                },
            },
            "required": ["title"],
        },
    },
}

# --------------------------------------------------------------------------- #
# Stable system prompt
# --------------------------------------------------------------------------- #
_SYSTEM_PROMPT = """\
You are a title rewriter for a personal knowledge vault. Many notes have \
automatically-generated garbage titles (filenames, WhatsApp message headers, \
empty strings). Your job is to replace them with clean, descriptive titles.

You receive a note's old title and the first ~800 characters of its body. Return \
a clean noun-phrase title that describes the actual subject matter.

Rules:
- 5-12 words, noun phrase style (like a book or article title)
- Must reflect what the note is actually about — look at the body, not the old title
- Do NOT include: WhatsApp, Note, Audio, Voice Message, Image, Video, Document, \
  Attachment, or any other channel/format words
- Do NOT start with "A" or "The"
- Proper nouns, numbers, and technical terms are fine
- If the body is too short or unclear to determine a topic, use "Untitled Note" as fallback
- Always call record_title exactly once

Examples:
Old title: "1png-attached-00000104-1png"
Body: "Whiteboard with Q3 product strategy: growth flywheel, reduce CAC by 40%..."
→ title: "Q3 Product Strategy Whiteboard: Growth Flywheel and CAC Reduction"

Old title: "AUD-20240315-WA0042"
Body: "Just wanted to say the talk you gave last night was incredible, the part about..."
→ title: "Feedback on Last Night's Talk"

Old title: ""
Body: "https://arxiv.org/abs/2402.00159 — Really interesting paper on retrieval augmented generation..."
→ title: "Arxiv Paper on Retrieval Augmented Generation"
"""


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #
@dataclass
class TitleResult:
    title: str
    reason: str = ""
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# Trace
# --------------------------------------------------------------------------- #
_DEFAULT_TITLE_TRACES_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "title_traces.jsonl"
)
_TRACE_LOCK = threading.Lock()


def _resolve_title_traces_path() -> Path:
    env = os.environ.get("CONNECTING_DOTS_TITLE_TRACES")
    if env:
        return Path(env)
    return _DEFAULT_TITLE_TRACES_PATH


def _append_title_trace(record: dict[str, Any], *, path: Optional[Path] = None) -> None:
    target = path or _resolve_title_traces_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    with _TRACE_LOCK:
        with open(target, "ab") as f:
            f.write(line.encode("utf-8"))


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
# Public API
# --------------------------------------------------------------------------- #
def rewrite(
    *,
    old_title: str,
    body: str,
    vault_path: str = "",
    model: Optional[str] = None,
    client: Optional[AzureOpenAI] = None,
    trace: bool = True,
) -> TitleResult:
    """Generate a clean title for a note with a garbage title.

    Args:
        old_title: The existing (bad) title from frontmatter.
        body: Note body text, truncated to MAX_BODY_CHARS internally.
        vault_path: Vault-relative path for trace logs only.
        model: Override deployment name.
        client: Inject AzureOpenAI for testing.
        trace: Write to title_traces.jsonl.

    Returns:
        TitleResult. Never raises — errors returned in `.error`.
    """
    chosen_model = (
        model
        or os.environ.get("TITLE_MODEL")
        or os.environ.get("AZURE_OPENAI_DEPLOYMENT")
        or DEFAULT_MODEL
    )
    api = client or _get_client()

    truncated_body = (body or "")[:MAX_BODY_CHARS]
    user_content = (
        f"Old title: {old_title or '(empty)'}\n"
        f"Body: {truncated_body or '(empty)'}"
    )

    started = time.perf_counter()
    error: Optional[str] = None
    in_tokens = out_tokens = cached_in = 0
    new_title = ""
    reason = ""

    try:
        response = api.chat.completions.create(
            model=chosen_model,
            max_tokens=MAX_TOKENS,
            tools=[_TITLE_TOOL],
            tool_choice={"type": "function", "function": {"name": "record_title"}},
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

        raw = _parse_tool_call(response, "record_title")
        new_title = (raw.get("title") or "").strip()
        reason = (raw.get("reason") or "").strip()

        if not new_title:
            error = "LLM returned empty title"
            log.warning("Empty title returned for %s", vault_path)

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        log.warning("Title rewrite failed for %s: %s", vault_path, error)

    latency_ms = (time.perf_counter() - started) * 1000.0

    if trace:
        try:
            _append_title_trace(
                {
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "vault_path": vault_path,
                    "model": chosen_model,
                    "old_title": old_title or "",
                    "new_title": new_title,
                    "input_tokens": in_tokens,
                    "output_tokens": out_tokens,
                    "cached_input_tokens": cached_in,
                    "duration_ms": round(latency_ms, 2),
                    "error": error,
                }
            )
        except Exception:
            log.exception("Failed to write title trace")

    return TitleResult(
        title=new_title,
        reason=reason,
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


__all__ = ["TitleResult", "rewrite", "needs_rewrite"]
