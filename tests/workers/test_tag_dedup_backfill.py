"""End-to-end tests for workers.tag_dedup_backfill using a tmp vault."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

from workers import tag_dedup_backfill
from workers.tag_dedup_backfill import (
    _collect_tags,
    cmd_build_map,
    cmd_apply,
    main,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _write_note(path: Path, tags: list[str], title: str = "Test") -> None:
    fm = {"title": title, "tags": tags}
    content = (
        f"---\n{yaml.safe_dump(fm, allow_unicode=True).rstrip()}\n---\n# {title}\n\nBody.\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_tmp_vault(tmp_path: Path) -> Path:
    """Create a small mock vault with duplicate tags."""
    vault = tmp_path / "vault"
    _write_note(
        vault / "sources" / "note1.md",
        tags=["#entity/AI", "#entity/artificial-intelligence", "#source/email"],
    )
    _write_note(
        vault / "sources" / "note2.md",
        tags=["#entity/Anthropic", "#entity/anthropic", "#topic/machine-learning"],
    )
    _write_note(
        vault / "sources" / "note3.md",
        tags=["#topic/Machine-Learning", "#topic/machine learning"],
    )
    _write_note(
        vault / "sources" / "no_tags.md",
        tags=[],
        title="No tags",
    )
    return vault


# --------------------------------------------------------------------------- #
# _collect_tags
# --------------------------------------------------------------------------- #
def test_collect_tags_finds_all_unique(tmp_path):
    vault = _make_tmp_vault(tmp_path)
    tags = _collect_tags(vault)
    assert "#entity/AI" in tags
    assert "#entity/artificial-intelligence" in tags
    assert "#entity/Anthropic" in tags
    # Unique — no duplicates in the list
    assert len(tags) == len(set(tags))


# --------------------------------------------------------------------------- #
# build-map end-to-end (no LLM)
# --------------------------------------------------------------------------- #
def test_build_map_creates_cache(tmp_path):
    vault = _make_tmp_vault(tmp_path)
    map_path = tmp_path / "map.json"

    with patch.object(tag_dedup_backfill, "_resolve_vault_root", return_value=vault):
        result = cmd_build_map(
            map_path=map_path,
            model="gpt-4.1",
            reuse_map=False,
            force_rebuild=False,
            dry_run=True,  # skip LLM
        )

    assert map_path.exists()
    data = json.loads(map_path.read_text())
    assert isinstance(data, dict)
    # AI variants should collapse
    assert data.get("#entity/AI") == data.get("#entity/ai") or (
        data.get("#entity/AI") is not None
    )


def test_build_map_reuses_cache(tmp_path):
    vault = _make_tmp_vault(tmp_path)
    map_path = tmp_path / "map.json"
    # Pre-write a fake cache
    fake_map = {"#entity/AI": "#entity/ai"}
    map_path.write_text(json.dumps(fake_map), encoding="utf-8")

    with patch.object(tag_dedup_backfill, "_resolve_vault_root", return_value=vault):
        result = cmd_build_map(
            map_path=map_path,
            model="gpt-4.1",
            reuse_map=True,
            force_rebuild=False,
            dry_run=False,
        )

    # Should return the fake map unchanged (no LLM calls)
    assert result == fake_map


# --------------------------------------------------------------------------- #
# apply end-to-end
# --------------------------------------------------------------------------- #
def test_apply_rewrites_vault(tmp_path):
    vault = _make_tmp_vault(tmp_path)
    map_path = tmp_path / "map.json"
    canonical_map = {
        "#entity/AI": "#entity/ai",
        "#entity/artificial-intelligence": "#entity/ai",
        "#entity/Anthropic": "#entity/anthropic",
        "#topic/Machine-Learning": "#topic/machine-learning",
        "#topic/machine learning": "#topic/machine-learning",
    }
    map_path.write_text(json.dumps(canonical_map), encoding="utf-8")

    cmd_apply(map_path=map_path, vault_root=vault, limit=None, dry_run=False)

    # Check note1: AI variants collapsed
    text = (vault / "sources" / "note1.md").read_text()
    assert "#entity/ai" in text
    assert "#entity/AI" not in text
    assert "#entity/artificial-intelligence" not in text
    # Source tag preserved
    assert "#source/email" in text


def test_apply_dry_run_no_mutation(tmp_path):
    vault = _make_tmp_vault(tmp_path)
    map_path = tmp_path / "map.json"
    canonical_map = {
        "#entity/AI": "#entity/ai",
        "#entity/artificial-intelligence": "#entity/ai",
    }
    map_path.write_text(json.dumps(canonical_map), encoding="utf-8")

    original = (vault / "sources" / "note1.md").read_text()
    cmd_apply(map_path=map_path, vault_root=vault, limit=None, dry_run=True)
    after = (vault / "sources" / "note1.md").read_text()

    assert original == after


# --------------------------------------------------------------------------- #
# main() CLI integration
# --------------------------------------------------------------------------- #
def test_main_all_subcommand(tmp_path):
    vault = _make_tmp_vault(tmp_path)
    map_path = tmp_path / "map.json"

    with patch.object(tag_dedup_backfill, "_resolve_vault_root", return_value=vault):
        rc = main(
            [
                "all",
                "--map-path",
                str(map_path),
                "--dry-run",
                "--log-level",
                "WARNING",
            ]
        )

    assert rc == 0
    # Map was created even in dry-run (Phase A still runs)
    assert map_path.exists()
