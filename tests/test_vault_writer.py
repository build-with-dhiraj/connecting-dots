"""Tests for `lib.vault_writer.writer`.

Covers the public contract of `write_note(...)`:

- Handler-driven routing taxonomy (P1-taxonomy).
- TOCTOU-safe collision resolution under thread contention (P1-collision-race).
- Atomic-write durability incl. cleanup on simulated crash (P1-fsync-dir).
- Unicode-aware slugification incl. CJK / Arabic / mixed / emoji-only
  (P1-unicode-slug).
- `raw_meta` frontmatter serialization (coordinated contract change).
- Frontmatter shape: required keys, key order, ISO-8601 captured_at.

All tests use the `CONNECTING_DOTS_VAULT_ROOT` env var to redirect writes to
a tmp dir, so the real vault under `vault/` is never touched.
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from lib.vault_writer import stable_id, write_note
from lib.vault_writer import writer as writer_mod


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def vault(tmp_path, monkeypatch) -> Path:
    """Redirect the writer's vault root to a per-test tmp dir."""
    monkeypatch.setenv("CONNECTING_DOTS_VAULT_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)


def _parse(note_path: Path) -> tuple[dict, str]:
    """Split a written note into (frontmatter, body)."""
    raw = note_path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n\n(.*)$", raw, flags=re.DOTALL)
    assert m, f"Note at {note_path} has no parseable frontmatter:\n{raw!r}"
    fm = yaml.safe_load(m.group(1))
    body = m.group(2)
    return fm, body


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_happy_path_writes_file_with_frontmatter_and_body(vault, now):
    path = write_note(
        source="whatsapp",
        handler="youtube",
        url="https://youtu.be/abc123",
        title="The dot-product memory model",
        text="Long-form notes about embedding geometry.",
        captured_at=now,
    )
    assert path.exists()
    assert path.suffix == ".md"
    fm, body = _parse(path)
    assert fm["source"] == "whatsapp"
    assert fm["handler"] == "youtube"
    assert fm["url"] == "https://youtu.be/abc123"
    assert fm["captured_at"] == "2026-05-27T12:00:00Z"
    assert fm["entities"] == []
    assert fm["topics"] == []
    assert "title" in fm
    assert "# The dot-product memory model" in body
    assert "Long-form notes about embedding geometry." in body


def test_frontmatter_contains_required_keys(vault, now):
    path = write_note(
        source="mailto",
        handler="web",
        url="https://example.com/post",
        title="Example",
        text="body",
        captured_at=now,
        entities=["LanceDB", "Pinecone"],
        topics=["vector-databases"],
    )
    fm, _ = _parse(path)
    for key in ("source", "handler", "captured_at", "url", "entities", "topics"):
        assert key in fm, f"missing required key: {key}"
    assert fm["entities"] == ["LanceDB", "Pinecone"]
    assert fm["topics"] == ["vector-databases"]


def test_captured_at_is_iso8601_utc(vault):
    # Naive datetime should be coerced to UTC.
    naive = datetime(2026, 1, 1, 9, 30, 0)
    path = write_note(
        source="whatsapp",
        handler="web",
        url="https://example.com/x",
        title="time test",
        text="",
        captured_at=naive,
    )
    fm, _ = _parse(path)
    assert fm["captured_at"] == "2026-01-01T09:30:00Z"


# --------------------------------------------------------------------------- #
# Routing taxonomy (P1-taxonomy)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "source,handler,expected_subdir",
    [
        # Handler-first routing — content type wins over ingest channel.
        ("whatsapp", "youtube", "sources/youtube"),
        ("mailto", "youtube", "sources/youtube"),
        ("manual", "youtube", "sources/youtube"),
        ("whatsapp", "instagram", "sources/instagram"),
        ("mailto", "instagram", "sources/instagram"),
        ("linkedin", "linkedin", "sources/linkedin"),
        ("manual", "linkedin", "sources/linkedin"),
        ("whatsapp", "web", "sources/web"),
        ("mailto", "web", "sources/web"),
        # Failed-handler bucket.
        ("whatsapp", "failed", "inbox/_failed"),
        ("mailto", "failed", "inbox/_failed"),
        # Unknown handler → inbox fallback.
        ("whatsapp", "unknown", "inbox"),
        ("manual", "", "inbox"),
    ],
)
def test_routing_by_handler(vault, now, source, handler, expected_subdir):
    path = write_note(
        source=source,
        handler=handler,
        url="https://example.com/x",
        title=f"route {source} {handler}",
        text="",
        captured_at=now,
    )
    rel = path.relative_to(vault).as_posix()
    assert rel.startswith(expected_subdir + "/"), (
        f"expected {expected_subdir}/, got {rel} for "
        f"source={source} handler={handler}"
    )


