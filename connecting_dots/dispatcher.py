"""Central URL dispatcher (component #2).

Routes captured URLs to per-domain handlers (`connecting_dots.handlers.*`)
and persists the resulting `NoteRecord` to the Obsidian vault via
`lib.vault_writer.write_note()`.

Public surface used by every ingest channel (WhatsApp stream consumer,
mailto IMAP poller, future LinkedIn/manual):

    dispatch_url(url, source, captured_at, raw_payload, message_id=None)

Design choices:
- **Explicit registry over auto-discovery.** Handlers are listed by import
  path in `HANDLER_MODULES`. Lazy `importlib` imports keep the dispatcher
  bootable when sibling-agent handler modules haven't landed yet.
- **First match wins.** Order in `HANDLER_MODULES` is significant —
  specific handlers (youtube, instagram, linkedin) come before the
  catch-all `web` handler.
- **Idempotent.** A SQLite dedupe table at `data/dedupe.db` keys on
  `message_id`. The stream consumer also dedupes there; calling
  `dispatch_url` directly with a known id is a no-op.
- **Errors degrade, never drop.** A handler exception produces a
  `NoteRecord(handler="failed", text="", raw_meta={"error": ...})` so the
  user can see and fix the capture manually.

The legacy `_MockDispatcher` / `register_dispatcher()` indirection from the
Day-1 stub is gone — this module IS the dispatcher.
"""
from __future__ import annotations

import importlib
import logging
import os
import sqlite3
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from connecting_dots.generated.inbound_envelope import InboundEnvelope
from connecting_dots.handlers.base import Handler, HandlerNotFound
from connecting_dots.types import NoteRecord

logger = logging.getLogger(__name__)

SourceChannel = Literal["whatsapp", "mailto", "linkedin", "manual"]

# ---------------------------------------------------------------------------
# Handler registry — explicit, ordered, specific-before-generic.
#
# Each entry is a module path. The dispatcher imports the module lazily and
# resolves the handler object via `_resolve_handler_attr` (tries a small set
# of conventional names: `handler`, `{stem}_handler`, then a class named
# `{Stem}Handler` which is instantiated). Sibling agents picked different
# names for their singletons; this resolver tolerates all of them without
# forcing a coordination round-trip.
#
# Missing modules are logged once and skipped so the dispatcher boots even
# when a sibling handler hasn't landed yet.
#
# The catch-all `web` handler MUST be last.
# ---------------------------------------------------------------------------
HANDLER_MODULES: list[str] = [
    "connecting_dots.handlers.youtube",
    "connecting_dots.handlers.instagram",
    "connecting_dots.handlers.linkedin",
    "connecting_dots.handlers.web",  # fallback — must be last
]

_DEDUPE_DB_PATH = Path(os.environ.get("DEDUPE_DB_PATH", "data/dedupe.db"))

# Module-level caches — small and immutable enough that a single lock is fine.
_REGISTRY_LOCK = threading.Lock()
_REGISTRY_CACHE: list[Handler] | None = None
_MISSING_HANDLERS_LOGGED: set[str] = set()


# --------------------------------------------------------------------------- #
# Registry resolution
# --------------------------------------------------------------------------- #
def _resolve_handler_attr(mod: Any) -> Handler | None:
    """Pick the handler instance out of a handler module.

    Tries (in order):
      1. `mod.handler`                              — the canonical export
      2. `mod.{stem}_handler`  (e.g. `youtube_handler`) — sibling-agent convention
      3. `mod.{Stem}Handler()` (e.g. `YouTubeHandler`)   — class fallback (instantiated)

    Returns None if none of these yield a `Handler`-shaped object.
    """
    # 1. canonical name
    cand = getattr(mod, "handler", None)
    if cand is not None and isinstance(cand, Handler):
        return cand

    stem = mod.__name__.rsplit(".", 1)[-1]  # "connecting_dots.handlers.youtube" -> "youtube"

    # 2. `{stem}_handler`
    cand = getattr(mod, f"{stem}_handler", None)
    if cand is not None and isinstance(cand, Handler):
        return cand

    # 3. `{Stem}Handler` (special-cased for YouTube which is CamelCased oddly)
    class_candidates = [
        f"{stem.capitalize()}Handler",
        # YouTube uses YouTubeHandler — special case the embedded capital.
        "YouTubeHandler" if stem == "youtube" else None,
    ]
    for cls_name in filter(None, class_candidates):
        cls = getattr(mod, cls_name, None)
        if cls is not None and isinstance(cls, type):
            try:
                instance = cls()
            except Exception:  # noqa: BLE001
                logger.exception("[dispatch] could not instantiate %s.%s", mod.__name__, cls_name)
                continue
            if isinstance(instance, Handler):
                return instance
    return None


