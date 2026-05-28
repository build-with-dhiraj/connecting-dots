"""Tests for the smarter Untitled Note fallback chain.

Covers parse_wa_media_filename, derive_better_title, and the
title_backfill --fix-untitled worker path.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from connecting_dots.enrichment.title import (
    call_llm_for_title,
    derive_better_title,
    parse_wa_media_filename,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _make_title_response(title: str) -> SimpleNamespace:
    args = json.dumps({"title": title, "reason": "test"})
    tool_calls = [
        SimpleNamespace(
            id="call_1",
            type="function",
            function=SimpleNamespace(name="record_title", arguments=args),
        )
    ]
    message = SimpleNamespace(role="assistant", content=None, tool_calls=tool_calls)
    return SimpleNamespace(
        id="chatcmpl-test",
        choices=[SimpleNamespace(index=0, message=message, finish_reason="tool_calls")],
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            prompt_tokens_details=SimpleNamespace(cached_tokens=80),
        ),
    )


def _make_note_file(
    tmp_path: Path,
    fm: dict,
    body: str = "",
    filename: str = "note.md",
) -> Path:
    serialized = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
    content = f"---\n{serialized}---\n{body}"
    p = tmp_path / filename
    p.write_text(content, encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# parse_wa_media_filename tests
# --------------------------------------------------------------------------- #
def test_parse_image_filename():
    result = parse_wa_media_filename("IMG-20250115-WA0001.jpg")
    assert result is not None
    assert result["kind"] == "Image"
    assert result["date_human"] == "15 Jan 2025"


def test_parse_voice_note_filename():
    result = parse_wa_media_filename("PTT-20260227-WA0042.opus")
    assert result is not None
    assert result["kind"] == "Voice note"
    assert result["date_human"] == "27 Feb 2026"


def test_parse_document_filename_no_date():
    # DOC files often have no parseable date pattern
    result = parse_wa_media_filename("00000337-Resume_Saksham.pdf")
    assert result is None


def test_parse_unknown_filename_returns_none():
    result = parse_wa_media_filename("my-random-note.md")
    assert result is None


def test_parse_audio_with_dashed_date():
    result = parse_wa_media_filename("AUD-2025-10-31-22-18-59.mp3")
    assert result is not None
    assert result["kind"] == "Audio"
    assert result["date_human"] == "31 Oct 2025"


def test_parse_video_filename():
    result = parse_wa_media_filename("VID-20250715-WA0008.mp4")
    assert result is not None
    assert result["kind"] == "Video"
    assert result["date_human"] == "15 Jul 2025"


# --------------------------------------------------------------------------- #
# derive_better_title layer tests
# --------------------------------------------------------------------------- #
def test_filename_layer_used_when_media_filename_present():
    fm = {
        "title": "Untitled Note",
        "captured_at": "2025-01-15T10:00:00Z",
        "source": "whatsapp",
        "raw_meta": {
            "original_title": "IMG-20250115-WA0001.jpg",
            "media_filename": "IMG-20250115-WA0001.jpg",
        },
    }
    title, source = derive_better_title(fm)
    assert source == "filename"
    assert "Image" in title
    assert "Jan" in title


def test_entity_topic_layer_calls_llm_with_correct_prompt():
    fm = {
        "title": "Untitled Note",
        "captured_at": "2025-03-10T08:00:00Z",
        "source": "whatsapp",
        "entities": ["Anthropic", "Claude"],
        "topics": ["ai-engineering", "llm"],
        "raw_meta": {"original_title": "PTT-20250310-WA0001.opus"},
    }
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_title_response(
        "Anthropic Claude Engineering Discussion"
    )
    title, source = derive_better_title(fm, client=mock_client)
    assert source == "entity-llm"
    assert title == "Anthropic Claude Engineering Discussion"
    # Verify the LLM was called with entities and topics in content
    call_args = mock_client.chat.completions.create.call_args
    messages = call_args.kwargs["messages"]
    user_msg = next(m for m in messages if m["role"] == "user")
    assert "Anthropic" in user_msg["content"]
    assert "ai-engineering" in user_msg["content"]


def test_date_fallback_when_no_entities():
    fm = {
        "title": "Untitled Note",
        "captured_at": "2025-06-20T12:00:00Z",
        "source": "whatsapp",
        "entities": [],
        "topics": [],
        "raw_meta": {"original_title": ""},
    }
    title, source = derive_better_title(fm)
    assert source == "date-fallback"
    assert "2025-06-20" in title
    assert "Whatsapp" in title or "whatsapp" in title.lower()


def test_fix_untitled_skips_when_already_v2_stamped():
    """Notes that already have title_v2_at should be skipped."""
    from workers.title_backfill import _is_untitled_candidate

    fm = {
        "title": "Untitled Note",
        "raw_meta": {
            "original_title": "PTT-20250310-WA0001.opus",
            "title_v2_at": "2025-05-01T00:00:00Z",
            "title_v2_source": "filename",
        },
    }
    assert _is_untitled_candidate(fm) is False


def test_fix_untitled_skips_non_untitled_notes():
    """Notes with a real title should not be candidates."""
    from workers.title_backfill import _is_untitled_candidate

    fm = {
        "title": "Anthropic Claude Free Course Notes",
        "raw_meta": {"original_title": "IMG-20250115-WA0001.jpg"},
    }
    assert _is_untitled_candidate(fm) is False


def test_dry_run_no_mutation(tmp_path):
    """--fix-untitled --dry-run must not write any files."""
    from workers.title_backfill import _process_untitled_sync

    note_path = _make_note_file(
        tmp_path,
        {
            "title": "Untitled Note",
            "source": "whatsapp",
            "captured_at": "2025-01-15T00:00:00Z",
            "raw_meta": {"original_title": "IMG-20250115-WA0001.jpg",
                         "media_filename": "IMG-20250115-WA0001.jpg"},
        },
        body="<attached: IMG-20250115-WA0001.jpg>",
    )
    original_mtime = note_path.stat().st_mtime

    result = _process_untitled_sync(
        note_path, model=None, vault_root=tmp_path, dry_run=True
    )
    assert result["status"] == "dry_run"
    assert note_path.stat().st_mtime == original_mtime


def test_atomic_write_preserves_other_frontmatter(tmp_path):
    """The fix should not destroy unrelated frontmatter keys."""
    from workers.title_backfill import _process_untitled_sync

    note_path = _make_note_file(
        tmp_path,
        {
            "title": "Untitled Note",
            "source": "whatsapp",
            "captured_at": "2025-01-15T00:00:00Z",
            "tags": ["media", "photo"],
            "raw_meta": {
                "original_title": "IMG-20250115-WA0001.jpg",
                "media_filename": "IMG-20250115-WA0001.jpg",
            },
        },
        body="<attached: IMG-20250115-WA0001.jpg>",
    )

    result = _process_untitled_sync(
        note_path, model=None, vault_root=tmp_path, dry_run=False
    )
    assert result["status"] == "ok"

    content = note_path.read_text(encoding="utf-8")
    # Pull frontmatter back out
    end = content.find("\n---\n", 4)
    fm_reloaded = yaml.safe_load(content[4:end])

    assert fm_reloaded["tags"] == ["media", "photo"]
    assert fm_reloaded["source"] == "whatsapp"
    assert fm_reloaded["title"] != "Untitled Note"
    assert fm_reloaded["raw_meta"]["title_v2_source"] == "filename"
    assert fm_reloaded["raw_meta"]["title_v2_at"]


def test_cli_fix_untitled_flag_routes_correctly(tmp_path, monkeypatch):
    """Passing --fix-untitled routes to the untitled batch, not the main batch."""
    from workers import title_backfill

    # Point vault root to our tmp dir (no notes → counts should be all-zero)
    monkeypatch.setattr(
        title_backfill, "_iter_vault_notes", lambda _root: []
    )
    monkeypatch.setattr(
        title_backfill, "_resolve_vault_root", lambda: tmp_path
    )

    # Should not raise
    rc = title_backfill.main(["--fix-untitled", "--dry-run"])
    assert rc == 0
