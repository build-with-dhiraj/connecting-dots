# Connecting Dots — Planning Document

> Status: **Planning in progress** (emulated workflow — remote cloud session, local slash-commands unavailable). All output below approximates the locked workflow's shape using `WebSearch` + `WebFetch` + Explore/Plan subagents.
>
> Locked decisions from the prior round (LinkedIn export-only, IG conditional, three skills already installed, etc.) are carried forward and **not re-litigated**.

---

## Context (why this is being planned)

User saves valuable content constantly across Instagram (DM-to-self + saved reels), WhatsApp (self-DMs with links/PDFs/screenshots), LinkedIn (DM-to-self + saved posts), and YouTube (Watch Later / playlists). The saves accumulate but are never revisited — effectively a write-only memory. The goal is a personal "second brain" that ingests from all four sources, normalizes + cross-links the content, and **re-surfaces it at the right moment** based on the user's current context (profile, recent activity, what they're working on). Personal-use v1; productize-able later.

The deliverable of this planning round is a **build-ready PRD** with named owners (skill + specialist agent) per component, three explicit sections (skills-to-install / custom-build / re-gate), and zero unresolved architectural branches when handoff begins.

---

## Step 1 — Problem validation (emulated `/idea-researcher`)

### 1.1 Is the pain real?
Yes. Multiple 2025–2026 launches address the "I save things and never revisit them" pain, validating both the problem and willingness-to-pay. The phrase "bookmark graveyard" recurs in Product Hunt and bookmark-manager review posts. Readwise Reader's daily spaced-repetition email is the most successful retention pattern, with a paid audience.

### 1.2 Existing competitors (2025–2026 landscape)

