"""Vault README refresh worker.

Regenerates vault/README.md with live note counts and an LLM overview paragraph.

Usage: python -m workers.vault_readme_refresh [--no-llm] [--dry-run] [--model NAME]
"""
from __future__ import annotations

import argparse
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_DEFAULT_VAULT_ROOT = Path(__file__).resolve().parent.parent / "vault"


def _resolve_vault_root() -> Path:
    env = os.environ.get("CONNECTING_DOTS_VAULT_ROOT")
    if env:
        return Path(env)
    return _DEFAULT_VAULT_ROOT


def count_notes(vault_root: Path) -> dict[str, int]:
    """Count .md files per tracked folder. Returns dict with total + per-folder keys."""
    def _md_count(path: Path) -> int:
        if not path.exists():
            return 0
        return sum(1 for _ in path.rglob("*.md"))

    web = _md_count(vault_root / "sources" / "web")
    youtube = _md_count(vault_root / "sources" / "youtube")
    linkedin = _md_count(vault_root / "sources" / "linkedin")
    instagram = _md_count(vault_root / "sources" / "instagram")
    whatsapp = _md_count(vault_root / "sources" / "whatsapp")
    inbox = _md_count(vault_root / "inbox")
    themes = _md_count(vault_root / "themes")
    digests = _md_count(vault_root / "digests")

    total = web + youtube + linkedin + instagram + whatsapp + inbox + themes + digests
    return {
        "total": total,
        "inbox": inbox,
        "web": web,
        "youtube": youtube,
        "linkedin": linkedin,
        "instagram": instagram,
        "whatsapp": whatsapp,
        "themes": themes,
        "digests": digests,
    }


_README_TEMPLATE = """\
# Connecting Dots — Personal Knowledge Vault

> Last refreshed by `vault-readme` worker on {date}.

## What this is

{overview}

## Where to start

- **Browse by theme** → [[by-topic.base]] — table view of every note, filterable by topic
- **Best of finance** → [[financial-performance]], [[dividend-payout]], [[indian-stock-market]]
- **Best of product** → [[product-management]], [[design-systems]], [[hiring]]
- **Recent saves** → use Obsidian's "Files" panel sorted by "Modified time desc"

## Vault size at a glance

| Metric | Count |
|---|---|
| Total notes | {total} |
| Web articles | {web} |
| Inbox / unrouted | {inbox} |
| LinkedIn posts | {linkedin} |
| YouTube transcripts | {youtube} |
| Instagram saves | {instagram} |
| WhatsApp messages | {whatsapp} |
| MoC theme pages | {themes} |
| Digest pages | {digests} |

## Folder map

```
vault/
├── README.md                ← you are here — navigation hub
├── inbox/                   ← {inbox} notes awaiting routing (mixed sources)
├── sources/
│   ├── web/                 ← {web} notes (articles, blogs, X threads, GitHub, finance)
│   ├── linkedin/            ← {linkedin} notes (LinkedIn posts + ZIP export)
│   ├── youtube/             ← {youtube} notes (transcripts + video metadata)
│   ├── instagram/           ← {instagram} notes (OG meta; IG anon-blocks limit saves)
│   └── whatsapp/            ← {whatsapp} notes (WhatsApp Export Chat conversational text)
├── themes/                  ← {themes} LLM-curated Maps of Content (MoC) pages
├── digests/                 ← {digests} daily resurface digests (Bundle 2 populates this)
└── .lancedb/                ← embeddings index (Track B fills this)
```

## Tag conventions

Tags follow four namespaced taxonomies applied automatically during NER enrichment:

| Prefix | Meaning | Example |
|---|---|---|
| `#source/<domain>` | Where the link came from | `#source/substack.com` |
| `#ingest/<channel>` | How it entered the vault | `#ingest/whatsapp`, `#ingest/linkedin-zip` |
| `#entity/<name>` | Named entity extracted by NER | `#entity/anthropic`, `#entity/jensen-huang` |
| `#topic/<theme>` | Topic cluster extracted by NER | `#topic/large-language-models`, `#topic/ipo` |

Use Obsidian's tag panel (right sidebar) to browse all tags. Filter by prefix to
narrow to a taxonomy group.

## What's still pending

- **Bundle 2 digests** — daily resurface digest not yet running; `digests/` is empty.
- **Cross-source wikilinks** — the edge-builder worker creates within-theme wikilinks;
  cross-theme linking is not yet automated.
- **Embeddings backfill** — `.lancedb/` index is initialised but not yet populated
  at scale (Track B PR pending).
- **Instagram saves** — IG anonymous-mode blocks OG metadata; only URL stubs land.
- **WhatsApp routing** — raw WA export files currently land in `inbox/` rather than
  `sources/whatsapp/` until the routing worker is extended.

## How content flows in

| Channel | Mechanism |
|---|---|
| WhatsApp self-DM | Send a link to yourself → ZIP export watcher parses `_chat.txt` |
| Email / mailto | Forward to ingest address → IMAP poller picks up + dispatches |
| LinkedIn | Export ZIP from LinkedIn settings → watcher imports saved posts |
| YouTube | Share a YT link via WhatsApp or email → transcript fetch + metadata |
| Web | Any URL in an ingest message → Trafilatura article extraction |

All ingest paths write through `lib/vault_writer.py` (atomic write, slug dedup,
stable frontmatter serialization). NER enrichment, TL;DR generation, and MoC
page updates run as background workers after ingest.
"""


def render_readme(*, stats: dict[str, int], overview: str, date: Optional[str] = None) -> str:
    """Render the README template with live stats and the synthesised overview."""
    today = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _README_TEMPLATE.format(
        date=today,
        overview=overview,
        **{k: stats.get(k, 0) for k in (
            "total", "inbox", "web", "youtube", "linkedin",
            "instagram", "whatsapp", "themes", "digests",
        )},
    )


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-readme-", suffix=".md", dir=str(path.parent))
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


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vault_readme_refresh",
        description="Regenerate vault/README.md with live stats and LLM overview.",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip Azure OpenAI call; use the static fallback paragraph.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the generated README to stdout; do not write to disk.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Azure deployment name override.",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    vault_root = _resolve_vault_root()
    log.info("Vault root: %s", vault_root)

    stats = count_notes(vault_root)
    log.info("Note counts: %s", stats)

    if args.no_llm:
        from connecting_dots.enrichment.vault_readme_synth import _STATIC_PARAGRAPH
        overview = _STATIC_PARAGRAPH
        log.info("--no-llm: using static paragraph")
    else:
        from connecting_dots.enrichment.vault_readme_synth import synthesise_overview
        overview = synthesise_overview(stats=stats, model=args.model)
        log.info("LLM overview generated (%d chars)", len(overview))

    content = render_readme(stats=stats, overview=overview)

    if args.dry_run:
        print(content)
        log.info("--dry-run: no file written")
        return 0

    readme_path = vault_root / "README.md"
    _write_atomic(readme_path, content)
    log.info("Written: %s (%d bytes)", readme_path, len(content.encode("utf-8")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