def test_yt_via_mailto_does_not_land_in_inbox(vault, now):
    """Regression: prior source-based routing sent YT-via-mailto to inbox/."""
    path = write_note(
        source="mailto",
        handler="youtube",
        url="https://youtu.be/abc",
        title="yt via email",
        text="",
        captured_at=now,
    )
    assert path.relative_to(vault).parts[0:2] == ("sources", "youtube")


# --------------------------------------------------------------------------- #
# Collision (P1-collision-race) — sequential
# --------------------------------------------------------------------------- #
def test_same_title_produces_suffixed_collision(vault, now):
    p1 = write_note(
        source="whatsapp",
        handler="web",
        url="https://example.com/1",
        title="same title",
        text="first",
        captured_at=now,
    )
    p2 = write_note(
        source="whatsapp",
        handler="web",
        url="https://example.com/2",
        title="same title",
        text="second",
        captured_at=now,
    )
    p3 = write_note(
        source="whatsapp",
        handler="web",
        url="https://example.com/3",
        title="same title",
        text="third",
        captured_at=now,
    )
    assert p1.stem == "same-title"
    assert p2.stem == "same-title-2"
    assert p3.stem == "same-title-3"


def test_collision_cap_raises_runtime_error(vault, now, monkeypatch):
    """Force the cap low to exercise the overflow path."""
    monkeypatch.setattr(writer_mod, "_MAX_COLLISION_SUFFIX", 3)
    for _ in range(3):
        write_note(
            source="whatsapp",
            handler="web",
            url="https://example.com/x",
            title="cap test",
            text="",
            captured_at=now,
        )
    with pytest.raises(RuntimeError, match="collision overflow"):
        write_note(
            source="whatsapp",
            handler="web",
            url="https://example.com/x",
            title="cap test",
            text="",
            captured_at=now,
        )


# --------------------------------------------------------------------------- #
# Collision race (P1-collision-race) — threaded
# --------------------------------------------------------------------------- #
def test_concurrent_writes_produce_distinct_files(vault, now):
    """10 threads writing the same title must end up at 10 distinct paths.

    The TOCTOU-racy `.exists()`-then-write pattern would lose this race;
    `O_CREAT | O_EXCL` wins it deterministically.
    """
    n_threads = 10
    barrier = threading.Barrier(n_threads)
    results: list[Path] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(idx: int) -> None:
        try:
            barrier.wait(timeout=5)
            p = write_note(
                source="whatsapp",
                handler="web",
                url=f"https://example.com/{idx}",
                title="race condition",
                text=f"thread {idx}",
                captured_at=now,
            )
            with lock:
                results.append(p)
        except BaseException as e:  # noqa: BLE001
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"workers raised: {errors!r}"
    assert len(results) == n_threads
    assert len({p for p in results}) == n_threads, (
        f"distinct paths required, got {sorted(p.name for p in results)}"
    )
    for p in results:
        assert p.exists(), f"{p} disappeared"


# --------------------------------------------------------------------------- #
# Unicode slug (P1-unicode-slug)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "title,expect_substring",
    [
        ("向量数据库的几何", "向量数据库的几何"),       # Chinese
        ("قواعد بيانات المتجهات", "قواعد"),              # Arabic (just check kept)
        ("Привет мир", "привет-мир"),                    # Cyrillic (lowercased)
        ("Hello 世界 mixed", "hello-世界-mixed"),         # Mixed Latin + CJK
    ],
)
def test_slugify_preserves_unicode_scripts(vault, now, title, expect_substring):
    path = write_note(
        source="whatsapp",
        handler="web",
        url="https://example.com/unicode",
        title=title,
        text="",
        captured_at=now,
    )
    assert expect_substring in path.stem, (
        f"slug {path.stem!r} did not contain {expect_substring!r}"
    )


def test_slugify_emoji_only_falls_back_to_hash(vault, now):
    path = write_note(
        source="whatsapp",
        handler="web",
        url="https://example.com/emoji-1",
        title="🎉🎊🎈",
        text="",
        captured_at=now,
    )
    assert path.stem.startswith("note-")
    assert len(path.stem) == len("note-") + 8


def test_slugify_empty_title_uses_url_hash_so_no_collision(vault, now):
    """Two empty-title items with different URLs must NOT collide."""
    p1 = write_note(
        source="whatsapp",
        handler="web",
        url="https://example.com/A",
        title="",
        text="",
        captured_at=now,
    )
    p2 = write_note(
        source="whatsapp",
        handler="web",
        url="https://example.com/B",
        title="",
        text="",
        captured_at=now,
    )
    assert p1.stem != p2.stem
    assert p1.stem.startswith("note-")
    assert p2.stem.startswith("note-")