def _load_handlers() -> list[Handler]:
    """Resolve `HANDLER_MODULES` -> concrete handler instances.

    Missing modules (sibling agents not yet committed) are skipped with a
    one-time warning so the dispatcher stays bootable during parallel
    development.
    """
    handlers: list[Handler] = []
    for module_path in HANDLER_MODULES:
        try:
            mod = importlib.import_module(module_path)
        except ImportError as exc:
            if module_path not in _MISSING_HANDLERS_LOGGED:
                _MISSING_HANDLERS_LOGGED.add(module_path)
                logger.warning(
                    "[dispatch] handler module %s not importable yet (%s) — skipping",
                    module_path, exc,
                )
            continue
        handler = _resolve_handler_attr(mod)
        if handler is None:
            logger.error(
                "[dispatch] handler module %s did not expose a handler "
                "(tried `handler`, `{stem}_handler`, `{Stem}Handler()`) — skipping",
                module_path,
            )
            continue
        handlers.append(handler)
    return handlers


def get_handlers(*, refresh: bool = False) -> list[Handler]:
    """Return the cached handler list, optionally forcing a re-resolve.

    `refresh=True` is used by tests that monkeypatch `HANDLER_MODULES`.
    """
    global _REGISTRY_CACHE
    with _REGISTRY_LOCK:
        if refresh or _REGISTRY_CACHE is None:
            _REGISTRY_CACHE = _load_handlers()
        return list(_REGISTRY_CACHE)


def set_handlers(handlers: Iterable[Handler]) -> None:
    """Test hook: install a fixed handler list, bypassing import resolution."""
    global _REGISTRY_CACHE
    with _REGISTRY_LOCK:
        _REGISTRY_CACHE = list(handlers)


def reset_handlers() -> None:
    """Test hook: clear the cache so the next call re-resolves from disk."""
    global _REGISTRY_CACHE
    with _REGISTRY_LOCK:
        _REGISTRY_CACHE = None


