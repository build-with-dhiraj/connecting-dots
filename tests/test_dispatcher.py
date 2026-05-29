"""Dispatcher routing, dedupe, and error-degradation tests.

These tests deliberately avoid importing real handler modules — sibling
agents are still writing them. We install mock handlers via
`dispatcher.set_handlers()` and stub `lib.vault_writer.write_note` so the
tests don't need PyYAML or filesystem state beyond the SQLite dedupe table.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from connecting_dots import dispatcher
from connecting_dots.inbound_envelope import InboundEnvelope
from connecting_dots.types import NoteRecord


# --------------------------------------------------------------------------- #
# Mock handlers
# --------------------------------------------------------------------------- #
@dataclass
class MockHandler:
    """Concrete handler satisfying the Protocol — used to assert routing."""

    name: str
    domains: tuple[str, ...] = ()
    raises: Exception | None = None
    calls: list[InboundEnvelope] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []

    def matches(self, url: str) -> bool:
        return any(d in url for d in self.domains)

    def handle(self, envelope: InboundEnvelope) -> NoteRecord:
        self.calls.append(envelope)
        if self.raises:
            raise self.raises
        return NoteRecord(
            source=envelope.source.value,
            handler=self.name,
            url=str(envelope.url),
            title=f"{self.name}: {envelope.url}",
            text=f"body produced by {self.name}",
            captured_at=envelope.captured_at,
            raw_meta={"mock": True},
        )


class _AlwaysMatchHandler(MockHandler):
    def matches(self, url: str) -> bool:  # noqa: ARG002
        return True


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def captured_writes(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace `lib.vault_writer.write_note` with an in-memory recorder."""
    writes: list[dict[str, Any]] = []

    def fake_write_note(**kwargs: Any) -> Any:  # noqa: ANN401
        writes.append(kwargs)

        # Minimal shape mirroring VaultWriteResult so callers that inspect the
        # result don't choke. The dispatcher currently ignores the return value.
        class _R:
            vault_path = Path("/tmp/fake.md")
            relative_path = "fake.md"
            slug = "fake"
            created = True

        return _R()

    # Patch the symbol where the dispatcher imports it from.
    monkeypatch.setattr("lib.vault_writer.write_note", fake_write_note, raising=True)
    return writes


@pytest.fixture
def isolated_dedupe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the dispatcher at a temp SQLite dedupe DB."""
    db = tmp_path / "dedupe.db"
    monkeypatch.setattr(dispatcher, "_DEDUPE_DB_PATH", db, raising=True)
    return db


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    """Make sure cross-test handler state doesn't leak."""
    dispatcher.reset_handlers()
    yield
    dispatcher.reset_handlers()


def _now() -> datetime:
    return datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_youtube_url_routes_to_youtube_handler(
    captured_writes: list[dict[str, Any]],
    isolated_dedupe: Path,
) -> None:
    yt = MockHandler(name="youtube", domains=("youtube.com", "youtu.be"))
    web = _AlwaysMatchHandler(name="web")
    dispatcher.set_handlers([yt, web])

    record = dispatcher.dispatch_url(
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        source="whatsapp",
        captured_at=_now(),
        raw_payload={"src": "wa"},
        message_id="wamid.test.youtube",
    )

    assert record is not None
    assert record.handler == "youtube"
    assert len(yt.calls) == 1
    assert len(web.calls) == 0
    assert len(captured_writes) == 1
    assert captured_writes[0]["source"] == "whatsapp"
    assert "youtube" in captured_writes[0]["text"]  # mock body mentions handler name


def test_arbitrary_url_falls_through_to_web_handler(
    captured_writes: list[dict[str, Any]],
    isolated_dedupe: Path,
) -> None:
    yt = MockHandler(name="youtube", domains=("youtube.com",))
    ig = MockHandler(name="instagram", domains=("instagram.com",))
    web = _AlwaysMatchHandler(name="web")
    dispatcher.set_handlers([yt, ig, web])

    record = dispatcher.dispatch_url(
        url="https://some-random-blog.example/post/42",
        source="mailto",
        captured_at=_now(),
        raw_payload={},
        message_id="mailto:abc",
    )

    assert record is not None
    assert record.handler == "web"
    assert len(yt.calls) == 0
    assert len(ig.calls) == 0
    assert len(web.calls) == 1


def test_duplicate_message_id_is_noop(
    captured_writes: list[dict[str, Any]],
    isolated_dedupe: Path,
) -> None:
    web = _AlwaysMatchHandler(name="web")
    dispatcher.set_handlers([web])

    first = dispatcher.dispatch_url(
        url="https://example.com/dup",
        source="mailto",
        captured_at=_now(),
        raw_payload={},
        message_id="dup-key-1",
    )
    second = dispatcher.dispatch_url(
        url="https://example.com/dup",
        source="mailto",
        captured_at=_now(),
        raw_payload={},
        message_id="dup-key-1",
    )

    assert first is not None and first.handler == "web"
    assert second is None  # dedupe hit
    assert len(web.calls) == 1
    assert len(captured_writes) == 1


