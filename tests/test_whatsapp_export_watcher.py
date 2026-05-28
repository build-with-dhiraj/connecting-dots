"""End-to-end tests for `workers.whatsapp_export_watcher`.

We synthesize WhatsApp Export Chat ZIPs in tmp dirs — no network, no real
export needed. The dispatcher is mocked so we assert dispatch plans
directly, plus a separate test runs the real dispatcher with a tmp
dedupe DB + tmp vault to verify the integration (idempotency, dedupe).
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import pytest

from workers import whatsapp_export_watcher as w


# --------------------------------------------------------------------------- #
# Fixtures: synthesize WA-export ZIPs
# --------------------------------------------------------------------------- #


IOS_TRANSCRIPT = (
    "‎[15/01/2026, 09:00:00 AM] Dhiraj: Messages and calls are end-to-end encrypted. Tap to learn more.\n"
    "‎[15/01/2026, 09:01:00 AM] Dhiraj: https://youtu.be/abc123\n"
    "‎[15/01/2026, 09:02:00 AM] Dhiraj: ‎<attached: IMG-20260115-WA0001.jpg>\n"
    "look at this sunset\n"
    "‎[15/01/2026, 09:03:00 AM] Dhiraj: just a random thought\n"
    "‎[15/01/2026, 09:04:00 AM] Dhiraj: ‎<attached: PTT-20260115-WA0002.opus>\n"
    "‎[15/01/2026, 09:05:00 AM] Dhiraj: This message was deleted\n"
)


def _make_export_zip(
    path: Path,
    *,
    transcript: str = IOS_TRANSCRIPT,
    transcript_filename: str = "_chat.txt",
    media_files: dict[str, bytes] | None = None,
) -> Path:
    """Mint a tmp ZIP that mimics WA's Export Chat (with Media) output."""
    if media_files is None:
        media_files = {
            "IMG-20260115-WA0001.jpg": b"\xff\xd8\xff\xe0fakejpeg",
            "PTT-20260115-WA0002.opus": b"OggSfakeopus",
        }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(transcript_filename, transcript)
        for name, content in media_files.items():
            zf.writestr(name, content)
    return path


# --------------------------------------------------------------------------- #
# Dispatch sink: capture plans for assertion
# --------------------------------------------------------------------------- #


class _PlanSink:
    """Drop-in for `_default_dispatch` that records every plan."""

    def __init__(self) -> None:
        self.plans: list[w._DispatchPlan] = []

    def __call__(self, plan: w._DispatchPlan, **_kwargs: Any) -> None:
        self.plans.append(plan)


# --------------------------------------------------------------------------- #
# Watcher end-to-end (dispatch mocked)
# --------------------------------------------------------------------------- #


def test_sweep_dispatches_url_text_and_media(tmp_path: Path) -> None:
    inbox = tmp_path / "wa-exports"
    inbox.mkdir()
    _make_export_zip(inbox / "chat_export_2026-01.zip")

    sink = _PlanSink()
    n = w.sweep_once(inbox, dispatch=sink)

    # IOS_TRANSCRIPT has: encryption-banner (skipped), url, image, text,
    # audio, deleted (skipped). Expected dispatched = 4.
    assert n == 4
    types = [p.message_type for p in sink.plans]
    assert types == ["url", "image", "text", "audio"]

    # All synthetic message_ids are deterministic + namespaced.
    assert all(p.message_id.startswith("whatsapp_export:") for p in sink.plans)
    # raw_payload carries the export marker on every plan.
    for p in sink.plans:
        assert p.envelope_json["raw_payload"]["export_source"] is True
        assert p.envelope_json["source"] == "whatsapp"


def test_media_envelope_carries_local_media_path(tmp_path: Path) -> None:
    inbox = tmp_path / "wa-exports"
    inbox.mkdir()
    _make_export_zip(inbox / "x.zip")
    sink = _PlanSink()
    w.sweep_once(inbox, dispatch=sink)

    image_plan = next(p for p in sink.plans if p.message_type == "image")
    local_path = image_plan.envelope_json["local_media_path"]
    assert local_path.endswith("IMG-20260115-WA0001.jpg")
    assert Path(local_path).exists(), "local_media_path must point to a real extracted file"
    # media_id is synthetic (no Meta media id available for export envelopes).
    assert image_plan.envelope_json["media_id"].startswith("local:")
    assert image_plan.envelope_json["media_filename"] == "IMG-20260115-WA0001.jpg"


