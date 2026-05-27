"""Atomic vault note writer with stable frontmatter serialization.

Day-1 component #10. Owns:
- Slug derivation from title/url with Unicode-aware normalization.
- Frontmatter YAML serialization (stable key order for diff hygiene).
- Handler-based vault routing (content-type taxonomy, not ingest channel).
- Atomic write via tempfile + os.rename + parent fsync (POSIX).
- TOCTOU-safe collision resolution via O_CREAT | O_EXCL.
- Post-write embedding hook (component #9 — stub).

Out of scope (later components):
- NER entity extraction (#8) — caller passes already-extracted entities or [].
- Edge / wikilink computation (#11).
"""
from __future__ import annotations

import errno
import hashlib
import os
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

VAULT_ROOT = Path(__file__).resolve().parent.parent.parent / "vault"

# Stable frontmatter key order. Anything not in this list is appended alphabetically.
_FRONTMATTER_ORDER = (
    "source",
    "handler",
    "captured_at",
    "url",
    "title",
    "entities",
    "topics",
    "labels",
    "raw_meta",
)

# Recognized content-type handlers that route to dedicated source dirs.
_HANDLER_DIRS = {
    "youtube": "sources/youtube",
    "instagram": "sources/instagram",
    "linkedin": "sources/linkedin",
    "web": "sources/web",
}

_FAILED_DIR = "inbox/_failed"
_INBOX_DIR = "inbox"

_MAX_COLLISION_SUFFIX = 999
_MAX_SLUG_LEN = 80


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
def _route_subdir(*, source: str, handler: str) -> str:
    """Return the vault subdirectory (relative to vault root) for a note.

    Routing is driven by `handler` (content type), not `source` (ingest channel)
    so that a YouTube link arriving via WhatsApp or mailto both land in
    `sources/youtube/`.
    """
    h = (handler or "").lower()
    if h in _HANDLER_DIRS:
        return _HANDLER_DIRS[h]
    if h == "failed":
        return _FAILED_DIR
    return _INBOX_DIR


# --------------------------------------------------------------------------- #
# Slug derivation — Unicode-aware
# --------------------------------------------------------------------------- #
def _slugify(title: str, url: str = "", max_len: int = _MAX_SLUG_LEN) -> str:
    """Normalize a title into a filesystem-safe slug.

    Accepts non-ASCII scripts (CJK, Arabic, Cyrillic, etc.) by keeping any
    Unicode `L` (letter) or `N` (number) category char, plus spaces and dashes.
    Collapses runs of whitespace/dashes. Falls back to a hash-of-url-derived
    slug when the result is empty, so distinct empty-title items don't collide.
    """
    if title is None:
        title = ""
    # NFKC keeps CJK as single chars; NFKD splits accents. We want NFKC so
    # CJK characters survive intact instead of being decomposed into ideographs.
    normalized = unicodedata.normalize("NFKC", title).strip()

    kept_chars: list[str] = []
    for ch in normalized:
        if ch in ("-", " ", "_"):
            kept_chars.append(" ")
            continue
        cat = unicodedata.category(ch)
        # L* = letters (any script). N* = numbers.
        if cat.startswith("L") or cat.startswith("N"):
            kept_chars.append(ch)
        # Drop everything else (punctuation, symbols, emoji, control).

    slug = "".join(kept_chars).strip()
    # Collapse internal whitespace runs to a single dash.
    parts = slug.split()
    slug = "-".join(parts).lower()
    slug = slug[:max_len].strip("-")

    if slug:
        return slug

    # Empty-title fallback: hash the URL so two distinct empty-title notes
    # don't collide into the same file.
    seed = (url or title or "").encode("utf-8", errors="ignore")
    digest = hashlib.sha256(seed).hexdigest()[:8]
    return f"note-{digest}"


# --------------------------------------------------------------------------- #
# Collision-safe path resolution (TOCTOU-free via O_CREAT|O_EXCL)
# --------------------------------------------------------------------------- #
def _create_exclusive(target_dir: Path, slug: str) -> tuple[Path, int]:
    """Atomically reserve a path by creating an empty file with O_EXCL.

    Returns (path, fd). Caller must close the fd. Suffix-bumps `-2`, `-3`, ...
    up to `_MAX_COLLISION_SUFFIX` on `FileExistsError`. This is the only
    way to win a race between multiple writers / threads / processes
    attempting the same slug.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    mode = 0o644

    candidates = [f"{slug}.md"] + [
        f"{slug}-{n}.md" for n in range(2, _MAX_COLLISION_SUFFIX + 1)
    ]
    for name in candidates:
        candidate = target_dir / name
        try:
            fd = os.open(str(candidate), flags, mode)
            return candidate, fd
        except FileExistsError:
            continue
        except OSError as e:
            # EEXIST on some platforms surfaces as plain OSError.
            if e.errno == errno.EEXIST:
                continue
            raise
    raise RuntimeError(
        f"Slug collision overflow after {_MAX_COLLISION_SUFFIX} attempts "
        f"at {target_dir / slug}.md"
    )


# --------------------------------------------------------------------------- #
# Frontmatter
# --------------------------------------------------------------------------- #
def _ordered_frontmatter(meta: dict[str, Any]) -> dict[str, Any]:
    ordered: dict[str, Any] = {}
    for k in _FRONTMATTER_ORDER:
        if k in meta:
            ordered[k] = meta[k]
    for k in sorted(meta):
        if k not in ordered:
            ordered[k] = meta[k]
    return ordered


def _serialize(meta: dict[str, Any], body: str) -> str:
    fm = yaml.safe_dump(
        _ordered_frontmatter(meta),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).rstrip()
    body = (body or "").rstrip() + "\n"
    return f"---\n{fm}\n---\n\n{body}"


def _normalize_captured_at(value: datetime | str | None) -> str:
    if value is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(value, str):
        return value  # trust caller-provided ISO string
    else:
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Atomic write with parent fsync
# --------------------------------------------------------------------------- #
def _fsync_dir(path: Path) -> None:
    """Best-effort directory fsync for durability after rename. POSIX-only."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except (OSError, AttributeError):
        # Windows / unsupported FS — silently skip. Data is still on the file
        # itself via the write-side fsync below.
        pass


