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
from connecting_dots.generated.inbound_envelope import InboundEnvelope
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
