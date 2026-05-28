"""Tests for connecting_dots.enrichment.title."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from connecting_dots.enrichment.title import TitleResult, needs_rewrite, rewrite


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _make_title_response(
    *,
    title: str = "Q3 Product Strategy Whiteboard",
    reason: str = "Describes actual content.",
    input_tokens: int = 500,
    output_tokens: int = 30,
    cached_input_tokens: int = 400,
    include_tool_call: bool = True,
    arguments_as_string: bool = True,
):
    payload = {"title": title, "reason": reason}
    args = json.dumps(payload) if arguments_as_string else payload

    tool_calls = []
    if include_tool_call:
        tool_calls.append(
            SimpleNamespace(
                id="call_title",
                type="function",
                function=SimpleNamespace(name="record_title", arguments=args),
            )
        )

    message = SimpleNamespace(
        role="assistant", content=None, tool_calls=tool_calls or None
    )
    return SimpleNamespace(
        id="chatcmpl-title",
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
    client.chat.completions.create.return_value = _make_title_response()
    return client


@pytest.fixture
def isolated_traces(tmp_path, monkeypatch):
    traces = tmp_path / "title_traces.jsonl"
    monkeypatch.setenv("CONNECTING_DOTS_TITLE_TRACES", str(traces))
    return traces


# --------------------------------------------------------------------------- #
# needs_rewrite — garbage detector
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "title,expected",
    [
        (None, True),
        ("", True),
        ("Hi", True),  # < 5 chars
        ("1png-attached-00000104-1png", True),
        ("IMG-20240315-WA0042", True),
        ("AUD-20240315-WA0042", True),
        ("PTT-20240315-WA0042", True),
        ("VID-20240315-WA0042", True),
        ("DOC-20240315-WA0042", True),
        ("WhatsApp Audio 2024-03-15 at 10.30", True),
        ("https://example.com/some-article", True),
        ("<attached: file.pdf>", True),
        # Good titles should NOT trigger rewrite
        ("The Future of AI Engineering", False),
        ("How to Build a Second Brain", False),
        ("Naval Ravikant on Wealth vs Status", False),
    ],
)
def test_needs_rewrite(title, expected):
    assert needs_rewrite(title) == expected


# --------------------------------------------------------------------------- #
# rewrite — happy path
# --------------------------------------------------------------------------- #
def test_rewrite_returns_result(mock_client, isolated_traces):
    result = rewrite(
        old_title="1png-attached-00000104-1png",
        body="Whiteboard with Q3 product strategy",
        client=mock_client,
    )
    assert isinstance(result, TitleResult)
    assert result.title == "Q3 Product Strategy Whiteboard"
    assert result.error is None


def test_rewrite_token_accounting(mock_client, isolated_traces):
    result = rewrite(
        old_title="IMG-001",
        body="content here",
        client=mock_client,
    )
    assert result.input_tokens == 500
    assert result.output_tokens == 30
    assert result.cached_input_tokens == 400


def test_rewrite_writes_trace(mock_client, isolated_traces):
    rewrite(
        old_title="IMG-001",
        body="some content to describe",
        client=mock_client,
        vault_path="inbox/test.md",
    )
    lines = isolated_traces.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["vault_path"] == "inbox/test.md"
    assert rec["old_title"] == "IMG-001"
    assert rec["error"] is None


def test_rewrite_error_does_not_raise(isolated_traces):
    bad = MagicMock()
    bad.chat.completions.create.side_effect = ConnectionError("timeout")
    result = rewrite(old_title="IMG-001", body="body", client=bad)
    assert result.error is not None
    assert result.title == ""


def test_rewrite_no_tool_call_returns_error(isolated_traces):
    client = MagicMock()
    client.chat.completions.create.return_value = _make_title_response(
        include_tool_call=False
    )
    result = rewrite(old_title="IMG-001", body="body", client=client)
    assert result.error == "LLM returned empty title"


def test_rewrite_frontmatter_mutation(tmp_path, isolated_traces, monkeypatch):
    """Verify title_backfill worker rewrites frontmatter correctly."""
    import workers.title_backfill as worker_mod

    vault_root = tmp_path / "vault"
    inbox = vault_root / "inbox"
    inbox.mkdir(parents=True)

    note = inbox / "bad-title.md"
    note.write_text(
        "---\ntitle: IMG-001\nsource: whatsapp\n---\nSome good content here describing things.\n",
        encoding="utf-8",
    )

    def fake_rewrite(**kwargs):
        return TitleResult(title="Rewritten Clean Title", reason="test")

    monkeypatch.setattr(worker_mod, "rewrite", fake_rewrite)

    result = worker_mod._process_one_sync(note, model=None, vault_root=vault_root, dry_run=False)

    assert result["status"] == "ok"
    text = note.read_text(encoding="utf-8")
    assert "Rewritten Clean Title" in text
    assert "original_title: IMG-001" in text


def test_rewrite_idempotency(tmp_path, isolated_traces):
    """Notes with original_title set are skipped."""
    from workers.title_backfill import _process_one_sync

    vault_root = tmp_path / "vault"
    inbox = vault_root / "inbox"
    inbox.mkdir(parents=True)
    note = inbox / "already-done.md"
    note.write_text(
        "---\ntitle: Good Title Already\nraw_meta:\n  original_title: IMG-001\n---\nbody\n",
        encoding="utf-8",
    )
    result = _process_one_sync(note, model=None, vault_root=vault_root, dry_run=False)
    assert result["status"] == "skipped_idempotent"


def test_rewrite_dry_run(tmp_path, isolated_traces):
    """Dry run logs but does not mutate the note."""
    from workers.title_backfill import _process_one_sync

    vault_root = tmp_path / "vault"
    inbox = vault_root / "inbox"
    inbox.mkdir(parents=True)
    note = inbox / "dry-run-note.md"
    original = "---\ntitle: IMG-001\n---\nThis has some good content here.\n"
    note.write_text(original, encoding="utf-8")

    result = _process_one_sync(note, model=None, vault_root=vault_root, dry_run=True)
    assert result["status"] == "dry_run"
    assert note.read_text(encoding="utf-8") == original