def _atomic_write_into_reservation(
    final_path: Path, reservation_fd: int, content: str
) -> None:
    """Write `content` durably to `final_path`.

    `reservation_fd` is the O_EXCL-created empty file at `final_path`. We
    write the real bytes via a sibling tmpfile + os.rename so a crash mid-write
    leaves either (a) the empty reservation file (caller can detect & clean up
    at next boot if desired) or (b) the fully written final, never a partial.
    """
    # Drop the reservation fd — we only used it to win the race. The empty
    # placeholder file remains; the rename below overwrites it atomically.
    try:
        os.close(reservation_fd)
    except OSError:
        pass

    parent = final_path.parent
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".md", dir=str(parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.rename(tmp, str(final_path))
    except Exception:
        # Best-effort cleanup of both tmp and the empty reservation.
        for p in (tmp, str(final_path)):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
        raise

    _fsync_dir(parent)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def write_note(
    *,
    source: str,
    handler: str,
    url: str,
    title: str,
    text: str,
    captured_at: datetime,
    entities: list[str] | None = None,
    topics: list[str] | None = None,
    raw_meta: dict | None = None,
) -> Path:
    """Write a single canonical note to the vault.

    Routing is driven by `handler` (content-type), not `source` (ingest channel).
    Frontmatter records both so Obsidian queries can filter by either axis.

    Returns the absolute `Path` to the written note.

    Raises:
        RuntimeError: if more than 999 slug collisions accumulate.
    """
    subdir = _route_subdir(source=source, handler=handler)
    target_dir = _resolve_vault_root() / subdir

    slug = _slugify(title or "", url=url)
    final_path, fd = _create_exclusive(target_dir, slug)
    fd_closed = False

    try:
        meta: dict[str, Any] = {
            "source": source,
            "handler": handler,
            "captured_at": _normalize_captured_at(captured_at),
            "url": url or "",
            "title": title or "",
            "entities": list(entities or []),
            "topics": list(topics or []),
            "labels": [],
        }
        if raw_meta:
            meta["raw_meta"] = raw_meta

        body = _compose_body(title, text)
        serialized = _serialize(meta, body)
        _atomic_write_into_reservation(final_path, fd, serialized)
        fd_closed = True  # ownership transferred to the helper

        rel = final_path.relative_to(_resolve_vault_root()).as_posix()
        _embed_on_write_stub(rel, body, meta)
        return final_path
    except BaseException:
        # Clean up the empty reservation + any tmp sibling so a crash mid-write
        # never leaves an empty .md or .tmp- file behind.
        if not fd_closed:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(str(final_path))
        except FileNotFoundError:
            pass
        raise


def _compose_body(title: str, text: str) -> str:
    """Prepend an H1 only if the body doesn't already start with one."""
    t = (title or "").strip()
    body = (text or "").lstrip()
    if not t:
        return body
    if body.startswith("# "):
        return body
    return f"# {t}\n\n{body}" if body else f"# {t}\n"


def stable_id(relative_path: str) -> str:
    """Stable LanceDB row id derived from the vault-relative path."""
    return hashlib.sha1(relative_path.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Vault root indirection (allows env override + per-test isolation)
# --------------------------------------------------------------------------- #
def _resolve_vault_root() -> Path:
    """Return the active vault root.

    Honors `CONNECTING_DOTS_VAULT_ROOT` env var so tests (and per-user
    deployments) can redirect without monkeypatching module state.
    """
    env = os.environ.get("CONNECTING_DOTS_VAULT_ROOT")
    if env:
        return Path(env)
    return VAULT_ROOT


# --------------------------------------------------------------------------- #
# Embedding-on-write — STUB (component #9)
# --------------------------------------------------------------------------- #
def _embed_on_write_stub(
    relative_path: str, text: str, meta: dict[str, Any]
) -> None:
    """TODO(component #9): embed `text`, upsert into LanceDB `items` table.

    Intentionally a no-op on Day 1 so the writer is unit-testable without the
    embedding model on disk.
    """
    return None
