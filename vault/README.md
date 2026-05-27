# Connecting Dots — Obsidian Vault

This is the canonical storage layer for Connecting Dots. Every captured item from
every source lane lands here as a single Markdown note with structured frontmatter.
A sidecar LanceDB index at `.lancedb/` provides vector search over the same items.

## Layout

```
vault/
├── inbox/              # Untriaged items (default landing for fresh captures)
├── sources/
│   ├── whatsapp/       # WA self-DM captures
│   ├── youtube/        # YT shares (transcript + metadata)
│   ├── instagram/      # IG shares (OG meta + caption)
│   └── linkedin/       # LinkedIn export + saved posts
├── themes/             # Theme/cluster notes (built by component #11 edge builder)
├── digests/            # Daily resurface digests (built by component #14)
└── .lancedb/           # Vector index sidecar (table: items)
```

## Frontmatter schema

Every note in `inbox/` or `sources/**` carries this YAML frontmatter:

```yaml
---
source: whatsapp | youtube | instagram | linkedin | web
captured_at: 2026-05-27T08:14:00Z   # ISO-8601 UTC
url: https://...                     # original URL (optional for raw text)
entities:                            # populated by NER (component #8)
  - name: "LanceDB"
    type: TECH
topics:                              # topic tags (component #11)
  - vector-databases
  - pkm
labels: []                           # user feedback: useful | neutral | noise
---
```

Body is free-form Markdown. Wikilinks (`[[other-note]]`) are honored by the edge
builder. See `inbox/example.md` for a working example.

## Writer contract

All writes go through `lib/vault_writer.py`. It guarantees:

- Slug collision avoidance (`-2`, `-3` suffixes)
- Atomic write (tmp file + rename)
- Frontmatter serialization order stable for diff hygiene
- Embedding hook fires post-write (component #9 — currently a TODO stub)
