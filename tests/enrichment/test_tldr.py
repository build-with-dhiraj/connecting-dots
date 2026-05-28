"""Tests for connecting_dots.enrichment.tldr."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from connecting_dots.enrichment.tldr import TLDRResult, extract


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _make_tldr_response(
    *,
    sentence_1: str = "This paper argues that scale beats domain knowledge.",
    sentence_2: str = "The key takeaway is to invest in scalable architectures.",
    input_tokens: int = 600,
    output_tokens: int = 60,
    cached_input_tokens: int = 500,
    include_tool_call: bool = True,
    arguments_as_string: bool = True,
):
    payload = {"sentence_1": sentence_1, "sentence_2": sentence_2}
    args = json.dumps(payload) if arguments_as_string else payload

    tool_calls = []
    if include_tool_call:
        tool_calls.append(
            SimpleNamespace(
                id="call_tldr",
                type="function",
                function=SimpleNamespace(name="record_tldr", arguments=args),
            )
        )

    message = SimpleNamespace(
        role="assistant", content=None, tool_calls=tool_calls or None
    )
    return SimpleNamespace(
        id="chatcmpl-tldr",
        choices=[SimpleNamespace(index=0, message=message, finish_reason="tool_calls")],
        usage=SimpleNamespace(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            prompt_tokens_details=SimpleNamespace(cached_tokens=cached_input_tokens),
        ),
    )


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.chat.completions.create.return_value = _make_tldr_response()
    return client


@pytest.fixture
def isolated_traces(tmp_path, monkeypatch):
    traces = tmp_path / "tldr_traces.jsonl"
    monkeypatch.setenv("CONNECTING_DOTS_TLDR_TRACES", str(traces))
    return traces


# --------------------------------------------------------------------------- #
# TLDRResult.as_blockquote
# --------------------------------------------------------------------------- #
def test_as_blockquote_format():
    r = TLDRResult(
        sentence_1="Sentence one here.",
        sentence_2="Sentence two here.",
    )
    bq = r.as_blockquote()
    assert bq.startswith("> **TL;DR.**")
    assert "Sentence one here." in bq
    assert "Sentence two here." in bq


# --------------------------------------------------------------------------- #
# extract — happy path
# --------------------------------------------------------------------------- #
def test_extract_returns_two_sentences(mock_client, isolated_traces):
    body = "x" * 300
    result = extract(body=body, client=mock_client)
    assert isinstance(result, TLDRResult)
    assert result.sentence_1 == "This paper argues that scale beats domain knowledge."
    assert result.sentence_2 == "The key takeaway is to invest in scalable architectures."
    assert result.error is None


def test_extract_token_accounting(mock_client, isolated_traces):
    result = extract(body="x" * 300, client=mock_client)
    assert result.input_tokens == 600
    assert result.output_tokens == 60
    assert result.cached_input_tokens == 500


def test_extract_writes_trace(mock_client, isolated_traces):
    extract(body="x" * 300, client=mock_client, vault_path="sources/test.md")
    lines = isolated_traces.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["vault_path"] == "sources/test.md"
    assert rec["error"] is None


def test_extract_error_does_not_raise(isolated_traces):
    bad = MagicMock()
    bad.chat.completions.create.side_effect = TimeoutError("API timeout")
    result = extract(body="x" * 300, client=bad)
    assert result.error is not None
    assert result.sentence_1 == ""
    assert result.sentence_2 == ""


# --------------------------------------------------------------------------- #
# Prepend logic in worker
# --------------------------------------------------------------------------- #
def test_tldr_prepend_to_body(tmp_path, isolated_traces, monkeypatch):
    """Verify tldr_backfill prepends blockquote correctly."""
    import workers.tldr_backfill as worker_mod

    vault_root = tmp_path / "vault"
    sources = vault_root / "sources"
    sources.mkdir(parents=True)
    note = sources / "long-note.md"
    long_body = "A" * 300
    note.write_text(
        f"---\ntitle: Test Note\n---\n{long_body}\n",
        encoding="utf-8",
    )

    def fake_extract(**kwargs):
        return TLDRResult(sentence_1="S1.", sentence_2="S2.")

    monkeypatch.setattr(worker_mod, "extract", fake_extract)

    result = worker_mod._process_one_sync(note, model=None, vault_root=vault_root, dry_run=False)

    assert result["status"] == "ok"
    text = note.read_text(encoding="utf-8")
    assert "> **TL;DR.**" in text
    assert "S1. S2." in text
    assert "tldr_at" in text


def test_tldr_idempotency(tmp_path, isolated_traces):
    """Notes with tldr_at set are skipped."""
    from workers.tldr_backfill import _process_one_sync

    vault_root = tmp_path / "vault"
    sources = vault_root / "sources"
    sources.mkdir(parents=True)
    note = sources / "done.md"
    note.write_text(
        "---\ntitle: Done\nraw_meta:\n  tldr_at: '2026-05-28T00:00:00Z'\n---\nbody here\n",
        encoding="utf-8",
    )
    result = _process_one_sync(note, model=None, vault_root=vault_root, dry_run=False)
    assert result["status"] == "skipped_idempotent"


def test_tldr_skips_short_body(tmp_path, isolated_traces):
    """Notes with body < MIN_BODY_CHARS are skipped."""
    from workers.tldr_backfill import _process_one_sync

    vault_root = tmp_path / "vault"
    sources = vault_root / "sources"
    sources.mkdir(parents=True)
    note = sources / "short.md"
    note.write_text("---\ntitle: Short\n---\nshort body\n", encoding="utf-8")
    result = _process_one_sync(note, model=None, vault_root=vault_root, dry_run=False)
    assert result["status"] == "skipped_too_short"


def test_tldr_dry_run(tmp_path, isolated_traces):
    vault_root = tmp_path / "vault"
    sources = vault_root / "sources"
    sources.mkdir(parents=True)
    note = sources / "dry.md"
    long_body = "B" * 300
    original = f"---\ntitle: Dry\n---\n{long_body}\n"
    note.write_text(original, encoding="utf-8")

    from workers.tldr_backfill import _process_one_sync
    result = _process_one_sync(note, model=None, vault_root=vault_root, dry_run=True)
    assert result["status"] == "dry_run"
    assert note.read_text(encoding="utf-8") == original