def test_processed_zip_is_moved_into_timestamped_subdir(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    zp = _make_export_zip(inbox / "chat.zip")

    sink = _PlanSink()
    w.sweep_once(inbox, dispatch=sink)

    assert not zp.exists(), "processed zip should have moved out of the inbox root"
    processed_root = inbox / ".processed"
    # Exactly one timestamped subdir, containing the original zip name.
    subdirs = list(processed_root.iterdir())
    assert len(subdirs) == 1
    assert (subdirs[0] / "chat.zip").exists()
    # Extraction artefacts present too.
    assert any((inbox / ".unpacked").iterdir())


def test_idempotency_on_reimport(tmp_path: Path) -> None:
    """Same export dropped twice → identical synthetic message_ids."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _make_export_zip(inbox / "a.zip")
    sink1 = _PlanSink()
    w.sweep_once(inbox, dispatch=sink1)

    _make_export_zip(inbox / "b.zip")
    sink2 = _PlanSink()
    w.sweep_once(inbox, dispatch=sink2)

    ids1 = sorted(p.message_id for p in sink1.plans)
    ids2 = sorted(p.message_id for p in sink2.plans)
    assert ids1 == ids2, "deterministic message_ids must match across re-imports"


def test_malformed_zip_is_skipped_not_crashed(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    bad = inbox / "not-really-a-zip.zip"
    bad.write_bytes(b"this is not a zip file at all")

    sink = _PlanSink()
    n = w.sweep_once(inbox, dispatch=sink)
    assert n == 0
    # Malformed ZIPs stay put so the user can inspect.
    assert bad.exists()


def test_non_whatsapp_zip_is_skipped(tmp_path: Path) -> None:
    """A valid ZIP that isn't a WA export (no root .txt) is a no-op."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    zp = inbox / "random.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("foo.csv", "a,b\n1,2\n")
        zf.writestr("subdir/notes.txt", "nested txt does not count")
    sink = _PlanSink()
    n = w.sweep_once(inbox, dispatch=sink)
    assert n == 0
    assert zp.exists(), "non-WA ZIPs must be left in place"


def test_alternate_transcript_filename(tmp_path: Path) -> None:
    """Older iOS / Android exports use `WhatsApp Chat with Me.txt` not `_chat.txt`."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _make_export_zip(
        inbox / "older.zip",
        transcript_filename="WhatsApp Chat with Me.txt",
    )
    sink = _PlanSink()
    n = w.sweep_once(inbox, dispatch=sink)
    assert n > 0, "watcher must find any *.txt at the ZIP root, not just `_chat.txt`"


def test_media_referenced_but_missing_from_zip_is_skipped(tmp_path: Path) -> None:
    """If the user exported without media, the line still parses but we
    can't materialise the file — drop the envelope rather than dispatch a
    media envelope with a nonexistent path."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _make_export_zip(
        inbox / "no-media.zip",
        transcript=(
            "‎[15/01/2026, 09:02:00 AM] Dhiraj: ‎<attached: IMG-MISSING-WA0001.jpg>\n"
        ),
        media_files={},  # no media in the ZIP
    )
    sink = _PlanSink()
    n = w.sweep_once(inbox, dispatch=sink)
    assert n == 0


def test_zip_slip_rejected(tmp_path: Path) -> None:
    """Zip-slip member must abort extraction, dispatch nothing, leave file."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    zp = inbox / "evil.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("_chat.txt", "15/01/2026, 10:00 - Dhiraj: hi")
        zf.writestr("../escaped.txt", "nope")

    sink = _PlanSink()
    n = w.sweep_once(inbox, dispatch=sink)
    assert n == 0
    # Path-traversal sibling is unprocessable → file stays for inspection.
    assert zp.exists()


def test_symlink_member_rejected(tmp_path: Path) -> None:
    zp = tmp_path / "symlink.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        info = zipfile.ZipInfo("link.txt")
        info.external_attr = 0o120777 << 16  # symlink mode
        zf.writestr(info, "/etc/passwd")
    with zipfile.ZipFile(zp) as zf:
        with pytest.raises(RuntimeError, match="symlink"):
            w._safe_extract(zf, tmp_path / "out")


class _FakeSizeZip:
    """Mirrors the LinkedIn test fake — overrides `file_size` to simulate
    zip-bomb headers without writing huge files."""

    def __init__(self, zf: zipfile.ZipFile, overrides: dict[str, int]) -> None:
        self._zf = zf
        self._overrides = overrides

    def infolist(self) -> list[zipfile.ZipInfo]:
        out: list[zipfile.ZipInfo] = []
        for info in self._zf.infolist():
            if info.filename in self._overrides:
                info.file_size = self._overrides[info.filename]
            out.append(info)
        return out

    def extractall(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("extractall must not run once caps trip")


def test_oversized_member_rejected(tmp_path: Path) -> None:
    """Per-member 200 MB cap."""
    zp = tmp_path / "bomb.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("huge.bin", b"x")
    with zipfile.ZipFile(zp) as zf:
        fake = _FakeSizeZip(zf, {"huge.bin": 300 * 1024 * 1024})
        with pytest.raises(RuntimeError, match="per-file size cap"):
            w._safe_extract(fake, tmp_path / "out")  # type: ignore[arg-type]


def test_oversized_total_rejected(tmp_path: Path) -> None:
    """Total 2 GB cap across all members."""
    zp = tmp_path / "bomb_total.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        for i in range(25):
            zf.writestr(f"chunk_{i}.bin", b"x")
    overrides = {f"chunk_{i}.bin": 100 * 1024 * 1024 for i in range(25)}  # 2.5 GB total
    with zipfile.ZipFile(zp) as zf:
        fake = _FakeSizeZip(zf, overrides)
        with pytest.raises(RuntimeError, match="total uncompressed size cap"):
            w._safe_extract(fake, tmp_path / "out")  # type: ignore[arg-type]


def test_us_locale_transcript_parses(tmp_path: Path) -> None:
    """US-formatted MM/DD/YYYY export must still parse end-to-end."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _make_export_zip(
        inbox / "us.zip",
        transcript="[01/13/2026, 09:00 AM] Dhiraj: https://example.com/us",
        media_files={},
    )
    sink = _PlanSink()
    n = w.sweep_once(inbox, dispatch=sink)
    assert n == 1
    assert sink.plans[0].message_type == "url"


