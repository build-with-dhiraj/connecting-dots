"""2-sentence TL;DR extractor.

Generates exactly two sentences summarising a note and prepends them to the body
as a blockquote. Uses the same Azure OpenAI tool-calling pattern as `ner.py`.

Idempotency: skip notes where `raw_meta.tldr_at` is set.
Skips notes with body < 200 chars (too short to need a summary).

Output format prepended to body:
    > **TL;DR.** Sentence one. Sentence two.

Stamps `raw_meta.tldr_at` + `raw_meta.tldr_model` on success.
"""
from __future__ import annotations

import json
import logging
import os
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
MAX_BODY_CHARS = 4_000
MIN_BODY_CHARS = 200  # skip very short notes

# --------------------------------------------------------------------------- #
# Tool schema
# --------------------------------------------------------------------------- #
_TLDR_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "record_tldr",
        "description": "Record the 2-sentence TL;DR.",
        "parameters": {
            "type": "object",
            "properties": {
                "sentence_1": {
                    "type": "string",
                    "description": "What is this note about? One sentence.",
                },
                "sentence_2": {
                    "type": "string",
                    "description": "The key insight, takeaway, or actionable signal. One sentence.",
                },
            },
            "required": ["sentence_1", "sentence_2"],
        },
    },
}

# --------------------------------------------------------------------------- #
# Stable system prompt
# --------------------------------------------------------------------------- #
_SYSTEM_PROMPT = """\
You are a summarisation system for a personal knowledge vault. Your job is to \
generate a 2-sentence TL;DR for each note.

Sentence 1: What is this note about? (the topic/subject)
Sentence 2: The key insight, takeaway, or actionable signal from this note.

Rules:
- Exactly 2 sentences — no more, no less
- Be specific and concrete — no vague generalities
- Write in present tense, declarative style
- Do NOT start with "This note..." or "The author..."
- Do NOT fabricate facts not present in the note
- If the note is too vague to have a key insight, sentence 2 can be "Worth revisiting for context."
- Always call record_tldr exactly once

Examples:
Note about "The bitter lesson" in ML: reward signal + search beat domain knowledge in the long run.
→ s1: "Sutton argues that general methods leveraging computation consistently outperform \
approaches that encode human domain knowledge."
→ s2: "The strategic implication: invest in scalable architectures and data, not clever priors."
"""


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #
@dataclass
class TLDRResult:
    sentence_1: str
    sentence_2: str
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    error: Optional[str] = None

    def as_blockquote(self) -> str:
        """Format as the markdown blockquote prepended to the note body."""
        return f"> **TL;DR.** {self.sentence_1} {self.sentence_2}"


# --------------------------------------------------------------------------- #
# Trace
# --------------------------------------------------------------------------- #
_DEFAULT_TLDR_TRACES_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "tldr_traces.jsonl"
)
_TRACE_LOCK = threading.Lock()


def _resolve_tldr_traces_path() -> Path:
    env = os.environ.get("CONNECTING_DOTS_TLDR_TRACES")
    if env:
        return Path(env)
    return _DEFAULT_TLDR_TRACES_PATH


def _append_tldr_trace(record: dict[str, Any], *, path: Optional[Path] = None) -> None:
    target = path or _resolve_tldr_traces_path()
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
def extract(
    *,
    body: str,
    vault_path: str = "",
    model: Optional[str] = None,
    client: Optional[AzureOpenAI] = None,
    trace: bool = True,
) -> TLDRResult:
    """Generate a 2-sentence TL;DR for the given note body.

    Args:
        body: Full note body text; truncated to MAX_BODY_CHARS internally.
        vault_path: Vault-relative path for trace logs only.
        model: Override deployment name.
        client: Inject AzureOpenAI for testing.
        trace: Write to tldr_traces.jsonl.

    Returns:
        TLDRResult. Never raises — errors returned in `.error`.
    """
    chosen_model = (
        model
        or os.environ.get("TLDR_MODEL")
        or os.environ.get("AZURE_OPENAI_DEPLOYMENT")
        or DEFAULT_MODEL
    )
    api = client or _get_client()

    truncated = (body or "")[:MAX_BODY_CHARS]
    user_content = f"Note body:\n{truncated}"

    started = time.perf_counter()
    error: Optional[str] = None
    in_tokens = out_tokens = cached_in = 0
    s1 = s2 = ""

    try:
        response = api.chat.completions.create(
            model=chosen_model,
            max_tokens=MAX_TOKENS,
            tools=[_TLDR_TOOL],
            tool_choice={"type": "function", "function": {"name": "record_tldr"}},
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

        raw = _parse_tool_call(response, "record_tldr")
        s1 = (raw.get("sentence_1") or "").strip()
        s2 = (raw.get("sentence_2") or "").strip()

        if not s1 or not s2:
            error = f"Incomplete TL;DR: s1={bool(s1)}, s2={bool(s2)}"
            log.warning("Incomplete TL;DR for %s", vault_path)

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        log.warning("TL;DR extraction failed for %s: %s", vault_path, error)

    latency_ms = (time.perf_counter() - started) * 1000.0

    if trace:
        try:
            _append_tldr_trace(
                {
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "vault_path": vault_path,
                    "model": chosen_model,
                    "input_tokens": in_tokens,
                    "output_tokens": out_tokens,
                    "cached_input_tokens": cached_in,
                    "duration_ms": round(latency_ms, 2),
                    "error": error,
                }
            )
        except Exception:
            log.exception("Failed to write TL;DR trace")

    return TLDRResult(
        sentence_1=s1,
        sentence_2=s2,
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


__all__ = ["TLDRResult", "extract", "MIN_BODY_CHARS"]
