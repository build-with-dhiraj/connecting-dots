"""Atomic vault note writer with stable frontmatter serialization.

Day-1 component #10. Owns:
- Slug derivation from title/url with collision suffixing.
- Frontmatter YAML serialization (stable key order for diff hygiene).
- Atomic write via tempfile + os.replace.
- Post-write embedding hook (component #9 — stub).

Out of scope (later components):
- NER entity extraction (#8) — caller passes already-extracted entities or [].
- Edge / wikilink computation (#11).
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

VAULT_ROOT = Path(__file__).resolve().parent.parent.parent / "vault"

# Source -> subdir under vault/sources. Unknown sources land in inbox/.
_SOURCE_DIRS = {
    "whatsapp": "sources/whatsapp",
    "youtube": "sources/youtube",
    "instagram": "sources/instagram",
    "linkedin": "sources/linkedin",
}

# Stable frontmatter key order. Anything not in this list is appended alphabetically.
_FRONTMATTER_ORDER = ("source", "captured_at", "url", "entities", "topics", "labels")


@dataclass(frozen=True)
class VaultWriteResult:
    vault_path: Path           # absolute path on disk
    relative_path: str         # path relative to vault root (used as LanceDB key)
    slug: str
    created: bool              # False if we collided and suffix-bumped


def _slugify(text: str, max_len: int = 60) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[-\s]+", "-", text).strip("-")
    return (text or "untitled")[:max_len]


def _derive_slug(text: str, url: str | None) -> str:
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    # Strip a leading "# " if the caller already wrote a Markdown title.
    first_line = re.sub(r"^#+\s*", "", first_line)
    if first_line:
        return _slugify(first_line)
    if url:
        return _slugify(re.sub(r"^https?://", "", url).replace("/", "-"))
    return "untitled"


def _resolve_collision(target_dir: Path, slug: str) -> tuple[Path, bool]:
    """Return (final_path, created_fresh)."""
    candidate = target_dir / f"{slug}.md"
    if not candidate.exists():
        return candidate, True
    for n in range(2, 1000):
        candidate = target_dir / f"{slug}-{n}.md"
        if not candidate.exists():
            return candidate, False
    raise RuntimeError(f"Slug collision overflow at {target_dir / slug}")


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
    body = body.rstrip() + "\n"
    return f"---\n{fm}\n---\n\n{body}"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _normalize_captured_at(value: datetime | str | None) -> str:
    if value is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(value, str):
        return value  # trust caller-provided ISO string
    else:
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_note(
    *,
    source: str,
    text: str,
    url: str | None = None,
    entities: Iterable[dict[str, Any]] | None = None,
    topics: Iterable[str] | None = None,
    captured_at: datetime | str | None = None,
    vault_root: Path | None = None,
) -> VaultWriteResult:
    """Write a single canonical note to the vault.

    Returns a `VaultWriteResult` whose `relative_path` is the stable key used in
    LanceDB (`items.vault_path`).
    """
    root = vault_root or VAULT_ROOT
    subdir = _SOURCE_DIRS.get(source, "inbox")
    target_dir = root / subdir

    slug = _derive_slug(text, url)
    final_path, created = _resolve_collision(target_dir, slug)

    meta: dict[str, Any] = {
        "source": source,
        "captured_at": _normalize_captured_at(captured_at),
        "url": url or "",
        "entities": list(entities or []),
        "topics": list(topics or []),
        "labels": [],
    }
    _atomic_write(final_path, _serialize(meta, text))

    rel = final_path.relative_to(root).as_posix()
    _embed_on_write_stub(rel, text, meta)

    return VaultWriteResult(
        vault_path=final_path,
        relative_path=rel,
        slug=final_path.stem,
        created=created,
    )


def stable_id(relative_path: str) -> str:
    """Stable LanceDB row id derived from the vault-relative path."""
    return hashlib.sha1(relative_path.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Embedding-on-write — STUB (component #9)
# --------------------------------------------------------------------------- #
def _embed_on_write_stub(relative_path: str, text: str, meta: dict[str, Any]) -> None:
    """TODO(component #9): embed `text`, upsert into LanceDB `items` table.

    Planned wiring:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer("BAAI/bge-small-en-v1.5")
        vec = model.encode(text, normalize_embeddings=True)
        tbl = lancedb.connect("vault/.lancedb").open_table("items")
        tbl.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute([
            {"id": stable_id(relative_path), "vector": vec, "text": text,
             "source": meta["source"], "captured_at": meta["captured_at"],
             "url": meta["url"], "vault_path": relative_path}
        ])

    Intentionally a no-op on Day 1 so the writer is unit-testable without the
    embedding model on disk.
    """
    return None