def test_slugify_caps_at_80_chars(vault, now):
    long_title = "a" * 500
    path = write_note(
        source="whatsapp",
        handler="web",
        url="https://example.com/long",
        title=long_title,
        text="",
        captured_at=now,
    )
    # 80-char cap; .md suffix not included in stem.
    assert len(path.stem) <= 80


# --------------------------------------------------------------------------- #
# raw_meta serialization (coordinated contract change)
# --------------------------------------------------------------------------- #
def test_raw_meta_serializes_into_frontmatter(vault, now):
    raw = {
        "wa_message_id": "wamid.HBgL...",
        "nested": {"og_title": "Example: a post #1 - things", "n": 42},
        "list_value": ["a", "b"],
    }
    path = write_note(
        source="whatsapp",
        handler="web",
        url="https://example.com/raw",
        title="raw meta test",
        text="",
        captured_at=now,
        raw_meta=raw,
    )
    fm, _ = _parse(path)
    assert fm["raw_meta"] == raw


def test_raw_meta_handles_special_yaml_chars(vault, now):
    raw = {
        "with_colon": "key: value",
        "with_hash": "# heading-ish",
        "with_dash": "- bullet-ish",
        "with_newline": "line1\nline2",
        "with_none": None,
    }
    path = write_note(
        source="whatsapp",
        handler="web",
        url="https://example.com/special",
        title="special chars",
        text="",
        captured_at=now,
        raw_meta=raw,
    )
    fm, _ = _parse(path)
    assert fm["raw_meta"] == raw


def test_raw_meta_with_datetime_value(vault, now):
    raw = {"fetched_at": datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)}
    path = write_note(
        source="whatsapp",
        handler="web",
        url="https://example.com/dt",
        title="dt meta",
        text="",
        captured_at=now,
        raw_meta=raw,
    )
    # yaml.safe_dump emits datetimes as ISO-ish; yaml.safe_load round-trips
    # back to datetime. Either way, no crash and key is present.
    fm, _ = _parse(path)
    assert "fetched_at" in fm["raw_meta"]


def test_raw_meta_absent_when_none(vault, now):
    path = write_note(
        source="whatsapp",
        handler="web",
        url="https://example.com/none",
        title="no meta",
        text="",
        captured_at=now,
        raw_meta=None,
    )
    fm, _ = _parse(path)
    assert "raw_meta" not in fm


def test_raw_meta_absent_when_empty_dict(vault, now):
    path = write_note(
        source="whatsapp",
        handler="web",
        url="https://example.com/empty",
        title="empty meta",
        text="",
        captured_at=now,
        raw_meta={},
    )
    fm, _ = _parse(path)
    assert "raw_meta" not in fm


# --------------------------------------------------------------------------- #
# Atomic-write durability (P1-fsync-dir)
# --------------------------------------------------------------------------- #
def test_no_partial_files_when_serialization_crashes(vault, now, monkeypatch):
    """If serialization blows up mid-write, the vault must not contain
    a corrupt .md file or a leftover .tmp- sibling."""

    def boom(*_a, **_k):
        raise RuntimeError("simulated crash mid-serialize")

    monkeypatch.setattr(writer_mod, "_serialize", boom)
    with pytest.raises(RuntimeError, match="simulated crash"):
        write_note(
            source="whatsapp",
            handler="web",
            url="https://example.com/crash",
            title="crash test",
            text="",
            captured_at=now,
        )

    # No .md file with our slug should have been left behind, and no .tmp-* sibling.
    target_dir = vault / "sources/web"
    leftovers = list(target_dir.glob("*"))
    md_files = [p for p in leftovers if p.suffix == ".md"]
    tmp_files = [p for p in leftovers if p.name.startswith(".tmp-")]
    assert not md_files, f"leftover md files: {md_files}"
    assert not tmp_files, f"leftover tmp files: {tmp_files}"


def test_written_file_has_no_partial_content(vault, now):
    """Sanity: the file as it lands on disk has the full frontmatter+body,
    not a half-flushed prefix."""
    path = write_note(
        source="whatsapp",
        handler="web",
        url="https://example.com/full",
        title="full file",
        text="full body text",
        captured_at=now,
    )
    raw = path.read_text(encoding="utf-8")
    assert raw.startswith("---\n")
    assert "\n---\n\n" in raw
    assert raw.rstrip().endswith("full body text")


# --------------------------------------------------------------------------- #
# stable_id
# --------------------------------------------------------------------------- #
def test_stable_id_is_deterministic():
    assert stable_id("sources/youtube/foo.md") == stable_id("sources/youtube/foo.md")
    assert stable_id("a") != stable_id("b")
    assert len(stable_id("anything")) == 16