def test_handler_exception_produces_degraded_record(
    captured_writes: list[dict[str, Any]],
    isolated_dedupe: Path,
) -> None:
    broken = _AlwaysMatchHandler(name="web", raises=RuntimeError("upstream 500"))
    dispatcher.set_handlers([broken])

    record = dispatcher.dispatch_url(
        url="https://will-fail.example/x",
        source="whatsapp",
        captured_at=_now(),
        raw_payload={"trace": True},
        message_id="wamid.failure",
    )

    assert record is not None
    assert record.handler == "failed"
    assert record.text == ""
    assert record.title == "https://will-fail.example/x"
    assert "upstream 500" in record.raw_meta["error"]
    # Even on failure, we still wrote a placeholder note to the vault.
    assert len(captured_writes) == 1
    assert captured_writes[0]["url"] == "https://will-fail.example/x"


# --------------------------------------------------------------------------- #
# dispatch_envelope — routes by message_type
# --------------------------------------------------------------------------- #
def _envelope(
    *,
    message_type: str,
    message_id: str = "wamid.env-test",
    url: str | None = None,
    text: str | None = None,
    media_id: str | None = None,
    media_mime_type: str | None = None,
    media_filename: str | None = None,
) -> InboundEnvelope:
    data: dict[str, Any] = {
        "message_id": message_id,
        "message_type": message_type,
        "source": "whatsapp",
        "captured_at": _now().isoformat(),
        "raw_payload": {"meta": True},
    }
    if url is not None:
        data["url"] = url
    if text is not None:
        data["text"] = text
    if media_id is not None:
        data["media_id"] = media_id
    if media_mime_type is not None:
        data["media_mime_type"] = media_mime_type
    if media_filename is not None:
        data["media_filename"] = media_filename
    return InboundEnvelope.model_validate(data)


def test_dispatch_envelope_routes_url(
    captured_writes: list[dict[str, Any]],
    isolated_dedupe: Path,
) -> None:
    """A URL envelope must traverse the existing per-domain handler routing."""
    yt = MockHandler(name="youtube", domains=("youtube.com", "youtu.be"))
    web = _AlwaysMatchHandler(name="web")
    dispatcher.set_handlers([yt, web])

    env = _envelope(
        message_type="url",
        message_id="env-url-1",
        url="https://www.youtube.com/watch?v=abc",
    )
    record = dispatcher.dispatch_envelope(env)

    assert record is not None
    assert record.handler == "youtube"
    assert len(yt.calls) == 1
    assert len(web.calls) == 0
    assert len(captured_writes) == 1


def test_dispatch_envelope_routes_text(
    captured_writes: list[dict[str, Any]],
    isolated_dedupe: Path,
) -> None:
    """A text envelope (no URL) must land in the RawHandler — not a URL handler."""
    yt = MockHandler(name="youtube", domains=("youtube.com",))
    web = _AlwaysMatchHandler(name="web")
    dispatcher.set_handlers([yt, web])

    env = _envelope(
        message_type="text",
        message_id="env-text-1",
        text="just a plain note saved to my second brain",
    )
    record = dispatcher.dispatch_envelope(env)

    assert record is not None
    assert record.handler == "raw"
    assert record.text == "just a plain note saved to my second brain"
    # Neither URL handler must have been called — non-URL envelopes bypass
    # per-domain matching entirely.
    assert len(yt.calls) == 0
    assert len(web.calls) == 0
    assert len(captured_writes) == 1
    assert captured_writes[0]["handler"] == "raw"
    assert captured_writes[0]["raw_meta"]["message_type"] == "text"


def test_dispatch_envelope_routes_image(
    captured_writes: list[dict[str, Any]],
    isolated_dedupe: Path,
) -> None:
    """An image envelope carries media_id; RawHandler stashes it in raw_meta
    so component #5 can download from Meta's media endpoint later."""
    dispatcher.set_handlers([_AlwaysMatchHandler(name="web")])

    env = _envelope(
        message_type="image",
        message_id="env-image-1",
        media_id="meta-media-id-xyz",
        media_mime_type="image/jpeg",
        text="caption: my receipt",
    )
    record = dispatcher.dispatch_envelope(env)

    assert record is not None
    assert record.handler == "raw"
    assert record.raw_meta["message_type"] == "image"
    assert record.raw_meta["media_id"] == "meta-media-id-xyz"
    assert record.raw_meta["media_mime_type"] == "image/jpeg"
    assert record.raw_meta["pending_enrichment"] is True
    assert record.text == "caption: my receipt"
    assert len(captured_writes) == 1


def test_dispatch_envelope_routes_document_with_filename(
    captured_writes: list[dict[str, Any]],
    isolated_dedupe: Path,
) -> None:
    dispatcher.set_handlers([_AlwaysMatchHandler(name="web")])

    env = _envelope(
        message_type="document",
        message_id="env-doc-1",
        media_id="meta-doc-id",
        media_mime_type="application/pdf",
        media_filename="receipt-2026-05.pdf",
    )
    record = dispatcher.dispatch_envelope(env)

    assert record is not None
    assert record.handler == "raw"
    assert record.raw_meta["media_filename"] == "receipt-2026-05.pdf"
    # No body text -> title derived from "WhatsApp <type>".
    assert record.title == "WhatsApp document"


