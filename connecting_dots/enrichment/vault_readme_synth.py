"""Synthesis helper for vault/README.md 'What this is' paragraph.

Calls Azure OpenAI gpt-4.1 to generate a 2-3 sentence overview of the vault.
The paragraph is stable across runs given the same stats, but can be regenerated
cheaply (~$0.001) by the vault_readme_refresh worker.

Usage (programmatic)::

    from connecting_dots.enrichment.vault_readme_synth import synthesise_overview
    paragraph = synthesise_overview(stats={"total": 1464, "themes": 30})
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from openai import AzureOpenAI

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4.1"
DEFAULT_API_VERSION = "2024-10-21"

_SYSTEM_PROMPT = """\
You are a knowledge architect writing documentation for a personal second-brain vault.
Write exactly 2-3 sentences (no headings, no bullet points) that explain what this vault is,
what sources feed into it, and how it is automatically enriched.
Be specific and concrete. Write in present tense. Do not use first person."""

_STATIC_PARAGRAPH = (
    "Connecting Dots is a personal knowledge vault that continuously ingests saved "
    "links and content from WhatsApp, YouTube, LinkedIn, Instagram, and the web, "
    "storing each item as a structured Markdown note with YAML frontmatter. "
    "Every note is automatically enriched with named entities, topics, and a "
    "2-sentence TL;DR using Azure OpenAI, then organised into theme pages (Maps of "
    "Content) for browsable discovery. "
    "The result is a self-updating second-brain that surfaces connections across "
    "thousands of saves without any manual tagging."
)

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


def synthesise_overview(
    *,
    stats: dict[str, Any],
    model: Optional[str] = None,
    client: Optional[AzureOpenAI] = None,
) -> str:
    """Call Azure OpenAI to generate the 'What this is' paragraph.

    Args:
        stats: Vault counts dict (total, web, whatsapp, linkedin, youtube, themes, etc.).
        model: Override deployment name; falls back to env AZURE_OPENAI_DEPLOYMENT.
        client: Inject AzureOpenAI for testing.

    Returns:
        A 2-3 sentence paragraph string. Falls back to _STATIC_PARAGRAPH on error.
    """
    chosen_model = (
        model
        or os.environ.get("AZURE_OPENAI_DEPLOYMENT")
        or DEFAULT_MODEL
    )
    api = client or _get_client()

    user_content = (
        f"Vault stats: {stats}. "
        "Write the 2-3 sentence 'What this is' paragraph for the README."
    )

    try:
        response = api.chat.completions.create(
            model=chosen_model,
            max_tokens=200,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )
        choices = getattr(response, "choices", None) or []
        if choices:
            content = getattr(getattr(choices[0], "message", None), "content", None)
            if content and len(content.strip()) > 40:
                return content.strip()
    except Exception as exc:
        log.warning("vault_readme_synth: LLM call failed (%s), using static paragraph", exc)

    return _STATIC_PARAGRAPH


__all__ = ["synthesise_overview", "_STATIC_PARAGRAPH"]
