"""Repair stuck NER enrichment markers.

A bug in earlier versions of `workers/ner_backfill.py` stamped
`raw_meta.ner_enriched_at` even when the extraction errored. Combined with
the worker's skip-if-enriched check, that permanently locked failed notes
out of retry.

This module finds notes that look stuck — enriched-marker set but both
`entities` and `topics` empty — and clears the marker so the next backfill
sweep will retry them.
"""
from __future__ import annotations

import argparse
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


def _split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str]:
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text
    raw_fm = text[4:end]
    body = text[end + 5 :]
    try:
        fm = yaml.safe_load(raw_fm) or {}
    except yaml.YAMLError:
        return None, body
    if not isinstance(fm, dict):
        return None, body
    return fm, body


# Stable frontmatter key order — must match `workers/ner_backfill.py` and
# `lib/vault_writer/writer.py`.
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


def _ordered(meta: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in _FRONTMATTER_ORDER:
        if k in meta:
            out[k] = meta[k]
    for k in sorted(meta):
        if k not in out:
            out[k] = meta[k]
    return out


def _write_atomic(path: Path, fm: dict[str, Any], body: str) -> None:
    serialized_fm = yaml.safe_dump(
        _ordered(fm),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).rstrip()
    content = f"---\n{serialized_fm}\n---\n{body}"
    if not content.endswith("\n"):
        content += "\n"
    parent = path.parent
    fd, tmp = tempfile.mkstemp(prefix=".tmp-repair-", suffix=".md", dir=str(parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.rename(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def clear_failed_enrichment_markers(vault_root: Path) -> int:
    """Walk `vault_root` and unstick notes whose enrichment markers are set
    but whose `entities` and `topics` are both empty.

    Returns the number of notes whose markers were cleared.
    """
    if not vault_root.exists():
        return 0

    cleared = 0
    for path in sorted(vault_root.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue

        fm, body = _split_frontmatter(text)
        if fm is None:
            continue

        raw_meta = fm.get("raw_meta") or {}
        if not isinstance(raw_meta, dict):
            continue
        if not raw_meta.get("ner_enriched_at"):
            continue

        entities = fm.get("entities") or []
        topics = fm.get("topics") or []
        if (isinstance(entities, list) and entities) or (
            isinstance(topics, list) and topics
        ):
            continue

        new_raw_meta = {
            k: v for k, v in raw_meta.items() if k not in ("ner_enriched_at", "ner_model")
        }
        new_fm = dict(fm)
        if new_raw_meta:
            new_fm["raw_meta"] = new_raw_meta
        else:
            new_fm.pop("raw_meta", None)

        try:
            _write_atomic(path, new_fm, body)
        except OSError:
            log.exception("Failed to rewrite %s", path)
            continue
        cleared += 1

    return cleared


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="connecting_dots.enrichment.repair",
        description="Clear ner_enriched_at markers from notes whose entities + topics are empty.",
    )
    parser.add_argument(
        "--vault",
        default="vault",
        help="Path to vault root (default: ./vault).",
    )
    args = parser.parse_args(argv)

    count = clear_failed_enrichment_markers(Path(args.vault))
    print(f"{count} unstuck")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