# --------------------------------------------------------------------------- #
# dispatch_envelope — interactive / digest reaction handling
# --------------------------------------------------------------------------- #

def _interactive_envelope(row_id: str, itype: str = "list_reply", from_num: str = "918595087697") -> InboundEnvelope:
    """Build an interactive envelope with the WA native raw_payload structure."""
    if itype == "list_reply":
        interactive = {"type": "list_reply", "list_reply": {"id": row_id, "title": "👍"}}
    else:
        interactive = {"type": "button_reply", "button_reply": {"id": row_id, "title": "Yes"}}
    return InboundEnvelope.model_validate({
        "message_id": "wamid.interactive-test",
        "message_type": "interactive",
        "source": "whatsapp",
        "captured_at": _now().isoformat(),
        "raw_payload": {
            "from": from_num,
            "type": "interactive",
            "interactive": interactive,
        },
    })


def test_dispatch_envelope_interactive_digest_reaction_writes_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid digest row ID must call write_label with the correct slug/reaction/user."""
    labels_file = tmp_path / "labels.jsonl"
    monkeypatch.setenv("LABELS_DB", str(labels_file))

    env = _interactive_envelope("sources/web/note.md__up")
    result = dispatcher.dispatch_envelope(env)

    assert result is None  # no NoteRecord for label writes
    assert labels_file.exists()
    import json
    rows = [json.loads(line) for line in labels_file.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["item_slug"] == "sources/web/note.md"
    assert rows[0]["reaction"] == "thumbs_up"
    assert rows[0]["user"] == "918595087697"


def test_dispatch_envelope_interactive_invalid_row_id_falls_through_to_raw(
    captured_writes: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interactive envelope with a non-digest row ID must fall through to the raw handler."""
    env = _interactive_envelope("some-regular-button-no-double-underscore", itype="button_reply")
    result = dispatcher.dispatch_envelope(env)

    # raw handler should have been called and written a record
    assert result is not None
    assert result.handler == "raw"


def test_validation_failure_does_not_claim_dedupe_id(
    captured_writes: list[dict[str, Any]],
    isolated_dedupe: Path,
) -> None:
    """Bug 1 regression: a ValidationError from _build_envelope must NOT claim
    the message_id in the dedupe table.  A subsequent valid dispatch of the
    SAME message_id must succeed and write a note — previously it was silently
    dropped because the id was already marked 'seen'."""
    import sqlite3

    web = _AlwaysMatchHandler(name="web")
    dispatcher.set_handlers([web])

    mid = "regression-bug1-dedupe"

    # First call: pass an invalid source so _build_envelope raises ValidationError.
    # (source="__invalid__" is not in the enum so Pydantic will reject it.)
    try:
        dispatcher.dispatch_url(
            url="https://example.com/will-fail-validation",
            source="__invalid_source__",  # type: ignore[arg-type]
            captured_at=_now(),
            raw_payload={},
            message_id=mid,
        )
    except Exception:  # noqa: BLE001 — we expect a ValidationError to surface
        pass

    # The dedupe DB must NOT contain our message_id after the failed call.
    # If the DB file doesn't even exist yet, the id was definitely not claimed.
    if isolated_dedupe.exists():
        conn = sqlite3.connect(str(isolated_dedupe))
        try:
            row = conn.execute(
                "SELECT message_id FROM seen_message_ids WHERE message_id = ? "
                "/* table may not exist if dedupe was never opened */",
                (mid,),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None  # table doesn't exist — id was definitely not claimed
        finally:
            conn.close()
        assert row is None, (
            "message_id was claimed despite envelope validation failure — "
            "this is the bug that poisoned 297 YouTube ids"
        )

    # Second call: same message_id, now with a valid source — must succeed.
    record = dispatcher.dispatch_url(
        url="https://example.com/will-fail-validation",
        source="whatsapp",
        captured_at=_now(),
        raw_payload={},
        message_id=mid,
    )
    assert record is not None, "valid retry after validation failure must produce a record"
    assert len(captured_writes) == 1


def test_missing_handler_module_does_not_break_registry(
    monkeypatch: pytest.MonkeyPatch,
    captured_writes: list[dict[str, Any]],
    isolated_dedupe: Path,
) -> None:
    """Sibling-agent handlers may not have committed yet — registry must boot."""
    # Force a real importlib load against a known-bad module path.
    monkeypatch.setattr(
        dispatcher,
        "HANDLER_MODULES",
        [
            "connecting_dots.handlers.does_not_exist_yet",
            # We can't include real handlers without sibling agents — assert
            # the importer skips the missing one gracefully (returns empty).
        ],
        raising=True,
    )
    dispatcher.reset_handlers()
    handlers = dispatcher.get_handlers()
    assert handlers == []