# --------------------------------------------------------------------------- #
# Dedupe — shares the SQLite table with workers.stream_consumer
# --------------------------------------------------------------------------- #
def _open_dedupe(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or _DEDUPE_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)  # autocommit
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_message_ids (
            message_id TEXT PRIMARY KEY,
            seen_at TEXT NOT NULL
        )
        """
    )
    return conn


def _claim_message_id(conn: sqlite3.Connection, message_id: str) -> bool:
    """Atomically mark `message_id` as seen. Returns True on first claim."""
    try:
        conn.execute(
            "INSERT INTO seen_message_ids (message_id, seen_at) VALUES (?, ?)",
            (message_id, datetime.now(timezone.utc).isoformat()),
        )
        return True
    except sqlite3.IntegrityError:
        return False


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
def _pick_handler(url: str, handlers: list[Handler]) -> Handler:
    """First-match-wins routing. Raises `HandlerNotFound` if registry is empty
    AND no handler claims the URL — the `web` fallback normally prevents this."""
    for h in handlers:
        try:
            if h.matches(url):
                return h
        except Exception:  # noqa: BLE001 — a buggy matches() must not break routing
            logger.exception("[dispatch] handler %s.matches() raised — skipping", getattr(h, "name", "?"))
            continue
    raise HandlerNotFound(f"no handler matched url={url!r}")


def _degraded_record(
    *, url: str, source: str, captured_at: datetime, error: str, raw_payload: dict[str, Any],
) -> NoteRecord:
    """Build the placeholder record we write when a handler raises."""
    return NoteRecord(
        source=source,
        handler="failed",
        url=url,
        title=url,
        text="",
        captured_at=captured_at,
        entities=[],
        topics=[],
        raw_meta={"error": error, "raw_payload": raw_payload},
    )


def _write_record(record: NoteRecord) -> None:
    """Persist a `NoteRecord` via the vault writer.

    Imported lazily so a vault-writer import error doesn't kill module load
    (and so tests can monkeypatch the symbol on this module).
    """
    # local import: keeps the dispatcher importable when running unit tests that
    # don't have PyYAML installed yet, and lets tests monkeypatch easily.
    from lib.vault_writer import write_note

    write_note(
        source=record.source,
        text=_compose_body(record),
        url=record.url,
        entities=record.entities,
        topics=record.topics,
        captured_at=record.captured_at,
    )


def _compose_body(record: NoteRecord) -> str:
    """Prepend a Markdown H1 title so the vault writer's slug derivation picks
    up a stable filename, and so the rendered note has a heading."""
    title = (record.title or record.url).strip()
    body = record.text or ""
    return f"# {title}\n\n{body}" if body else f"# {title}\n"


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def dispatch_url(
    url: str,
    source: SourceChannel | str,
    captured_at: datetime,
    raw_payload: dict[str, Any] | None = None,
    message_id: str | None = None,
    *,
    dedupe_db: Path | None = None,
) -> NoteRecord | None:
    """Route a captured URL to its handler and persist the result.

    Returns the produced `NoteRecord` (or `None` on dedupe hit). The return
    value is mostly for tests — production callers fire-and-forget.
    """
    payload = raw_payload or {}

    # Idempotency: same message twice = no-op.
    if message_id:
        conn = _open_dedupe(dedupe_db)
        try:
            if not _claim_message_id(conn, message_id):
                logger.info("[dispatch] dedupe hit message_id=%s — no-op", message_id)
                return None
        finally:
            conn.close()

    envelope = _build_envelope(
        url=url, source=source, captured_at=captured_at,
        raw_payload=payload, message_id=message_id,
    )

    handlers = get_handlers()
    try:
        handler = _pick_handler(url, handlers)
    except HandlerNotFound:
        logger.error("[dispatch] no handler (incl. web fallback) for url=%s", url)
        record = _degraded_record(
            url=url, source=str(source), captured_at=captured_at,
            error="no handler matched", raw_payload=payload,
        )
        _write_record(record)
        return record

    try:
        record = handler.handle(envelope)
    except Exception as exc:  # noqa: BLE001 — handler failures degrade, never crash
        logger.exception("[dispatch] handler=%s raised on url=%s", handler.name, url)
        record = _degraded_record(
            url=url, source=str(source), captured_at=captured_at,
            error=f"{type(exc).__name__}: {exc}", raw_payload=payload,
        )

    try:
        _write_record(record)
    except Exception:  # noqa: BLE001
        logger.exception("[dispatch] vault write failed for url=%s — record=%s",
                         url, asdict(record))
        raise

    logger.info("[dispatch] handler=%s wrote note url=%s", record.handler, url)
    return record


# --------------------------------------------------------------------------- #
# Envelope construction
# --------------------------------------------------------------------------- #
def _build_envelope(
    *,
    url: str,
    source: SourceChannel | str,
    captured_at: datetime,
    raw_payload: dict[str, Any],
    message_id: str | None,
) -> InboundEnvelope:
    """Construct a validated `InboundEnvelope`. Pydantic enforces the schema
    contract before any handler runs."""
    # message_id is required on the envelope; synthesize a deterministic-ish
    # one for sources that didn't supply one (e.g. mailto with no Message-ID).
    mid = message_id or f"{source}:{int(captured_at.timestamp() * 1000)}:{hash(url) & 0xFFFFFFFF:x}"
    ts = captured_at if captured_at.tzinfo else captured_at.replace(tzinfo=timezone.utc)
    return InboundEnvelope.model_validate(
        {
            "message_id": mid,
            "url": url,
            "source": str(source),
            "captured_at": ts.isoformat(),
            "raw_payload": raw_payload,
        }
    )
