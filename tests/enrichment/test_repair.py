"""Tests for `connecting_dots.enrichment.repair`."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from connecting_dots.enrichment import repair


def _write_note(
    path: Path,
    *,
    entities: list,
    topics: list,
    ner_enriched_at: str | None = "2026-05-28T12:00:00Z",
    ner_model: str | None = "claude-haiku-4-5",
    extra_raw_meta: dict | None = None,
) -> None:
    raw_meta: dict = {}
    if ner_enriched_at is not None:
        raw_meta["ner_enriched_at"] = ner_enriched_at
    if ner_model is not None:
        raw_meta["ner_model"] = ner_model
    if extra_raw_meta:
        raw_meta.update(extra_raw_meta)
    fm = {
        "source": "whatsapp",
        "handler": "youtube",
        "captured_at": "2026-05-28T12:00:00Z",
        "url": "",
        "title": path.stem,
        "entities": entities,
        "topics": topics,
        "labels": [],
    }
    if raw_meta:
        fm["raw_meta"] = raw_meta
    fm_text = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm_text}\n---\n\n# {path.stem}\n\nbody\n", encoding="utf-8")


def _parse(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", raw, flags=re.DOTALL)
    assert m
    return yaml.safe_load(m.group(1))


def test_repair_unsticks_only_empty_enriched_notes(tmp_path):
    # 3 stuck: marker set, but no entities + no topics.
    _write_note(tmp_path / "stuck1.md", entities=[], topics=[])
    _write_note(tmp_path / "sub" / "stuck2.md", entities=[], topics=[])
    _write_note(tmp_path / "stuck3.md", entities=[], topics=[])
    # 2 healthy: marker set with real findings — must be left alone.
    _write_note(tmp_path / "healthy1.md", entities=["Naval"], topics=[])
    _write_note(tmp_path / "healthy2.md", entities=[], topics=["wealth"])
    # 1 unenriched (no marker) — must be left alone.
    _write_note(
        tmp_path / "fresh.md",
        entities=[],
        topics=[],
        ner_enriched_at=None,
        ner_model=None,
    )

    count = repair.clear_failed_enrichment_markers(tmp_path)
    assert count == 3

    for name in ("stuck1.md", "sub/stuck2.md", "stuck3.md"):
        fm = _parse(tmp_path / name)
        raw_meta = fm.get("raw_meta") or {}
        assert "ner_enriched_at" not in raw_meta
        assert "ner_model" not in raw_meta

    for name in ("healthy1.md", "healthy2.md"):
        fm = _parse(tmp_path / name)
        assert fm["raw_meta"]["ner_enriched_at"]
        assert fm["raw_meta"]["ner_model"]


def test_repair_preserves_other_raw_meta_keys(tmp_path):
    _write_note(
        tmp_path / "stuck.md",
        entities=[],
        topics=[],
        extra_raw_meta={"source_url": "https://example.com", "ner_error": "old"},
    )
    count = repair.clear_failed_enrichment_markers(tmp_path)
    assert count == 1
    fm = _parse(tmp_path / "stuck.md")
    raw_meta = fm["raw_meta"]
    assert "ner_enriched_at" not in raw_meta
    assert "ner_model" not in raw_meta
    assert raw_meta["source_url"] == "https://example.com"
    assert raw_meta["ner_error"] == "old"


def test_repair_empty_vault_returns_zero(tmp_path):
    assert repair.clear_failed_enrichment_markers(tmp_path) == 0


def test_repair_nonexistent_vault_returns_zero(tmp_path):
    assert repair.clear_failed_enrichment_markers(tmp_path / "nope") == 0


def test_repair_cli_prints_count(tmp_path, capsys):
    _write_note(tmp_path / "stuck.md", entities=[], topics=[])
    rc = repair.main(["--vault", str(tmp_path)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "1 unstuck" in captured.out
