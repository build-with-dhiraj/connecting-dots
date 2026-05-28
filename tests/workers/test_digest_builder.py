"""Tests for workers.digest_builder."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


def _write_note(vault: Path, slug: str, *, title: str, topics: list[str],
                entities: list[str], captured_at: str) -> None:
    p = vault / slug
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = {
        "title": title,
        "captured_at": captured_at,
        "topics": topics,
        "entities": [{"name": e, "type": "concept"} for e in entities],
    }
    fm_str = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, default_flow_style=False).rstrip()
    p.write_text(f"---\n{fm_str}\n---\n\nBody.", encoding="utf-8")


def _make_vault_with_notes(tmp_path: Path, n: int = 10) -> Path:
    vault = tmp_path / "vault"
    for i in range(n):
        days_ago = i * 10
        captured = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_note(vault, f"sources/web/note-{i:03d}.md",
                    title=f"Vault Note {i}", topics=[f"topic-{i % 3}"],
                    entities=[f"Entity{i % 5}"], captured_at=captured)
    return vault


# --------------------------------------------------------------------------- #
# Mock why_reason to avoid real LLM calls
# --------------------------------------------------------------------------- #

def _mock_generate_reasons(items, *, notes_by_slug=None, model=None, client=None):
    """Return items with a canned reason."""
    from connecting_dots.digest.resurface import DigestItem
    return [
        DigestItem(slug=item.slug, title=item.title, score=item.score,
                   reason="Revisit: interesting connection.", url=item.url)
        for item in items
    ]


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

@patch("workers.digest_builder.generate_reasons", side_effect=_mock_generate_reasons)
def test_digest_builder_dry_run(mock_reasons, tmp_path):
    """Dry run should not write any files."""
    vault = _make_vault_with_notes(tmp_path)
    queue_path = tmp_path / "queue.jsonl"
    log_path = tmp_path / "log.jsonl"

    from workers.digest_builder import run
    result = run(
        vault_root=vault,
        k=3,
        dry_run=True,
        send_wa=False,
        queue_path=queue_path,
        log_path=log_path,
    )

    assert result["dry_run"] is True
    assert len(result["items"]) == 3
    assert not queue_path.exists()
    assert not log_path.exists()


@patch("workers.digest_builder.generate_reasons", side_effect=_mock_generate_reasons)
def test_digest_builder_writes_queue_and_log(mock_reasons, tmp_path):
    """Full run should write queue and log files."""
    vault = _make_vault_with_notes(tmp_path)
    queue_path = tmp_path / "queue.jsonl"
    log_path = tmp_path / "log.jsonl"

    from workers.digest_builder import run
    result = run(
        vault_root=vault,
        k=3,
        dry_run=False,
        send_wa=False,
        queue_path=queue_path,
        log_path=log_path,
    )

    assert queue_path.exists(), "Queue file not written"
    assert log_path.exists(), "Log file not written"

    queue_lines = queue_path.read_text().strip().splitlines()
    assert len(queue_lines) == 3

    log_lines = log_path.read_text().strip().splitlines()
    assert len(log_lines) == 3

    # Verify queue rows have required fields
    row = json.loads(queue_lines[0])
    assert "slug" in row
    assert "title" in row
    assert "reason" in row


@patch("workers.digest_builder.generate_reasons", side_effect=_mock_generate_reasons)
def test_digest_builder_idempotent_dates(mock_reasons, tmp_path):
    """Running for the same date twice should overwrite (not append) the queue."""
    vault = _make_vault_with_notes(tmp_path)
    queue_path = tmp_path / "queue.jsonl"
    log_path = tmp_path / "log.jsonl"

    from workers.digest_builder import run
    today = date(2026, 5, 29)

    run(vault_root=vault, k=3, dry_run=False, send_wa=False,
        queue_path=queue_path, log_path=log_path, digest_date=today)
    run(vault_root=vault, k=3, dry_run=False, send_wa=False,
        queue_path=queue_path, log_path=log_path, digest_date=today)

    # Queue should be 3 lines (overwritten), not 6
    queue_lines = queue_path.read_text().strip().splitlines()
    assert len(queue_lines) == 3


@patch("workers.digest_builder.generate_reasons", side_effect=_mock_generate_reasons)
def test_digest_builder_writes_markdown_to_vault(mock_reasons, tmp_path):
    """Full run should write a markdown digest file under vault/digests/."""
    vault = _make_vault_with_notes(tmp_path)
    queue_path = tmp_path / "queue.jsonl"
    log_path = tmp_path / "log.jsonl"

    from workers.digest_builder import run
    today = date(2026, 5, 29)
    result = run(vault_root=vault, k=3, dry_run=False, send_wa=False,
                 queue_path=queue_path, log_path=log_path, digest_date=today)

    digest_file = vault / "digests" / "2026-05-29.md"
    assert digest_file.exists(), "Digest markdown not written"
    content = digest_file.read_text()
    assert "Daily Digest" in content
    assert "2026-05-29" in content
