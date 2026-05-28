"""Integration tests for `workers.ner_backfill`.

Spins up a tmp vault, writes a handful of synthetic notes, runs the
backfill with the Claude SDK mocked, and verifies:

- Frontmatter `entities` + `topics` get populated.
- `raw_meta.ner_enriched_at` and `raw_meta.ner_model` are set.
- A second run is a no-op (idempotent — already-enriched notes are skipped).
- Limit flag bounds the work.
- Notes already containing entities are left alone.
- Errors per-note don't crash the loop; failed notes get `ner_error`.
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from workers import ner_backfill


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def tmp_vault(tmp_path, monkeypatch):
    """A vault tree with a handful of notes under sources/ and inbox/."""
    monkeypatch.setenv("CONNECTING_DOTS_VAULT_ROOT", str(tmp_path))
    monkeypatch.setenv(
        "CONNECTING_DOTS_NER_TRACES", str(tmp_path / "ner_traces.jsonl")
    )
    (tmp_path / "sources" / "youtube").mkdir(parents=True)
    (tmp_path / "sources" / "web").mkdir(parents=True)
    (tmp_path / "inbox").mkdir(parents=True)
    (tmp_path / "inbox" / "_failed").mkdir(parents=True)
    return tmp_path


def _write_note(
    path: Path, *, title: str, body: str = "", entities: list[str] | None = None
) -> None:
    fm = {
        "source": "whatsapp",
        "handler": "youtube",
        "captured_at": "2026-05-28T12:00:00Z",
        "url": "",
        "title": title,
        "entities": entities or [],
        "topics": [],
        "labels": [],
    }
    fm_text = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm_text}\n---\n\n# {title}\n\n{body}\n", encoding="utf-8")


def _parse(path: Path) -> tuple[dict, str]:
    raw = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, flags=re.DOTALL)
    assert m
    return yaml.safe_load(m.group(1)), m.group(2)


# --------------------------------------------------------------------------- #
# Mocked Azure OpenAI client
# --------------------------------------------------------------------------- #
def _mock_response(entities: list[str], topics: list[str]):
    import json as _json

    payload = {
        "entities": [
            {"name": e, "type": "concept", "confidence": 0.95} for e in entities
        ],
        "topics": topics,
    }
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                index=0,
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            id="call_1",
                            type="function",
                            function=SimpleNamespace(
                                name="record_extraction",
                                arguments=_json.dumps(payload),
                            ),
                        )
                    ],
                ),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=1600,
            completion_tokens=20,
            total_tokens=1620,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        ),
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_backfill_populates_entities_and_topics(tmp_vault, monkeypatch):
    """Happy path: write 3 notes, run sweep, frontmatter gets filled."""
    _write_note(
        tmp_vault / "sources" / "youtube" / "naval.md",
        title="Naval on wealth",
        body="Seek wealth not status.",
    )
    _write_note(
        tmp_vault / "sources" / "web" / "buffett.md",
        title="Buffett on value",
        body="Wonderful business at fair price.",
    )
    _write_note(
        tmp_vault / "inbox" / "thought.md",
        title="A thought",
        body="Just thinking out loud.",
    )

    with patch("connecting_dots.enrichment.ner._get_client") as get_client:
        client = SimpleNamespace()
        client.chat = SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kw: _mock_response(["X", "Y"], ["topic-a"])
            )
        )
        get_client.return_value = client
        rc = ner_backfill.main(["--limit", "10", "--concurrency", "2"])

    assert rc == 0

    for rel in ("sources/youtube/naval.md", "sources/web/buffett.md", "inbox/thought.md"):
        fm, _ = _parse(tmp_vault / rel)
        assert fm["entities"] == ["X", "Y"], f"{rel} not enriched"
        assert fm["topics"] == ["topic-a"]
        assert fm["raw_meta"]["ner_enriched_at"]  # ISO timestamp
        assert fm["raw_meta"]["ner_model"] == "gpt-4.1"


def test_backfill_is_idempotent(tmp_vault):
    """Second run after first must be a no-op — already-enriched notes skipped."""
    _write_note(tmp_vault / "sources" / "web" / "a.md", title="A", body="content")

    call_count = {"n": 0}

    def _record_call(**kw):
        call_count["n"] += 1
        return _mock_response(["E"], ["t"])

    with patch("connecting_dots.enrichment.ner._get_client") as get_client:
        client = SimpleNamespace()
        client.chat = SimpleNamespace(completions=SimpleNamespace(create=_record_call))
        get_client.return_value = client

        ner_backfill.main(["--limit", "10"])
        first = call_count["n"]
        ner_backfill.main(["--limit", "10"])  # re-run
        second = call_count["n"]

    assert first == 1
    assert second == first, "second run should have skipped enriched note"


def test_backfill_skips_already_populated(tmp_vault):
    """A note that already has entities (e.g., manually authored) is left alone."""
    _write_note(
        tmp_vault / "sources" / "web" / "manual.md",
        title="manual",
        body="b",
        entities=["Manually Added"],
    )

    call_count = {"n": 0}
    with patch("connecting_dots.enrichment.ner._get_client") as get_client:
        client = SimpleNamespace()
        client.chat = SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kw: (call_count.update(n=call_count["n"] + 1) or _mock_response([], []))
            )
        )
        get_client.return_value = client
        ner_backfill.main(["--limit", "10"])

    assert call_count["n"] == 0
    fm, _ = _parse(tmp_vault / "sources" / "web" / "manual.md")
    assert fm["entities"] == ["Manually Added"]


def test_backfill_skips_failed_directory_and_example(tmp_vault):
    """Notes under inbox/_failed/ and inbox/example.md are excluded."""
    _write_note(
        tmp_vault / "inbox" / "_failed" / "broken.md", title="broken", body="b"
    )
    _write_note(tmp_vault / "inbox" / "example.md", title="example", body="b")
    _write_note(tmp_vault / "inbox" / "real.md", title="real", body="b")

    call_count = {"n": 0}
    with patch("connecting_dots.enrichment.ner._get_client") as get_client:
        client = SimpleNamespace()
        client.chat = SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kw: (call_count.update(n=call_count["n"] + 1) or _mock_response(["E"], ["t"]))
            )
        )
        get_client.return_value = client
        ner_backfill.main(["--limit", "10"])

    assert call_count["n"] == 1  # only inbox/real.md
    fm_broken, _ = _parse(tmp_vault / "inbox" / "_failed" / "broken.md")
    assert fm_broken["entities"] == []  # untouched


def test_backfill_limit_caps_work(tmp_vault):
    for i in range(5):
        _write_note(tmp_vault / "sources" / "web" / f"n{i}.md", title=f"n{i}")

    call_count = {"n": 0}
    with patch("connecting_dots.enrichment.ner._get_client") as get_client:
        client = SimpleNamespace()
        client.chat = SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kw: (call_count.update(n=call_count["n"] + 1) or _mock_response(["E"], []))
            )
        )
        get_client.return_value = client
        ner_backfill.main(["--limit", "3"])

    assert call_count["n"] == 3


def test_backfill_error_does_not_crash_loop(tmp_vault):
    """A failing extraction marks the note and the loop continues.

    Alphabetical iteration: boom.md is processed before ok.md, so call #1
    raises (boom) and call #2 succeeds (ok).
    """
    _write_note(tmp_vault / "sources" / "web" / "boom.md", title="boom", body="b")
    _write_note(tmp_vault / "sources" / "web" / "ok.md", title="ok", body="b")

    call_count = {"n": 0}

    def _raises_then_works(**kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated API failure")
        return _mock_response(["OK"], ["t"])

    with patch("connecting_dots.enrichment.ner._get_client") as get_client:
        client = SimpleNamespace()
        client.chat = SimpleNamespace(
            completions=SimpleNamespace(create=_raises_then_works)
        )
        get_client.return_value = client
        rc = ner_backfill.main(["--limit", "10", "--concurrency", "1"])

    assert rc == 0
    fm_boom, _ = _parse(tmp_vault / "sources" / "web" / "boom.md")
    fm_ok, _ = _parse(tmp_vault / "sources" / "web" / "ok.md")
    # The failed note must NOT have ner_enriched_at — otherwise the next
    # sweep would permanently skip it. It must have ner_error set instead.
    assert not fm_boom["raw_meta"].get("ner_enriched_at")
    assert fm_boom["raw_meta"].get("ner_error")
    # The successful note has the timestamp + entities populated.
    assert fm_ok["raw_meta"].get("ner_enriched_at")
    assert fm_ok["entities"] == ["OK"]


def test_backfill_retries_failed_note_on_next_sweep(tmp_vault):
    """A note that errored once must be re-processed on the next sweep."""
    _write_note(tmp_vault / "sources" / "web" / "flaky.md", title="flaky", body="b")

    call_count = {"n": 0}

    def _fail_then_succeed(**kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("transient API failure")
        return _mock_response(["E"], ["t"])

    with patch("connecting_dots.enrichment.ner._get_client") as get_client:
        client = SimpleNamespace()
        client.chat = SimpleNamespace(
            completions=SimpleNamespace(create=_fail_then_succeed)
        )
        get_client.return_value = client

        ner_backfill.main(["--limit", "10", "--concurrency", "1"])
        first_calls = call_count["n"]
        ner_backfill.main(["--limit", "10", "--concurrency", "1"])
        second_calls = call_count["n"]

    assert first_calls == 1
    assert second_calls == 2, "failed note must be retried on the next sweep"
    fm, _ = _parse(tmp_vault / "sources" / "web" / "flaky.md")
    assert fm["entities"] == ["E"]
    assert fm["raw_meta"].get("ner_enriched_at")
    assert "ner_error" not in fm["raw_meta"]
