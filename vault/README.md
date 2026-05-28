# Connecting Dots — Personal Knowledge Vault

> Last refreshed by `vault-readme` worker on 2026-05-29.

## What this is

Connecting Dots is a personal knowledge vault that continuously ingests saved links and content from WhatsApp, YouTube, LinkedIn, Instagram, and the web, storing each item as a structured Markdown note with YAML frontmatter. Every note is automatically enriched with named entities, topics, and a 2-sentence TL;DR using Azure OpenAI, then organised into theme pages (Maps of Content) for browsable discovery. The result is a self-updating second-brain that surfaces connections across thousands of saves without any manual tagging.

## Where to start

- **Browse by theme** → [[by-topic.base]] — table view of every note, filterable by topic
- **Best of finance** → [[financial-performance]], [[dividend-payout]], [[indian-stock-market]]
- **Best of product** → [[product-management]], [[design-systems]], [[hiring]]
- **Recent saves** → use Obsidian's "Files" panel sorted by "Modified time desc"

## Vault size at a glance

| Metric | Count |
|---|---|
| Total notes | 1,495 |
| Web articles | 1,075 |
| Inbox / unrouted | 307 |
| LinkedIn posts | 44 |
| YouTube transcripts | 37 |
| Instagram saves | 2 |
| WhatsApp messages | 0 |
| MoC theme pages | 30 |
| Digest pages | 0 |

## Folder map

```
vault/
├── README.md                ← you are here — navigation hub
├── inbox/                   ← 307 notes awaiting routing (mixed sources)
├── sources/
│   ├── web/                 ← 1,075 notes (articles, blogs, X threads, GitHub, finance)
│   ├── linkedin/            ← 44 notes (LinkedIn posts + ZIP export)
│   ├── youtube/             ← 37 notes (transcripts + video metadata)
│   ├── instagram/           ← 2 notes (OG meta; IG anon-blocks limit saves)
│   └── whatsapp/            ← 0 notes (WhatsApp Export Chat conversational text)
├── themes/                  ← 30 LLM-curated Maps of Content (MoC) pages
├── digests/                 ← 0 daily resurface digests (Bundle 2 populates this)
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