def test_synthetic_message_id_is_deterministic() -> None:
    """The id depends only on (sender, captured_at, body|filename) — same
    inputs must always produce the same id."""
    from datetime import datetime, timezone

    from connecting_dots.parsers.whatsapp_export import ParsedMessage

    msg = ParsedMessage(
        sender="Dhiraj",
        captured_at=datetime(2026, 1, 15, 4, 53, 45, tzinfo=timezone.utc),
        body="hello",
        message_type="text",
    )
    id1 = w._synthetic_message_id(msg)
    id2 = w._synthetic_message_id(msg)
    assert id1 == id2
    assert id1.startswith("whatsapp_export:")
    assert len(id1) == len("whatsapp_export:") + 16


# --------------------------------------------------------------------------- #
# Integration: real dispatcher, tmp dedupe DB + tmp vault
# --------------------------------------------------------------------------- #


def test_end_to_end_real_dispatcher_dedupes_reimports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-import through the real `_default_dispatch` produces zero
    additional vault writes — the synthetic message_ids land in the shared
    SQLite dedupe table on first import and short-circuit on the second.
    """
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    # Isolate the dedupe DB so we don't poison the project's data/dedupe.db.
    dedupe_path = tmp_path / "dedupe.db"
    monkeypatch.setenv("DEDUPE_DB_PATH", str(dedupe_path))
    # Reload dispatcher to pick up the env var.
    import importlib
    import connecting_dots.dispatcher as dispatcher_mod
    importlib.reload(dispatcher_mod)
    # The watcher imported `_open_dedupe` / `_claim_message_id` /
    # `dispatch_url` / `dispatch_envelope` from `connecting_dots.dispatcher`
    # at module-load time, so rebinding to the reloaded module is needed.
    monkeypatch.setattr(w, "_open_dedupe", dispatcher_mod._open_dedupe)
    monkeypatch.setattr(w, "_claim_message_id", dispatcher_mod._claim_message_id)
    monkeypatch.setattr(w, "dispatch_url", dispatcher_mod.dispatch_url)
    monkeypatch.setattr(w, "dispatch_envelope", dispatcher_mod.dispatch_envelope)

    # Stub the vault writer — we just want to count writes.
    writes: list[dict[str, Any]] = []

    def _fake_write_note(**kwargs: Any) -> None:
        writes.append(kwargs)

    import lib.vault_writer
    monkeypatch.setattr(lib.vault_writer, "write_note", _fake_write_note)

    # And stub each per-domain handler's network so the URL message
    # doesn't actually fetch youtube — let the web fallback / handler
    # produce whatever record it wants; we only count writes.
    # Easier: short-circuit the handler list to RawHandler only.
    from connecting_dots.handlers.raw import handler as raw_handler

    class _AlwaysURLHandler:
        name = "url-stub"

        def matches(self, url: str) -> bool:  # noqa: ARG002
            return True

        def handle(self, envelope):  # noqa: ANN001
            return raw_handler.handle(envelope)

    dispatcher_mod.set_handlers([_AlwaysURLHandler()])

    _make_export_zip(inbox / "first.zip")
    w.sweep_once(inbox)
    first_count = len(writes)
    assert first_count > 0, "first import must produce vault writes"

    _make_export_zip(inbox / "second.zip")
    w.sweep_once(inbox)
    second_count = len(writes) - first_count
    assert second_count == 0, "re-import must produce zero additional vault writes"

    dispatcher_mod.reset_handlers()