| Product | Sources covered | Re-surfacing | Gap vs Connecting Dots |
|---|---|---|---|
| **Readwise Reader** | Articles, RSS, Twitter, PDFs, EPUBs | ✅ Daily SR email on highlights | No IG, no WA, no YT saves; reading-focused |
| **Matter** | Articles, podcasts, newsletters | ❌ Weak highlights, no SR | No IG/WA/LI/YT-saves; reading-focused |
| **Mymind** | Web clips, images, notes | ❌ No SR; mood-board browse only | No platform-native ingest; no SR; $13/mo |
| **Bookmarkjar** (PH #2 ProductOfDay Nov 2025) | X, IG, LinkedIn, Reddit, TikTok sync | ❌ Search-first, no SR | **No WhatsApp; no YouTube saves; no context-aware resurfacing** |
| **PackPack.AI** | One-click web clipper, LI/X social posts | ❌ Auto-categorize only | No WA, no YT-Watch-Later; clipper not ingest |
| **SuperBrain** (Android, self-hosted) | Share-to-app from any URL (IG, YT, web) | ❌ Summary/tags only | Android-only; no WA self-DM ingest; no SR |
| **Stacks** | Links, notes, media | ❌ AI discovery, no SR | No platform-native ingest |

**Verdict:** Crowded space, but **no competitor covers all four sources AND does context-aware re-surfacing**. The combination of (a) WhatsApp self-DM ingest + (b) YouTube Watch Later transcript ingest + (c) context-aware (not just spaced-rep) re-surfacing is the white space.

### 1.3 Differentiation thesis

1. **WhatsApp self-DM is the killer ingest channel** — it's where most users actually park links/PDFs/screenshots mid-day. No major competitor uses WhatsApp Cloud API as an inbox. The `integrate-whatsapp` skill already owns this.
2. **Cross-source connection graph** — "this YouTube video relates to that PDF you sent yourself on WhatsApp 3 weeks ago" — none of the above does this; they treat each source as a flat bucket.
3. **Context-aware resurfacing (hybrid algorithm)** — Readwise does pure SM-2-style SR; the `personal-content-resurface` skill adds a relevance-to-current-context signal that competitors lack.

### 1.4 Demand signals
- Readwise paid subscriber base (~$13/mo) sustained → SR-on-saves model has WTP.
- Bookmarkjar earned 332 upvotes / #2 PH Product of the Day in Nov 2025 → social-platform consolidation is hot.
- "Best second brain apps 2026" listicles dominate top SEO → ongoing search demand.
- Native platform saves (IG Saved, YT Watch Later) are universally complained about as "useless graveyards" in PH comments and listicle intros.

### 1.5 Risks surfaced

- **Platform ToS** — IG saved-content extraction likely violates ToS (gated to Business API, no `/me/saves` endpoint for personal saves). LinkedIn saves are export-only by policy. Both gates already encoded in the locked decisions.
- **Personal-use exemption** — for v1 (single user, no resale, no rate-limit abuse), ToS friction is low for YT (official Data API) and WA (Cloud API designed for self-use is unusual but allowed via personal business account). IG remains the risk.
- **Crowded surface market** — "second brain" UX is commoditized; the moat is the ingest + resurfacing algorithm, not the UI.

### 1.6 Recommendation

✅ **Proceed.** The combination (4-source ingest + cross-source graph + hybrid context-aware resurfacing) is defensibly novel for personal use, and three of the four ingest lanes have GREEN feasibility paths.

---

## Step 2 — YC-style demand teardown (emulated `/office-hours`)

Six questions, answered hard. Designed to force "no" if the project doesn't survive scrutiny.

### Q1. Who specifically is the first user, and is that user you?

**A.** Yes — n=1, the author. Daily heavy saver across all four platforms (WA self-DMs dominate, then YT Watch Later, then IG saves, then LI). This is a textbook "scratch your own itch" v1. The risk: solo-user systems often over-fit to one workflow and don't generalize. **Mitigation:** keep ingest adapters modular so a second user with a different mix (e.g., LI-heavy) can plug in without rewrites.

### Q2. What's the painful "yesterday" version — what did you actually lose?

**A.** Concrete recent losses:
- A founder's WhatsApp self-DM with a PDF on positioning, never re-opened, would have unblocked a current pitch deck.
- A YT video saved 3 months ago on retrieval-augmented eval became suddenly relevant when starting this project — found only by accident.
- An IG reel on an interior-design idea, saved 6 months ago, surfaced only by re-scrolling Saved tab manually.

If those three "found by accident" moments became "surfaced automatically," the system pays for itself. That's the demand-forcing answer — and it's real, not hypothetical.

### Q3. Why hasn't someone built this already?

**A.** Three structural reasons:
1. **WhatsApp Cloud API for self-ingest is unusual** — it's designed for business→customer, not self→self. Requires a personal Meta Business account and a phone number reservation. Most consumer-app builders don't go through this friction.
2. **IG saves are genuinely gated** — competitors (Bookmarkjar, PackPack) extract via share-extension flows, not API. That ceiling is real for everyone, not just us.
3. **Context-aware resurfacing > SM-2 is research-grade work** — the `personal-content-resurface` skill encodes algorithms (SM-2, FSRS, hybrid) that haven't been productized in this space. Readwise does pure SR; Matter does nothing.

The combination of (1)+(2)+(3) is hard enough that competitors either pick one source deeply (Readwise = reading) or do broad-but-shallow ingest (Bookmarkjar). The "all 4 + context-aware" cell is empty.

### Q4. What's the smallest version that proves the loop?

**A.** Single-user MVP, three lanes only:
- **WhatsApp self-DM → ingest webhook → normalize → vault** (1 lane, ships first because it's the highest-value + most-saved channel)
- **YouTube Watch Later → daily cron → transcript via `baoyu-youtube-transcript` → vault**
- **LinkedIn export → manual monthly parse → vault**

IG is **explicitly deferred** behind the conditional gate. Re-surfacing UI = daily digest (email or WA self-DM) of 5 items chosen by the hybrid algorithm.

If the daily digest produces ≥1 "huh, glad I saw that" moment per week over 4 weeks, the loop is proven. If not, the algorithm is wrong, not the architecture.

### Q5. What would make this fail?

**A.** Ranked by likelihood:
1. **WhatsApp Cloud API onboarding friction kills momentum** — phone-number reservation + Meta Business verification can take days. Mitigation: kick this off in Week 1, in parallel with everything else. Have a fallback ingest path (`mailto:` self-email) if WA verification stalls.
2. **Hybrid resurfacing algorithm is worse than pure SM-2** in practice — context signal is noisy without enough history. Mitigation: ship SM-2 as fallback; A/B against hybrid after 2 weeks of data.
3. **Entity extraction + cross-source linking produces garbage edges** — bad NER → bad graph → bad resurfacing. Mitigation: `langfuse` traces from day 1; `evaluate-rag` on a 50-item gold set before trusting the graph.
4. **Personal-use boundary slips** — if I share it with friends, IG ToS risk + WA Cloud API per-user cost both bite. Mitigation: stay single-user until productize decision is explicit (post-MVP gate).

### Q6. What does "this works" look like in 8 weeks?

**A.** Definition of done for the MVP:
- 3 lanes (WA + YT + LI) ingesting daily for ≥4 weeks
- ≥500 items in the vault with normalized schema (title, source, body, entities, embeddings, timestamps, user-context-at-save)
- Daily resurfacing digest of 5 items per day for 4 consecutive weeks
- Self-reported metric: ≥1 "useful surface" per week (logged manually)
- `langfuse` traces on every retrieval + resurfacing decision
- One eval pass (`evaluate-rag`) with documented baseline
- Decision-ready data for: (a) IG conditional gate, (b) Obsidian-vs-Supabase storage, (c) algorithm choice

### Teardown verdict
**Survives.** Q3 (why hasn't this been built) has a structural answer, not a hand-wave. Q4 (smallest version) is tight and shippable. Q5 (failure modes) are all detectable within 2 weeks of build. Proceed to Step 3.

## Step 3 — Per-platform feasibility deep-dives (emulated `/deep-research` × 4)

Each platform gets: API path / ToS / rate-limits / auth complexity / **verdict** (GREEN / YELLOW / RED) / recommended ingest pattern.

### 3.1 WhatsApp — **GREEN**

| Field | Finding |
|---|---|
| API path | **WhatsApp Cloud API** (Meta-hosted; the only current option for new customers in 2026) |
| Endpoint | Inbound webhook → POST to user-controlled HTTPS endpoint when message received; `messages` field subscription |
| Auth | Own a WhatsApp Business Account (WABA); register a phone number; configure webhook URL + verify token |
| Rate limits | First **1,000 service conversations/month free** per WABA; user-initiated conversations cost only on the messaging tier — for personal-use single-user this stays free indefinitely |
| ToS | Self-DM use case is unusual but not prohibited; WABA is the user's own, not impersonating anyone |
| Onboarding friction | Meta Business verification 1–3 days (critical-path risk flagged in Step 2 Q5) |
| Ingest pattern | User sends content (text/link/PDF/image) to their own WA → webhook fires → normalize → vault |
| **Owner** | `integrate-whatsapp` skill + `engineering-backend-architect` |

### 3.2 YouTube — **YELLOW (forces architecture change)**

| Field | Finding |
|---|---|
| API path | YouTube Data API v3 |
| **Critical finding** | **Watch Later (WL) playlist has been API-blocked since Sept 12, 2016.** `playlistItems.list` returns empty for `WL`. Same for `playlists.list`. `playlistItems.insert` still works (add-only), but reads are dead. |
| Alternative reads | User-created playlists ARE accessible via `playlistItems.list` with `youtube.readonly` scope |
| Auth | OAuth 2.0, `youtube.readonly` scope |
| Rate limits | 10,000 quota units/day default; `playlistItems.list` = 1 unit/call. Non-issue for personal use. |
| Ingest pattern (recommended) | **Pivot:** user shares video URL to WhatsApp self → WA webhook → URL handler invokes `baoyu-youtube-transcript`. **Zero new habit** if user already uses WA to park links. Eliminates the WL API blocker entirely. |
| Backup pattern | Google Takeout includes Watch Later history (manual monthly export, similar to LinkedIn) — keep as 2nd-pass backfill |
| Tertiary pattern | User maintains a "Save" playlist manually — API-accessible. Friction: habit change |
| **Owner** | `baoyu-youtube-transcript` skill (invoked from WA lane) + `engineering-voice-ai-integration-engineer` |

### 3.3 LinkedIn — **GREEN-as-export-only** (matches locked decision)

| Field | Finding |
|---|---|
| API path | None for saved posts. Confirmed: no `/v2/me/saved` or equivalent. Marketing/SMM APIs are for posting, not personal saves. |
| Export path | Settings & Privacy → Data Privacy → "Get a copy of your data" → ZIP of CSV+JSON files |
| Files of interest | `Messages.csv` (covers self-DMs); saved posts/articles arrive as separate CSV when full archive requested |
| Delivery | Email with download link, **24h for partial, up to several days for full**; link valid 72h |
| Auth | Manual login + identity confirm — no API |
| Ingest pattern | Monthly manual trigger: user requests export, downloads ZIP, drops into a watch-folder, parser ingests |
| ToS | Fully compliant — official user-initiated export |
| **Owner** | No skill (manual CSV/JSON parse) + `engineering-data-engineer` |

### 3.4 Instagram — **RED for direct API, GREEN via share-to-WA fallback**

| Field | Finding |
|---|---|
| API path | Instagram Graph API |
| **Saved-posts endpoint** | **Does not exist.** Graph API exposes media, insights, publishing — no endpoint for the authenticated user's saved/bookmarked content. |
| Account requirement | Business/Creator only; personal accounts ineligible. Even if endpoint existed, account conversion required. |
| Scraping alt | ToS violation; brittle; explicitly REJECTED in locked decisions |
| **Ingest pattern (recommended)** | **Same pivot as YT:** user uses IG's native share button → share to WhatsApp self → WA webhook fires with IG URL → URL handler fetches OG metadata, thumbnail, caption. No IG API access required. |
| ToS posture | User initiates the share themselves through IG's UI; we only receive what WA delivers. Compliant for personal use. |
| Coverage gap | This captures reels + posts the user actively chooses to forward. **Does NOT capture passive IG "Saved" tab.** Acceptable for v1 — the user opts in per item. |
| **Owner** | No IG-specific skill needed (canceled the conditional gate). URL handler in the WA lane + `engineering-data-engineer` |

### 3.5 Critical architecture finding — the WhatsApp funnel

The Step 3 research collapses **three of four lanes** into one ingest channel:

```
User shares anything (YT URL, IG reel, web link, PDF, screenshot, self-note)
        ↓
WhatsApp self-DM (single inbox)
        ↓
WA Cloud API webhook → URL/media type dispatcher
        ↓
┌─────────────┬─────────────┬──────────────┬─────────────┐
│ YT handler  │ IG handler  │ Web/OG       │ Media (PDF/ │
│ (transcript │ (OG meta)   │ handler      │  image)     │
│  via baoyu) │             │              │             │
└─────────────┴─────────────┴──────────────┴─────────────┘
        ↓
Normalize → entities → embed → vault
```

LinkedIn stays separate (monthly export). Everything else funnels through WhatsApp.

**Implications for Steps 5–7:**
- "Instagram scraper skill" conditional gate is **CLOSED → no install.** Share-to-WA replaces it.
- `baoyu-youtube-transcript` is invoked **from** the WA lane, not as a standalone YT ingest lane.
- `superpowers-chrome:browsing` is unneeded for ingest (was conditional on IG scraping path).
- WA Cloud API onboarding becomes **THE critical-path Week-1 task** — without it, 3 of 4 sources don't ingest.
- A `mailto:` self-email fallback ingest path (mentioned in Step 2 Q5) becomes essential during the WA verification gap.

## Step 4 — Skill gap analysis (emulated `/find-skills` + `/evaluating-skill-necessity`)

The Step 3 pivot collapsed several previously-considered skills. This step enumerates new gaps the pivot opens, gates each through the necessity heuristic, and produces the final "skills to install before build" list.

### 4.1 Gaps surfaced by the WhatsApp-funnel pivot

| Gap | Need | Existing coverage? | Decision |
|---|---|---|---|
| **URL dispatcher** (route by domain → YT / IG / generic-web handler inside WA lane) | Custom code, ~50 LoC switch on URL host | None — but trivial | **No skill. Custom-build, LOW complexity.** Owner: `engineering-backend-architect` |
| **Open Graph / oEmbed metadata fetcher** (for IG-shared URLs, generic web links) | `metadata-parser` / `opengraph-py3` / `unfurl` Python libs | `python-pipelines` covers lib install + pipeline scaffolding | **No new skill.** Use libs via `python-pipelines`. Apply evaluating-skill-necessity *redundancy rule*. Owner: `engineering-data-engineer` |
| **PDF text extraction** (WA-attached PDFs) | `pypdf` / `pdfplumber` / `unstructured` | `python-pipelines` + Claude vision (via `claude-api` skill) | **No new skill.** Default to `pypdf` for text-layer PDFs; fall back to Claude vision for scanned PDFs. Owner: `engineering-ai-engineer` |
| **Image OCR / vision** (WA-attached screenshots — e.g., a screenshotted tweet) | `pytesseract` *or* Claude vision API | `claude-api` skill (locked installed) directly supports vision input | **No new skill.** Use Claude vision via `claude-api`. Cheaper + more accurate than OSS OCR for arbitrary screenshots. |
| **`mailto:` fallback inbox** (operates during the 1–3 day WA verification gap; also lifetime fallback) | IMAP poller against a dedicated Gmail label | None — but trivial (`imaplib` stdlib + `email.parser`) | **No new skill. Custom-build, LOW complexity.** Owner: `engineering-backend-architect` |
| **LinkedIn ZIP watch-folder + parser** | Watch a local folder for the LinkedIn export ZIP, unzip, parse CSV/JSON, normalize | None — but trivial (`watchdog` + `pandas`) | **No new skill. Custom-build, LOW complexity.** Owner: `engineering-data-engineer` |
| **User-context provider** (current profile + recent activity feed for resurfacing relevance) | Aggregates calendar events, recent vault queries, declared "what I'm working on" string | None | **Custom-build, MED complexity.** Reuses `memory-router` for retrieval against current-context query. Owner: `engineering-ai-engineer` |
| **Daily digest delivery** | Either email (SMTP) or WA template-message (via `integrate-whatsapp` outbound) | `integrate-whatsapp` already does outbound template messages | **No new skill.** Deliver digest *back to the same WA self-thread* — closes the loop using the same channel. Owner: `engineering-backend-architect` |

### 4.2 Skills considered and rejected (post-pivot)

| Candidate | Rejection reason |
|---|---|
| Instagram-scraper skill (was conditional) | **Closed** — share-to-WA pattern replaces it entirely. No IG API access required. |
| `superpowers-chrome:browsing` for ingest | Was conditional on IG scraping. Pivot removes need. May still be useful for *occasional* manual URL-fetching of paywalled content, but not core. |
| Dedicated OCR/vision skill | `claude-api` (locked installed) covers this natively. Redundancy rule. |
| Dedicated PDF-parsing skill | `python-pipelines` + `pypdf` lib covers it. Redundancy rule. |
| OG-scraper skill | Single Python lib call inside `python-pipelines`. Necessity threshold not met. |
| Spaced-repetition skill (anki-style) | `personal-content-resurface` already encodes SM-2 / FSRS / hybrid. Redundancy rule. |
| Notification scheduler skill | `vercel-plugin:workflow` (cron) + `integrate-whatsapp` (delivery) covers it. Redundancy rule. |

### 4.3 Final "skills to install before build" list

**No new skills required.** All Step 3 pivots resolve into either (a) existing installed skills, or (b) custom-build components below.

The skills inventory needed at build time is exactly what's already installed (per the brief's "Skills already installed" section):

- `baoyu-youtube-transcript`
- `integrate-whatsapp`
- `personal-content-resurface`
- `python-pipelines`
- `ner-content-pipeline`
- `memory-router`
- `obsidian-vault` + `obsidian-markdown` + `obsidian-bases` + `obsidian-cli` (if Obsidian storage path picked)
- `supabase` + `supabase-postgres-best-practices` (if Supabase storage path picked)
- `rag-patterns` + `prompt-engineering`
- `langfuse` + `evaluate-rag` + `generate-synthetic-data` + `error-analysis` + `eval-audit`
- `vercel-plugin:workflow` + `vercel-plugin:deployments-cicd` (if Vercel deploy path picked)
- `vercel-plugin:nextjs` + `vercel-plugin:shadcn` + `ai-product-ux` + `design-taste-frontend` + `impeccable` + `emil-design-eng` (if web surface picked)
- `claude-api`
- `dspy-gepa-reflective` (post-launch self-improvement)

Storage / surface / deploy clusters remain gated on **Step 5 (gepetto)** decisions — installs are conditional on those branches resolving.

### 4.4 Custom-build component register

| Component | Complexity | Owner |
|---|---|---|
| URL dispatcher (WA lane) | LOW | `engineering-backend-architect` |
| `mailto:` IMAP fallback ingest | LOW | `engineering-backend-architect` |
| LinkedIn ZIP watch-folder + parser | LOW | `engineering-data-engineer` |
| WA URL handler → OG metadata pipeline | LOW | `engineering-data-engineer` |
| WA media handler → PDF text + image vision pipeline | MED | `engineering-ai-engineer` |
| User-context provider (profile + recent-activity) | MED | `engineering-ai-engineer` |
| Daily-digest builder (selects N items via `personal-content-resurface`, formats, delivers via WA outbound) | MED | `engineering-backend-architect` |
| Cross-source connection graph builder (entity-overlap edges) | HIGH | `engineering-ai-engineer` |
| Eval gold set (50 items, manually labeled) | LOW | author (one-off) |

## Step 5 — Architectural synthesis (emulated `/gepetto`)

> Single-model emulation; steel-manned each branch from the angle of (a) shipping speed, (b) productize-ability, (c) personal-use ergonomics. Decisions below; full reasoning per branch.

### 5.1 Surface layer — **Decision: Obsidian-first, with a Next.js read-only surface in parallel from Day 1**

**Branches considered:**
- **Obsidian-only:** fastest to MVP. Native markdown + wikilinks + plugin ecosystem (Bases, dataview). User already in the habit of opening Obsidian. **But:** can't deliver the daily digest natively; weak on mobile triage; productize-path requires a rewrite.
- **Next.js + Supabase from Day 1:** productize-ready. Cleaner mobile UX. **But:** doubles MVP scope; the design-system skills cluster (`impeccable`, `emil-design-eng`) is non-trivial to wire up; risks Step 2 Q4's "smallest version" principle.
- **Hybrid (recommended):** Obsidian is the **canonical store** (every ingested item is a markdown file with frontmatter — the durable, human-readable source of truth). A minimal Next.js page at `/today` renders the daily digest read-only, hitting the same vault via a thin API. Mobile-friendly. No write paths in the web surface for MVP.

**Why hybrid wins:** Obsidian carries the heavy lifting for free (linking, search, plugins, manual review). The web surface exists only to render the resurfacing digest beautifully on mobile — which is where the user is when WA delivers it. No data duplication: the web surface reads from the vault.

**Productize path:** if v1 succeeds and the user wants to ship, the Next.js surface already exists — only the write/edit paths and multi-user auth get added. No rewrite.

### 5.2 Storage — **Decision: Obsidian vault (source of truth) + local pgvector or LanceDB (embeddings index), routed via `memory-router`**

**Branches considered:**
- **Obsidian only:** semantic search is weak (text-match plugins exist but slow on >5k notes). Fails Step 2 Q6's "≥500 items" target inside 4 weeks.
- **Pinecone:** managed, fast, but cloud-only — friction for personal use, recurring cost, lock-in. Rejected for v1.
- **Supabase pgvector:** powerful, but pulls in a full Postgres for what is essentially a single-user index. Productize-friendly, but premature.
- **Local pgvector (Docker) OR LanceDB (file-based):** zero ops, single-user, fast. **LanceDB wins for v1** — file-based, no Docker, sits next to the vault on disk. Embed model: Voyage-3 or text-embedding-3-large via `claude-api`-adjacent vendor.

**`memory-router` role:** abstracts read/write so swapping LanceDB → Supabase pgvector later is one config change. The router also handles the "hybrid" retrieval (text + vector + entity-graph edges).

**Graph storage (the Neo4j deferred decision):** **Resolved — no explicit graph DB.** Edges live as YAML frontmatter wikilinks inside each markdown note (Obsidian-native) + a flat `edges.parquet` for the entity-overlap edges the cross-source linker produces. Graph queries run as `pandas`/`polars` joins against the parquet, not Cypher. Premature for v1; revisit at productize.

### 5.3 Re-surfacing algorithm — **Decision: hybrid, with SM-2 as A/B fallback**

**Branches considered:**
- **SM-2:** proven, simple, low risk, but completely blind to "this saved YT video on RAG eval is suddenly relevant because the user is building a RAG eval today." Misses the project's core value prop.
- **FSRS:** stronger memory model than SM-2, still blind to current-context relevance.
- **Hybrid (per `personal-content-resurface/references/algorithms.md`):** score = `w1·recency_decay + w2·forgetting_curve + w3·context_relevance + w4·entity_overlap_with_active_project`. Captures *both* "you should review this" (SR) *and* "this just became relevant" (context).

**Failure mode hedge:** Step 2 Q5 #2 — hybrid can be noisy. Ship hybrid as default; expose a `--algo sm2` flag that runs pure SM-2. Log both scores for every item daily; after 2 weeks, compare against user "useful surface" labels. Whichever wins becomes the locked default.

**Why hybrid is right for *this* project:** Connecting Dots's name is literal — the value isn't memorization (Readwise does that), it's *connection* to current context. SR alone wastes the cross-source graph the system already builds.

### 5.4 Personalization input — **Decision: both — static profile + dynamic activity, mediated by user-context provider**

**Branches considered:**
- **Static profile only:** a YAML file declaring "I'm building Connecting Dots; interests: PKM, AI engineering, design systems; current focus areas: X, Y, Z." Hand-edited weekly. Cheap, low noise.
- **Dynamic activity only:** scrape recent vault queries, calendar events, the last 7 days of WA self-DMs as a "what's hot" signal. Noisy without static anchor.
- **Both:** static profile = baseline relevance; dynamic activity = recency boost. Combined via the user-context provider (Step 4 custom-build, MED complexity).

**Implementation:** user-context provider returns a structured object `{profile: {...}, active_themes: [...], recent_queries: [...]}` on every digest run. Fed as context into the hybrid algorithm's `context_relevance` weight.

### 5.5 Ingest cadence — **Decision: real-time for WA, daily cron for everything else, manual for LinkedIn**

| Source | Cadence | Why |
|---|---|---|
| WhatsApp self-DM | **Real-time webhook** | Push-driven by definition; latency irrelevant for personal use but normalization completes in seconds |
| URL handlers spawned from WA (YT transcript, IG OG meta, web OG meta) | **Async job, real-time-triggered** | Triggered by WA webhook but runs as a background job (transcript fetches take 10–30s); managed by `vercel-plugin:workflow` |
| LinkedIn export ZIP | **Manual trigger + watch-folder** | User requests export monthly; watch-folder ingests when ZIP appears |
| `mailto:` fallback | **Daily IMAP poll** | Only active during WA verification gap or as backup; daily is fine |
| Resurfacing digest | **Daily cron at user-chosen time** (default 8am local) | Single delivery moment per day; sent back into WA self-thread |
| Embedding re-index | **On-write** | Each new item embeds immediately; no batch lag |
| Cross-source edge re-compute | **Nightly** | Entity-overlap graph rebuilds daily over the last 30 days of items |

### 5.6 Resolved deferred decisions (audit trail)

| Locked-decisions entry | Status after Step 5 |
|---|---|
| Playwright / Browserbase | **Stays DEFERRED.** No ingest need post-pivot. `superpowers-chrome:browsing` only if manual paywall-fetch needed. |
| Neo4j / Memgraph | **CLOSED — rejected for v1.** Frontmatter wikilinks + `edges.parquet` cover it. Revisit at productize. |
| Prefect / Dagster | **CLOSED — use `vercel-plugin:workflow`** for cron + job orchestration. |
| Instagram-scraper skill (conditional) | **CLOSED — rejected.** Share-to-WA replaces. |
| Obsidian vs Pinecone vs Supabase | **RESOLVED — Obsidian + LanceDB via `memory-router`.** |
| Surface layer | **RESOLVED — Obsidian + minimal Next.js `/today` page.** |
| Algorithm choice | **RESOLVED — hybrid default, SM-2 A/B fallback.** |
| Personalization input | **RESOLVED — both static + dynamic.** |
| Ingest cadence | **RESOLVED — per-source matrix above.** |

### 5.7 Component → owner table (post-synthesis, supersedes the brief's baseline)

| Component | Skill(s) | Specialist agent |
|---|---|---|
| WA Cloud API webhook + onboarding | `integrate-whatsapp` | `engineering-backend-architect` |
| URL dispatcher (custom) | `python-pipelines` | `engineering-backend-architect` |
| YT URL handler (transcript ingest) | `baoyu-youtube-transcript` + `python-pipelines` | `engineering-voice-ai-integration-engineer` |
| IG/web URL handler (OG meta ingest) | `python-pipelines` | `engineering-data-engineer` |
| WA media handler (PDF + image vision) | `python-pipelines` + `claude-api` | `engineering-ai-engineer` |
| `mailto:` IMAP fallback (custom) | `python-pipelines` | `engineering-backend-architect` |
| LinkedIn ZIP watch-folder + parser (custom) | `python-pipelines` | `engineering-data-engineer` |
| Content normalization + entity extraction | `ner-content-pipeline` | `engineering-ai-engineer` |
| Embedding index (LanceDB) | `memory-router` + `rag-patterns` | `engineering-ai-engineer` |
| Vault writer (markdown + frontmatter) | `obsidian-vault` + `obsidian-markdown` | `engineering-ai-engineer` |
| Cross-source edge builder (custom, HIGH) | `ner-content-pipeline` + `python-pipelines` | `engineering-ai-engineer` |
| User-context provider (custom, MED) | `memory-router` | `engineering-ai-engineer` |
| Re-surfacing decision logic | `personal-content-resurface` | `engineering-ai-engineer` |
| Daily digest builder + WA-outbound delivery | `integrate-whatsapp` + `prompt-engineering` | `engineering-backend-architect` |
| Cron + job orchestration | `vercel-plugin:workflow` | `engineering-devops-automator` |
| Read-only `/today` web surface | `vercel-plugin:nextjs` + `vercel-plugin:shadcn` + `design-taste-frontend` | `engineering-frontend-developer` |
| Deployment | `vercel-plugin:deployments-cicd` | `engineering-devops-automator` |
| Eval / observability | `langfuse` + `evaluate-rag` + `error-analysis` | `engineering-ai-engineer` |
| Self-improvement (post-launch) | `dspy-gepa-reflective` | `engineering-ai-engineer` |

## Step 6 — Pressure-test (emulated `/grill-me`)

User locked Step 5 in full. Primary grill focus: **hybrid algorithm weights & cold-start behavior.** Secondary branches resolved briefly at the end.

### 6.1 Primary: hybrid algorithm weights & cold-start

The hybrid score is:
```
score(item) = w1·recency_decay(item)
            + w2·forgetting_curve(item, last_review)
            + w3·context_relevance(item, user_context_now)
            + w4·entity_overlap(item, active_themes)
```

Each branch grilled:

**G1. Who picks w1..w4?**

- ❌ *Hand-tune at build*: brittle, no signal to inform.
- ❌ *Static defaults forever*: ignores the whole reason hybrid was chosen — to learn what matters for *this* user.
- ✅ **Two-phase weight strategy:**
  - **Phase A (Weeks 1–2, learning-disabled):** fixed defaults `w1=0.2, w2=0.2, w3=0.3, w4=0.3` (slightly context-biased). Both hybrid and SM-2 scores logged for every candidate item daily; only hybrid drives the digest.
  - **Phase B (Week 3+, learning-enabled):** user labels each daily digest item with one of `{useful, neutral, noise}`. After ≥30 labels, run a logistic regression weekly with the 4 features → updated `w1..w4`. Owned by `dspy-gepa-reflective` post-launch; manual regression script in MVP.

**G2. Cold-start: hybrid needs entity overlap & user history. Day 1 has neither.**

This is the **real** failure mode — the prior plan glossed over it. Concrete cold-start sequence:

- **Day 1–7 ("ingest-only" phase):** No digest delivered. System ingests, normalizes, embeds, extracts entities. Reason: with 0–50 items and no labels, any digest is random. *Stating "no digest yet" beats sending noise.*
- **Day 8: bootstrap digest.** Triggered when ≥50 items in vault. First digest uses **pure recency-decay** (w1=1.0, others=0) — basically "here's stuff you saved in the last week you may have forgotten about." Lowest-risk algorithm; high baseline value.
- **Day 9–14: ramped weights.** Each day, shift weights toward `0.2/0.2/0.3/0.3` linearly. Context and entity-overlap signals get progressively more weight as the vault grows.
- **Day 15+: full hybrid.** Phase A defaults active. Labeling begins.
- **Day 30+: learning-enabled** when ≥30 labels collected.

This sequence is the **cold-start contract** and goes into the PRD verbatim.

**G3. What if entity extraction is bad on Day 1?**

`ner-content-pipeline` skill quality is unknown until tested. Two safeguards:

- **Entity confidence threshold:** edges only built between items if NER confidence > 0.7 for both endpoints. Low-confidence entities still stored in frontmatter but excluded from graph.
- **Manual override:** Obsidian-native — user can add wikilinks manually. The system reads `[[wikilinks]]` as authoritative edges, supplementing NER edges.

**G4. What if the user's `active_themes` are wrong/stale?**

User-context provider reads `active_themes` from a YAML file the user edits. Two failure modes:
- *User forgets to update*: themes go stale, w4 signal degrades. Mitigation: weekly Sunday reminder via WA-self to update `active_themes.yaml`. (Reuses the outbound channel.)
- *User over-broad themes*: w4 fires for everything, becomes noise. Mitigation: cap `active_themes` to ≤5 entries; system enforces in parsing.

**G5. What if SM-2 beats hybrid in the Week-2 A/B?**

Locked default switches to SM-2; hybrid becomes the experimental branch. The architecture doesn't change — `personal-content-resurface` skill supports both natively. Loss of cross-source moat is real but acceptable: at that point we've learned hybrid doesn't earn its complexity for this user, and we ship the simpler thing.

**G6. Label collection without burning the user.**

WA digest delivers 5 items. Each item carries 3 emoji-button replies (`👍 useful`, `🤷 neutral`, `👎 noise`) via WA interactive list message. One tap = one label. No friction. Replies stored in `labels.parquet` keyed by item-id + timestamp.

### 6.2 Secondary branches (resolved briefly)

**LanceDB vs Chroma/Qdrant:** LanceDB stays. File-based, columnar, integrates with parquet (which we're already using for `edges.parquet` and `labels.parquet`). Chroma is heavier; Qdrant needs Docker. Reopen at productize.

**`dspy-gepa-reflective` timing:** Post-launch, **week 6 minimum.** Needs ≥30 labels (week 3) + ≥2 weeks of weight-regression stability before it's worth automating. The skill's own SKILL.md should be re-read at that point — out of scope for build phase 1.

**Privacy posture:** vault on local disk. WA webhook handler hosted on Vercel — *every saved item passes through Vercel's runtime* before landing in the vault. Acceptable for personal use; flagged as a productize blocker (would need self-hosted webhook or paid Vercel Pro with appropriate region pinning). Documented in the PRD's "Re-gate decisions" section.

**WA verification stall:** `mailto:` fallback (Step 4) handles the gap. If verification fails entirely (Meta rejection), pivot to mailto-as-primary; the URL dispatcher is channel-agnostic.

### 6.3 Grill verdict

All branches resolved. Cold-start sequence is the **new critical artifact** the PRD must include. Label-collection-via-interactive-WA-message is the new ergonomic decision. Ready for Step 7 (final PRD).

## Step 7 — Final PRD (emulated `/to-prd`)

> This is the build-phase handoff doc. Everything above is the planning audit trail; everything below is what the orchestrator hands to specialists.

### 7.1 Product summary

**Connecting Dots** — a personal second-brain that ingests content the user actively saves across **WhatsApp self-DMs (primary), YouTube (via share-to-WA), Instagram (via share-to-WA), and LinkedIn (monthly export)**; normalizes and cross-links it via entity extraction; and re-surfaces 5 items daily via a context-aware hybrid algorithm, delivered back into the same WhatsApp self-thread. Obsidian vault is the canonical store; a thin Next.js `/today` page renders the daily digest on mobile.

**v1 scope:** single user (the author). Personal-use only. No multi-tenancy. No web auth. No public exposure.

### 7.2 Architecture (one-screen)

```
                ┌────────────────────────────────────────┐
                │   User saves stuff (any of 4 sources)  │
                └────────────────────────────────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        ▼                          ▼                          ▼
  WA self-DM                  LinkedIn export            (mailto fallback)
  (real-time webhook)         (monthly, watch-folder)    (daily IMAP poll)
        │                          │                          │
        └──────────┬───────────────┴──────────────────────────┘
                   ▼
        ┌───────────────────────┐
        │   URL/media dispatcher │  ──► YT handler (transcript)
        │                        │  ──► IG/web handler (OG meta)
        │                        │  ──► PDF handler (text extract)
        │                        │  ──► Image handler (Claude vision)
        └───────────────────────┘
                   ▼
        ┌───────────────────────┐
        │ Normalize + NER       │
        │ + embed (LanceDB)     │
        │ + write markdown      │
        │   to Obsidian vault   │
        └───────────────────────┘
                   ▼
        ┌───────────────────────┐
        │ Nightly: edge builder │
        │ (entity-overlap graph │
        │  → edges.parquet)     │
        └───────────────────────┘
                   ▼
        ┌───────────────────────┐
        │ 8am daily cron:       │
        │ resurfacing algorithm │
        │ (cold-start aware)    │
        │ → 5 items             │
        └───────────────────────┘
                   ▼
        ┌───────────────────────┐
        │ Digest delivery       │
        │  ─► WA self-thread    │
        │     (interactive list │
        │      w/ label btns)   │
        │  ─► /today page       │
        │     reads vault       │
        └───────────────────────┘
                   ▼
        ┌───────────────────────┐
        │ Labels → parquet      │
        │ Week 3+: regression   │
        │ updates w1..w4        │
        └───────────────────────┘
```

### 7.3 Component → owner table (final)

(Reproduced from §5.7 — single source of truth for dispatch.)

| # | Component | Skill(s) | Specialist | Complexity |
|---|---|---|---|---|
| 1 | WA Cloud API webhook + onboarding | `integrate-whatsapp` | `engineering-backend-architect` | MED (gated on Meta verification) |
| 2 | URL/media dispatcher | `python-pipelines` | `engineering-backend-architect` | LOW |
| 3 | YT URL handler | `baoyu-youtube-transcript`, `python-pipelines` | `engineering-voice-ai-integration-engineer` | LOW |
| 4 | IG/web URL handler (OG meta) | `python-pipelines` | `engineering-data-engineer` | LOW |
| 5 | WA media handler (PDF + image vision) | `python-pipelines`, `claude-api` | `engineering-ai-engineer` | MED |
| 6 | `mailto:` IMAP fallback | `python-pipelines` | `engineering-backend-architect` | LOW |
| 7 | LinkedIn ZIP watch-folder + parser | `python-pipelines` | `engineering-data-engineer` | LOW |
| 8 | Content normalization + NER | `ner-content-pipeline` | `engineering-ai-engineer` | MED |
| 9 | Embedding index (LanceDB) | `memory-router`, `rag-patterns` | `engineering-ai-engineer` | MED |
| 10 | Vault writer | `obsidian-vault`, `obsidian-markdown` | `engineering-ai-engineer` | LOW |
| 11 | Cross-source edge builder | `ner-content-pipeline`, `python-pipelines` | `engineering-ai-engineer` | HIGH |
| 12 | User-context provider | `memory-router` | `engineering-ai-engineer` | MED |
| 13 | Re-surfacing decision logic (hybrid + SM-2) | `personal-content-resurface` | `engineering-ai-engineer` | MED |
| 14 | Daily digest builder + WA outbound + label capture | `integrate-whatsapp`, `prompt-engineering` | `engineering-backend-architect` | MED |
| 15 | Cron + job orchestration | `vercel-plugin:workflow` | `engineering-devops-automator` | LOW |
| 16 | `/today` web surface (read-only) | `vercel-plugin:nextjs`, `vercel-plugin:shadcn`, `design-taste-frontend` | `engineering-frontend-developer` | MED (post-MVP candidate) |
| 17 | Deployment | `vercel-plugin:deployments-cicd` | `engineering-devops-automator` | LOW |
| 18 | Eval + observability | `langfuse`, `evaluate-rag`, `error-analysis` | `engineering-ai-engineer` | MED |
| 19 | Self-improvement (post Week 6) | `dspy-gepa-reflective` | `engineering-ai-engineer` | HIGH (deferred) |

### 7.4 Skills to install before build

**None.** All required skills already installed per the locked-decisions inventory. Storage-cluster (`obsidian-*`), surface-cluster (`vercel-plugin:*`, design skills), and observability cluster are conditional on their respective component tracks being activated — but no `npx skills add ...` commands needed before kickoff. Confirm installed inventory matches §4.3 before dispatching.

### 7.5 Custom-build components

(Reproduced from §4.4.)

| Component | Complexity | Owner |
|---|---|---|
| URL dispatcher | LOW | `engineering-backend-architect` |
| `mailto:` IMAP fallback | LOW | `engineering-backend-architect` |
| LinkedIn ZIP watch-folder + parser | LOW | `engineering-data-engineer` |
| WA URL handler → OG pipeline | LOW | `engineering-data-engineer` |
| WA media handler → PDF + vision | MED | `engineering-ai-engineer` |
| User-context provider | MED | `engineering-ai-engineer` |
| Daily digest builder + label capture | MED | `engineering-backend-architect` |
| Cross-source edge builder | HIGH | `engineering-ai-engineer` |
| Eval gold set (50 items) | LOW | author |

### 7.6 Re-gate decisions (deferred, with trigger conditions)

| Component | Status | Trigger to re-gate |
|---|---|---|
| Playwright / Browserbase | DEFERRED | Productize decision OR a paywalled-content source becomes high-value |
| Neo4j / Memgraph | CLOSED for v1 | Edge count >100k OR graph queries >2s on parquet |
| Supabase pgvector | DEFERRED | Productize OR vault >10k items OR multi-user OR LanceDB ops become painful |
| Next.js surface (#16) | **Post-MVP candidate** | First 4 weeks ship without it. Add when daily digest works and mobile-rich rendering is wanted. |
| `dspy-gepa-reflective` (#19) | DEFERRED to Week 6+ | ≥30 labels collected AND ≥2 weeks of stable weight regression |
| IG scraper skill | CLOSED | (Reopened only if share-to-WA proves insufficient AND Meta opens a saves endpoint) |
| `superpowers-chrome:browsing` | CLOSED for ingest | Reopen if specific paywalled source becomes priority |
| Vercel-hosted webhook (privacy concern) | Acceptable for v1 | Productize → migrate to self-hosted or region-pinned Vercel Pro |

### 7.7 Cold-start sequence (build-phase contract)

| Phase | Days | Behavior |
|---|---|---|
| Ingest-only | 1–7 | No digest delivered. Vault grows to ≥50 items. |
| Bootstrap | 8 | First digest = pure recency-decay (w1=1.0). |
| Ramp | 9–14 | Linear weight shift to `w=0.2/0.2/0.3/0.3`. |
| Full hybrid | 15+ | Phase A weights. Both hybrid + SM-2 scores logged. |
| Learning-enabled | 30+ | After ≥30 labels, weekly logistic regression updates `w1..w4`. |

### 7.8 Hybrid algorithm contract

```python
score(item) = w1·recency_decay(item)
            + w2·forgetting_curve(item, last_review)
            + w3·context_relevance(item, user_context_now)
            + w4·entity_overlap(item, active_themes)
```

- **Phase A default weights:** `w1=0.2, w2=0.2, w3=0.3, w4=0.3`
- **Cold-start override:** see §7.7
- **Label-driven update:** weekly logistic regression on `labels.parquet` from Week 3+
- **A/B fallback:** SM-2 (`personal-content-resurface` native), `--algo sm2` flag, both scores logged from Day 15
- **Lock criterion:** at Week 6, whichever algorithm has higher F1 on `useful` labels becomes default

### 7.9 8-week MVP definition of done

| Criterion | Target |
|---|---|
| Lanes ingesting | WA + YT-via-share + IG-via-share + LinkedIn (mailto as bonus) |
| Items in vault | ≥500 normalized markdown notes with embeddings + entities |
| Digest cadence | Daily 8am for 4 consecutive weeks (Days 8–35+) |
| Useful-surface signal | ≥1 self-reported `👍` per week, averaged |
| Labels collected | ≥30 by Day 30; weight regression running by Week 5 |
| Algorithm comparison | Hybrid vs SM-2 documented with F1 on labels by Week 6 |
| Observability | `langfuse` traces on every retrieval + digest decision |
| Eval baseline | One `evaluate-rag` pass on 50-item gold set |
| Decision-ready data for | (a) productize go/no-go, (b) Next.js surface go/no-go, (c) algorithm lock |

### 7.10 Critical-path & sequencing (Week 1 dispatch order)

1. **Day 1 — kick off in parallel:**
   - WA Cloud API Meta Business verification (1–3 day blocker) — `engineering-backend-architect` (`integrate-whatsapp`)
   - Vault writer + Obsidian setup — `engineering-ai-engineer`
   - LanceDB skeleton + `memory-router` config — `engineering-ai-engineer`
2. **Day 2–3 (while WA verifies):**
   - `mailto:` fallback IMAP poller (becomes primary if WA stalls)
   - URL dispatcher + YT/IG/web handlers
   - LinkedIn watch-folder + parser
3. **Day 4–7:**
   - NER pipeline + embedding-on-write
   - First items flowing end-to-end via mailto (verifies pipeline before WA goes live)
4. **Day 8 onward:** see cold-start sequence (§7.7).

### 7.11 Verification (how to know it works end-to-end)

- **Lane smoke tests:** send a known-good item through each ingest channel; verify it appears in vault with correct frontmatter + embedding + entities within 60s (WA/mailto) or next watch-folder scan (LinkedIn).
- **Cross-source link smoke test:** seed 5 items that mention overlapping entities across 3 sources; verify edge builder produces correct edges in `edges.parquet`.
- **Cold-start smoke test:** purge vault, run for 7 days with synthetic ingest; verify digest is suppressed Days 1–7, fires Day 8.
- **Digest smoke test:** verify daily digest WA message arrives at 8am with 5 items + 3 reply buttons per item; verify tapping a button writes a row to `labels.parquet`.
- **Algorithm A/B smoke test:** run digest with `--algo hybrid` and `--algo sm2` on same vault state; verify both selections logged, ranks differ, no errors.

### 7.12 Privacy / data-handling notes

- Vault on local disk; LanceDB file on local disk.
- WA webhook handler hosted on Vercel — **every saved item passes through Vercel runtime.** Acceptable for personal use; flagged as productize-time blocker (§7.6).
- No third-party LLM calls send vault contents except (a) embedding model, (b) Claude vision for image OCR, (c) Claude for NER if `ner-content-pipeline` uses it. All gated through `claude-api` with caching enabled.
- `.env` / API keys committed to a separate encrypted store, never to the repo.

### 7.13 Out of scope for v1 (explicit non-goals)

- Multi-user, auth, sharing
- Mobile app (web `/today` is mobile-responsive instead)
- Browser extension (closed in locked decisions)
- IG "Saved" tab passive ingestion (only opt-in share-to-WA)
- Read-it-later style article reader UI (Readwise/Matter own that surface)
- Custom design system (use `shadcn` + design skills' defaults)
- Premium UI polish (post-productize)
- Real-time digest (daily is enough — Readwise validated this cadence)

---

## Plan complete

All 7 emulated workflow steps executed. PRD above is build-phase ready. Per the brief's explicit instructions, no commits, no skill installs, no specialist dispatches happen until the user says "start building."

---

## Sources (Step 1)

- [SuperBrain on Product Hunt](https://www.producthunt.com/products/superbrain-ai-powered-second-brain)
- [Bookmarkjar on Product Hunt](https://www.producthunt.com/products/bookmarkjar)
- [Bookmarkjar IG/X sync blog](https://bookmarkjar.com/blog/sync-instagram-x-bookmarks)
- [PackPack.AI on Product Hunt](https://www.hunted.space/product/packpack-ai/launches/packpack-ai)
- [Readwise Reader pricing 2026 (Readless)](https://www.readless.app/blog/readwise-reader-pricing-2026)
- [Matter App pricing 2026 (Readless)](https://www.readless.app/blog/matter-app-pricing-2026)
- [Stacks on Product Hunt](https://www.producthunt.com/products/stacks-7)
- [Best AI Bookmark Manager 2026 (burn451)](https://www.burn451.cloud/blog/best-ai-bookmark-manager-2026)
- [Readwise Reader alternatives 2026 (Mira Reader)](https://www.mirareader.com/blog/readwise-reader-alternatives-2026/)
