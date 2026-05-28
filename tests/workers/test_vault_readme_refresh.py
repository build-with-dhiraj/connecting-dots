"""Tests for workers.vault_readme_refresh.

Covers:
- test_counts_notes_per_folder_correctly
- test_templates_metric_table
- test_llm_call_mocked_for_synthesis
- test_no_llm_flag_uses_static_paragraph
- test_dry_run_writes_nothing
- test_atomic_write
- test_preserves_obsidian_friendly_wikilink_targets
- test_handles_empty_folder_gracefully
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from workers.vault_readme_refresh import (
    _write_atomic,
    count_notes,
    main,
    render_readme,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _make_vault(tmp_path: Path, structure: dict[str, int]) -> Path:
    """Create a tmp vault tree with the given note counts per folder key."""
    folder_map = {
        "web": tmp_path / "sources" / "web",
        "youtube": tmp_path / "sources" / "youtube",
        "linkedin": tmp_path / "sources" / "linkedin",
        "instagram": tmp_path / "sources" / "instagram",
        "whatsapp": tmp_path / "sources" / "whatsapp",
        "inbox": tmp_path / "inbox",
        "themes": tmp_path / "themes",
        "digests": tmp_path / "digests",
    }
    for key, count in structure.items():
        folder = folder_map[key]
        folder.mkdir(parents=True, exist_ok=True)
        for i in range(count):
            (folder / f"note-{i}.md").write_text(f"# Note {i}\n", encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------------- #
# 1. count_notes_per_folder_correctly
# --------------------------------------------------------------------------- #
def test_counts_notes_per_folder_correctly(tmp_path, monkeypatch):
    vault = _make_vault(
        tmp_path,
        {"web": 5, "youtube": 3, "linkedin": 2, "instagram": 1, "whatsapp": 0, "inbox": 4, "themes": 2, "digests": 0},
    )
    monkeypatch.setenv("CONNECTING_DOTS_VAULT_ROOT", str(vault))

    from workers.vault_readme_refresh import _resolve_vault_root
    stats = count_notes(_resolve_vault_root())

    assert stats["web"] == 5
    assert stats["youtube"] == 3
    assert stats["linkedin"] == 2
    assert stats["instagram"] == 1
    assert stats["whatsapp"] == 0
    assert stats["inbox"] == 4
    assert stats["themes"] == 2
    assert stats["digests"] == 0
    assert stats["total"] == 5 + 3 + 2 + 1 + 0 + 4 + 2 + 0


# --------------------------------------------------------------------------- #
# 2. test_templates_metric_table
# --------------------------------------------------------------------------- #
def test_templates_metric_table():
    stats = {
        "total": 100, "web": 60, "inbox": 10, "linkedin": 8,
        "youtube": 5, "instagram": 2, "whatsapp": 5, "themes": 10, "digests": 0,
    }
    content = render_readme(stats=stats, overview="Test overview.", date="2026-05-29")

    assert "| Total notes | 100 |" in content
    assert "| Web articles | 60 |" in content
    assert "| LinkedIn posts | 8 |" in content
    assert "| YouTube transcripts | 5 |" in content
    assert "| MoC theme pages | 10 |" in content
    assert "2026-05-29" in content


# --------------------------------------------------------------------------- #
# 3. test_llm_call_mocked_for_synthesis
# --------------------------------------------------------------------------- #
def test_llm_call_mocked_for_synthesis():
    mock_content = "This is the LLM-generated overview paragraph."

    fake_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=mock_content)
            )
        ]
    )
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = fake_response

    from connecting_dots.enrichment.vault_readme_synth import synthesise_overview
    result = synthesise_overview(stats={"total": 50}, client=mock_client)

    assert result == mock_content
    mock_client.chat.completions.create.assert_called_once()


# --------------------------------------------------------------------------- #
# 4. test_no_llm_flag_uses_static_paragraph
# --------------------------------------------------------------------------- #
def test_no_llm_flag_uses_static_paragraph(tmp_path, monkeypatch):
    vault = _make_vault(tmp_path, {"web": 1, "inbox": 0, "youtube": 0, "linkedin": 0,
                                    "instagram": 0, "whatsapp": 0, "themes": 0, "digests": 0})
    monkeypatch.setenv("CONNECTING_DOTS_VAULT_ROOT", str(vault))

    from connecting_dots.enrichment.vault_readme_synth import _STATIC_PARAGRAPH

    with patch("connecting_dots.enrichment.vault_readme_synth.synthesise_overview") as mock_synth:
        rc = main(["--no-llm"])

    # synthesise_overview should NOT have been called
    mock_synth.assert_not_called()
    assert rc == 0

    readme = vault / "README.md"
    content = readme.read_text(encoding="utf-8")
    assert _STATIC_PARAGRAPH in content


# --------------------------------------------------------------------------- #
# 5. test_dry_run_writes_nothing
# --------------------------------------------------------------------------- #
def test_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    vault = _make_vault(tmp_path, {"web": 2, "inbox": 1, "youtube": 0, "linkedin": 0,
                                    "instagram": 0, "whatsapp": 0, "themes": 1, "digests": 0})
    monkeypatch.setenv("CONNECTING_DOTS_VAULT_ROOT", str(vault))

    rc = main(["--no-llm", "--dry-run"])

    assert rc == 0
    readme = vault / "README.md"
    assert not readme.exists()

    captured = capsys.readouterr()
    assert "Connecting Dots" in captured.out


# --------------------------------------------------------------------------- #
# 6. test_atomic_write
# --------------------------------------------------------------------------- #
def test_atomic_write(tmp_path):
    target = tmp_path / "out" / "README.md"
    content = "# Hello\n\nWorld.\n"

    _write_atomic(target, content)

    assert target.exists()
    assert target.read_text(encoding="utf-8") == content
    # No tmp files left behind
    leftover = list(tmp_path.rglob(".tmp-readme-*"))
    assert leftover == []


# --------------------------------------------------------------------------- #
# 7. test_preserves_obsidian_friendly_wikilink_targets
# --------------------------------------------------------------------------- #
def test_preserves_obsidian_friendly_wikilink_targets():
    stats = {k: 0 for k in ("total", "web", "inbox", "linkedin", "youtube",
                              "instagram", "whatsapp", "themes", "digests")}
    content = render_readme(stats=stats, overview="Overview.", date="2026-01-01")

    # Key wikilinks must be present and properly formatted
    assert "[[by-topic.base]]" in content
    assert "[[financial-performance]]" in content
    assert "[[product-management]]" in content
    assert "[[design-systems]]" in content
    assert "[[hiring]]" in content


# --------------------------------------------------------------------------- #
# 8. test_handles_empty_folder_gracefully
# --------------------------------------------------------------------------- #
def test_handles_empty_folder_gracefully(tmp_path, monkeypatch):
    # vault root with NO sub-folders at all
    (tmp_path / "vault_empty").mkdir()
    monkeypatch.setenv("CONNECTING_DOTS_VAULT_ROOT", str(tmp_path / "vault_empty"))

    from workers.vault_readme_refresh import _resolve_vault_root
    stats = count_notes(_resolve_vault_root())

    assert stats["total"] == 0
    assert stats["web"] == 0
    assert stats["inbox"] == 0

    # Should still render without error
    content = render_readme(stats=stats, overview="Empty vault.", date="2026-01-01")
    assert "| Total notes | 0 |" in content
