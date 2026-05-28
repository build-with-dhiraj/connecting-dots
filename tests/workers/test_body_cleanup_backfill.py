"""Integration tests for `workers.body_cleanup_backfill`.

Creates a tmp vault with synthetic notes, runs the backfill with the LLM
mocked, and verifies all skip conditions, mutation logic, and concurrency.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from connecting_dots.enrichment.body_cleanup import BodyCleanupResult
from workers import body_cleanup_backfill


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def tmp_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("CONNECTING_DOTS_VAULT_ROOT", str(tmp_path))
    (tmp_path / "sources" / "web").mkdir(parents=True)
    (tmp_path / "sources" / "youtube").mkdir(parents=True)
    (tmp_path / "inbox").mkdir(parents=True)
    (tmp_path / "inbox" / "_failed").mkdir(parents=True)
    return tmp_path


def _write_note(
    path: Path,
    *,
    handler: str = "web",
    body: str = "",
    raw_meta: dict | None = None,
) -> None:
    fm: dict = {
        "source": "web",
        "handler": handler,
        "captured_at": "2026-05-28T12:00:00Z",
        "url": "https://example.com/article",
        "title": "Test Article",
    }
    rm: dict = {"handler": handler}
    if raw_meta:
        rm.update(raw_meta)
    fm["raw_meta"] = rm
    fm_text = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm_text}\n---\n\n{body}\n", encoding="utf-8")


def _parse(path: Path) -> tuple[dict, str]:
    raw = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, flags=re.DOTALL)
    assert m, f"No frontmatter found in {path}"
    return yaml.safe_load(m.group(1)), m.group(2)


def _make_ok_result(cleaned: str | None = None) -> BodyCleanupResult:
    if cleaned is None:
        # Default: enough chars to pass the 10% truncation guard against 1000-char bodies
        cleaned = "# Article\n\nClean content. " + "X" * 200
    return BodyCleanupResult(
        cleaned_markdown=cleaned,
        removed_kinds=["cookie", "navigation"],
        removed_count=2,
    )


# --------------------------------------------------------------------------- #
# Skip: non-web handler
# --------------------------------------------------------------------------- #
def test_extract_skips_non_web_handler(tmp_vault):
    note = tmp_vault / "sources" / "youtube" / "video.md"
    long_body = "A" * 1000
    _write_note(note, handler="youtube", body=long_body)

    call_count = {"n": 0}

    def _counting_clean(**kw):
        call_count["n"] += 1
        return _make_ok_result()

    with patch("workers.body_cleanup_backfill.clean_body", side_effect=_counting_clean):
        body_cleanup_backfill.main(["--limit", "10", "--concurrency", "1"])

    assert call_count["n"] == 0
    # File is untouched
    fm, _ = _parse(note)
    assert not (fm.get("raw_meta") or {}).get("body_cleaned_at")


# --------------------------------------------------------------------------- #
# Skip: short body
# --------------------------------------------------------------------------- #
def test_extract_skips_short_body(tmp_vault):
    note = tmp_vault / "sources" / "web" / "short.md"
    _write_note(note, body="Too short.")  # << 800 chars

    call_count = {"n": 0}

    def _counting_clean(**kw):
        call_count["n"] += 1
        return _make_ok_result()

    with patch("workers.body_cleanup_backfill.clean_body", side_effect=_counting_clean):
        body_cleanup_backfill.main(["--limit", "10", "--concurrency", "1"])

    assert call_count["n"] == 0


# --------------------------------------------------------------------------- #
# Skip: already cleaned (idempotent)
# --------------------------------------------------------------------------- #
def test_extract_skips_already_cleaned(tmp_vault):
    note = tmp_vault / "sources" / "web" / "done.md"
    long_body = "A" * 1000
    _write_note(note, body=long_body, raw_meta={"body_cleaned_at": "2026-05-28T00:00:00Z"})

    call_count = {"n": 0}

    def _counting_clean(**kw):
        call_count["n"] += 1
        return _make_ok_result()

    with patch("workers.body_cleanup_backfill.clean_body", side_effect=_counting_clean):
        body_cleanup_backfill.main(["--limit", "10", "--concurrency", "1"])

    assert call_count["n"] == 0


# --------------------------------------------------------------------------- #
# TL;DR blockquote preserved
# --------------------------------------------------------------------------- #
def test_preserves_tldr_blockquote_at_top(tmp_vault, monkeypatch):
    note = tmp_vault / "sources" / "web" / "article.md"
    real_content = "A" * 900
    body_with_tldr = f"> **TL;DR.** Summary sentence one. Summary sentence two.\n\n{real_content}"
    _write_note(note, body=body_with_tldr)

    cleaned_content = "# Article\n\nClean body here. " + "Y" * 200
    monkeypatch.setattr(
        "workers.body_cleanup_backfill.clean_body",
        lambda **kw: _make_ok_result(cleaned_content),
    )

    result = body_cleanup_backfill._process_one_sync(
        note, model=None, vault_root=tmp_vault, dry_run=False
    )
    assert result["status"] == "ok"
    text = note.read_text(encoding="utf-8")
    # TL;DR must appear before cleaned body
    tldr_pos = text.find("> **TL;DR.**")
    clean_pos = text.find("Clean body here.")
    assert tldr_pos != -1, "TL;DR blockquote missing"
    assert clean_pos != -1, "Cleaned body missing"
    assert tldr_pos < clean_pos, "TL;DR must precede cleaned body"


# --------------------------------------------------------------------------- #
# H1 title preserved
# --------------------------------------------------------------------------- #
def test_preserves_h1_title(tmp_vault, monkeypatch):
    note = tmp_vault / "sources" / "web" / "article2.md"
    body = "A" * 900
    _write_note(note, body=body)

    cleaned_content = "# My Article Title\n\nClean content here. " + "Z" * 200
    monkeypatch.setattr(
        "workers.body_cleanup_backfill.clean_body",
        lambda **kw: _make_ok_result(cleaned_content),
    )

    result = body_cleanup_backfill._process_one_sync(
        note, model=None, vault_root=tmp_vault, dry_run=False
    )
    assert result["status"] == "ok"
    text = note.read_text(encoding="utf-8")
    assert "# My Article Title" in text


# --------------------------------------------------------------------------- #
# Suspicious truncation
# --------------------------------------------------------------------------- #
def test_suspicious_truncation_skips_write(tmp_vault, monkeypatch):
    note = tmp_vault / "sources" / "web" / "truncated.md"
    long_body = "A" * 1000  # 1000 chars
    _write_note(note, body=long_body)

    # Return only 50 chars — 5% of 1000, well below 10% threshold
    monkeypatch.setattr(
        "workers.body_cleanup_backfill.clean_body",
        lambda **kw: _make_ok_result("X" * 50),
    )

    result = body_cleanup_backfill._process_one_sync(
        note, model=None, vault_root=tmp_vault, dry_run=False
    )
    assert result["status"] == "suspicious_truncation"
    fm, _ = _parse(note)
    assert fm["raw_meta"].get("body_cleanup_skipped") == "suspicious_truncation"
    # Original body must not be replaced
    _, body = _parse(note)
    assert long_body in body


# --------------------------------------------------------------------------- #
# Atomic write preserves frontmatter
# --------------------------------------------------------------------------- #
def test_atomic_write_preserves_frontmatter(tmp_vault, monkeypatch):
    note = tmp_vault / "sources" / "web" / "fm_test.md"
    long_body = "B" * 1000
    _write_note(note, body=long_body)

    cleaned_content = "# Clean\n\nContent. " + "W" * 200
    monkeypatch.setattr(
        "workers.body_cleanup_backfill.clean_body",
        lambda **kw: _make_ok_result(cleaned_content),
    )

    result = body_cleanup_backfill._process_one_sync(
        note, model=None, vault_root=tmp_vault, dry_run=False
    )
    assert result["status"] == "ok"
    fm, _ = _parse(note)
    assert fm["handler"] == "web"
    assert fm["raw_meta"]["body_cleaned_at"]
    assert fm["raw_meta"]["body_cleaned_removed"] == ["cookie", "navigation"]


# --------------------------------------------------------------------------- #
# Concurrency default = 3
# --------------------------------------------------------------------------- #
def test_concurrency_default_3():
    assert body_cleanup_backfill.DEFAULT_CONCURRENCY == 3


# --------------------------------------------------------------------------- #
# Dry run — no mutation
# --------------------------------------------------------------------------- #
def test_dry_run_no_mutation(tmp_vault):
    note = tmp_vault / "sources" / "web" / "dry.md"
    long_body = "C" * 1000
    _write_note(note, body=long_body)
    original_text = note.read_text(encoding="utf-8")

    call_count = {"n": 0}

    def _counting_clean(**kw):
        call_count["n"] += 1
        return _make_ok_result()

    with patch("workers.body_cleanup_backfill.clean_body", side_effect=_counting_clean):
        body_cleanup_backfill.main(["--limit", "10", "--dry-run"])

    assert call_count["n"] == 0
    assert note.read_text(encoding="utf-8") == original_text


# --------------------------------------------------------------------------- #
# removed_kinds stamped in raw_meta
# --------------------------------------------------------------------------- #
def test_removed_kinds_stamped_in_raw_meta(tmp_vault, monkeypatch):
    note = tmp_vault / "sources" / "web" / "kinds.md"
    long_body = "D" * 1000
    _write_note(note, body=long_body)

    result_obj = BodyCleanupResult(
        cleaned_markdown="# Article\n\nClean. " + "V" * 200,
        removed_kinds=["cookie", "newsletter", "ad"],
        removed_count=3,
    )
    monkeypatch.setattr(
        "workers.body_cleanup_backfill.clean_body",
        lambda **kw: result_obj,
    )

    body_cleanup_backfill._process_one_sync(
        note, model=None, vault_root=tmp_vault, dry_run=False
    )
    fm, _ = _parse(note)
    assert fm["raw_meta"]["body_cleaned_removed"] == ["cookie", "newsletter", "ad"]


# --------------------------------------------------------------------------- #
# Resumes after partial run
# --------------------------------------------------------------------------- #
def test_resumes_after_partial_run(tmp_vault):
    """A note already cleaned is skipped; only the uncleaned one is processed."""
    done = tmp_vault / "sources" / "web" / "done.md"
    pending = tmp_vault / "sources" / "web" / "pending.md"
    long_body = "E" * 1000

    _write_note(done, body=long_body, raw_meta={"body_cleaned_at": "2026-05-28T00:00:00Z"})
    _write_note(pending, body=long_body)

    call_count = {"n": 0}

    def _counting_clean(**kw):
        call_count["n"] += 1
        return _make_ok_result()

    with patch("workers.body_cleanup_backfill.clean_body", side_effect=_counting_clean):
        body_cleanup_backfill.main(["--limit", "10", "--concurrency", "1"])

    assert call_count["n"] == 1  # only pending processed
    fm_done, _ = _parse(done)
    assert fm_done["raw_meta"]["body_cleaned_at"] == "2026-05-28T00:00:00Z"  # unchanged
