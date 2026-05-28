# connecting-dots

Multi-channel URL capture pipeline. WhatsApp is the primary ingest channel;
mailto IMAP (this component) is the hot-spare fallback that becomes primary
if WhatsApp Meta verification stalls.

## Components

- **#1 WhatsApp inbound** — Meta Cloud API webhook (in progress)
- **#1.5 Cross-language bridge** — Upstash Redis Stream (`inbound-stream`).
  TS webhook (Vercel) `XADD`s; Python `workers.stream_consumer` `XREAD`s.
- **#2 URL dispatcher** — channel-agnostic router (`connecting_dots/dispatcher.py`)
- **#3 YouTube handler** — TBD
- **#4 Instagram handler** — TBD
- **#5 Web/PDF handler** — TBD
- **#6 mailto IMAP fallback** — `workers/mailto_poller.py` (this doc)
- **#7 LinkedIn ZIP watcher** — `workers/linkedin_zip_watcher.py` (this doc)
- **#8 WhatsApp self-DM Export Chat watcher** — `workers/whatsapp_export_watcher.py` (this doc)

## Upstash Redis Stream bridge — setup runbook

The WhatsApp webhook runs on Vercel (TypeScript); the URL pipeline runs on
your laptop (Python). The bridge is a single Upstash Redis Stream so messages
accumulate server-side while the laptop sleeps. At-least-once delivery,
deduped by WhatsApp `messages[].id`.

### 1. Provision the Upstash database

1. Open https://console.upstash.com/redis and sign in.
2. **Create Database** → name it `connecting-dots-inbound`, region close to
   Vercel's primary (e.g. `us-east-1`), free tier is fine.
3. From the database detail page, copy the two values under **REST API**:
   - `UPSTASH_REDIS_REST_URL` (looks like `https://xxx.upstash.io`)
   - `UPSTASH_REDIS_REST_TOKEN`
4. Paste both into your local `.env` (copy from `.env.example`).
5. Add the same two vars to the Vercel project: **Settings → Environment
   Variables → Add** for Production and Preview. Redeploy so the webhook
   picks them up (`vc deploy --prod` or push to main).

### 2. Shared envelope schema

The canonical schema is `schemas/inbound_envelope.schema.json`. Both sides
generate types from it — never hand-edit the generated files.

```bash
# TS type (used by lib/inbound-dispatch.ts)
npm run gen:types

# Python pydantic model (used by workers/stream_consumer.py)
make gen-types-py

# Both
make gen-types
```

Round-trip test (TS emits JSON, Python parses via codegenned model):

```bash
make test-bridge
```

### 3. Run the consumer

```bash
# One-time: create a 3.11+ venv and install Python deps
uv venv --python 3.11
uv pip install -e .

# Run the consumer (long-running, SIGTERM-safe, resumable)
.venv/bin/python -m workers.stream_consumer
```

The consumer keeps its stream offset at `data/stream_offset.txt` and its
dedupe table at `data/dedupe.db`. Both are gitignored. Safe to stop/restart
at any time — at-least-once semantics + dedupe = no double dispatch.

### Why a stream and not a queue/webhook fan-out?

- Laptop can sleep; messages buffer in Upstash (free tier: 10k commands/day).
- Equal SDK quality on both sides (`@upstash/redis` + `upstash-redis`).
- Cross-language without HTTP fan-out from Vercel to a home IP.
- The mailto poller intentionally **bypasses** the stream and calls
  `dispatch_url` in-process — the stream is the cross-language bridge only.

## mailto IMAP fallback — setup runbook

The poller reads unread Gmail messages under a specific label, extracts the
first URL, hands it to the dispatcher, and marks the message read.

### 1. Create a Gmail App Password

App Passwords require 2-Step Verification on the Google account.

1. Turn on 2-Step Verification: https://myaccount.google.com/signinoptions/twosv
2. Open https://myaccount.google.com/apppasswords
3. Name it `connecting-dots-imap` and click **Create**
4. Copy the 16-character password (shown once). Paste into `.env` as
   `IMAP_APP_PASSWORD` (strip spaces).

### 2. Create the label and filter

We use a Gmail plus-address so the filter is unambiguous. Example address:
`yourname+save@gmail.com` — Gmail treats it as `yourname@gmail.com` for
delivery but exposes the `+save` part in the `To:` header.

1. In Gmail → **Settings → Labels → Create new label** → name: `connecting-dots`
2. **Settings → Filters and Blocked Addresses → Create a new filter**
   - **To:** `yourname+save@gmail.com`
   - Click **Create filter**
3. On the next pane, check:
   - **Skip the Inbox (Archive it)**
   - **Apply the label:** `connecting-dots`
   - **Never send it to Spam**
4. **Create filter**

To capture a URL, email/forward it to `yourname+save@gmail.com`. Mobile share
sheets → Mail → To: that address works on iOS and Android.

### 3. Configure environment

```bash
cp .env.example .env
# Fill IMAP_USER and IMAP_APP_PASSWORD
```

### 4. Run

One-shot (for cron or testing):

```bash
python -m workers.mailto_poller once
```

Long-running (5-minute polling loop, SIGTERM-safe):

```bash
python -m workers.mailto_poller
```

### Polling cadence

Default is `IMAP_POLL_INTERVAL_S=300` (5 min). This bounds capture latency to
~5 minutes, which is acceptable for a fallback channel. If mailto becomes the
primary ingest channel, tighten to 60s — Gmail IMAP can sustain that easily
for a single-mailbox poller.

### Dispatcher contract

All channels (mailto, WhatsApp, LinkedIn, manual) call:

```python
from connecting_dots.dispatcher import dispatch_url

dispatch_url(
    url="https://example.com/article",
    source="mailto",                  # Literal["whatsapp","mailto","linkedin","manual"]
    captured_at=datetime.now(timezone.utc),
    raw_payload={...},                # channel-specific provenance
    message_id="mailto:<imap-uid>",   # optional; enables dedupe via data/dedupe.db
)
```

The dispatcher routes the URL to the first matching handler in
`connecting_dots/handlers/` (specific → generic, with `web` as the catch-all
fallback), then writes the resulting `NoteRecord` to the vault via
`lib.vault_writer.write_note`. Handler exceptions are caught and converted
into a degraded `NoteRecord(handler="failed", text="")` so a capture is
never silently lost.

## Adding a new handler

Handlers live in `connecting_dots/handlers/` and satisfy the `Handler`
Protocol from `connecting_dots/handlers/base.py`:

```python
# connecting_dots/handlers/reddit.py
from connecting_dots.generated.inbound_envelope import InboundEnvelope
from connecting_dots.types import NoteRecord


class RedditHandler:
    name = "reddit"

    def matches(self, url: str) -> bool:
        return "reddit.com" in url or "redd.it" in url

    def handle(self, envelope: InboundEnvelope) -> NoteRecord:
        # ... extract post body, comments, etc. ...
        return NoteRecord(
            source=envelope.source.value,
            handler=self.name,
            url=str(envelope.url),
            title="...",
            text="...",
            captured_at=envelope.captured_at,
            raw_meta={"subreddit": "..."},
        )


handler = RedditHandler()  # module-level singleton resolved by the dispatcher
```

Then register the module in `HANDLER_MODULES` in
`connecting_dots/dispatcher.py` — **specific handlers go before the `web`
fallback**:

```python
HANDLER_MODULES = [
    "connecting_dots.handlers.youtube",
    "connecting_dots.handlers.reddit",       # <-- new
    "connecting_dots.handlers.instagram",
    "connecting_dots.handlers.linkedin",
    "connecting_dots.handlers.web",          # MUST stay last
]
```

The resolver accepts three export conventions for backwards-compat:
`mod.handler`, `mod.{stem}_handler` (e.g. `youtube_handler`), or a class
named `{Stem}Handler` that it will instantiate.

### Tests

```bash
.venv/bin/python -m pytest tests/test_dispatcher.py -v
```

Test fixtures use `dispatcher.set_handlers([...])` with mock handler objects,
so they don't depend on real handler modules being present.

## LinkedIn ZIP watcher — setup runbook

LinkedIn does not expose a "saves" API. Instead, request a monthly data
export from LinkedIn, drop the ZIP into the watched folder, and the worker
unpacks it and feeds every saved article / reaction into the dispatcher.

### 1. Request the LinkedIn export

1. Open https://www.linkedin.com/mypreferences/d/download-my-data while
   signed in. (Alternative path: top-right avatar → **Settings & Privacy**
   → **Data Privacy** → **Get a copy of your data**.)
2. Choose **Want something in particular?** → check at minimum:
   - **Saved Articles**
   - **Reactions**
   - **Activity** (gives you Shares / Comments — useful for richer signal)
3. Click **Request archive**. LinkedIn emails the ZIP within ~24h (often
   minutes for "fast" data sets like saved articles).
4. Download the `Complete_LinkedInDataExport_*.zip` from the email link.

### 2. Drop the ZIP into the inbox

```bash
mkdir -p data/linkedin-inbox
mv ~/Downloads/Complete_LinkedInDataExport_*.zip data/linkedin-inbox/
```

The watcher will pick it up on the next poll cycle (60s default).

### 3. Run the watcher

One-shot (cron, manual, or first-run sanity check):

```bash
python -m workers.linkedin_zip_watcher once
```

Long-running daemon (60s polling, SIGTERM-safe):

```bash
python -m workers.linkedin_zip_watcher
# or, after `pip install -e .`:
linkedin-zip-watcher
```

### What it does to each ZIP

1. Validates it looks like a LinkedIn export (presence of `Saved Articles.csv`
   / `Reactions.csv` / `Shares.csv` / etc.).
2. Extracts to `data/linkedin-inbox/.unpacked/<utc-timestamp>_<zipname>/`.
3. Parses `Saved Articles.csv` (columns: `SavedAt, ArticleTitle, ArticleURL,
   ArticleAuthor`) and `Reactions.csv` (columns: `Date, Type, Link`). Headers
   are matched case- and underscore-insensitively to survive LinkedIn's
   periodic header drift.
4. Dispatches each row via the in-process `dispatch_url`. The downstream
   LinkedIn handler reads `raw_payload.linkedin_export=True` and skips the
   live fetch entirely — title/author come straight from the CSV row.
5. Moves the ZIP to `data/linkedin-inbox/.processed/`. Malformed or
   non-LinkedIn ZIPs are left in place with a warning log so you can inspect.

### Idempotency

`message_id` is `linkedin:<sha256(url|captured_at)>` — deterministic. If you
re-request the same export window (LinkedIn lets you), the stream
consumer's `seen_message_ids` SQLite table absorbs the replay. Safe to
re-import.

### Why polling and not inotify?

macOS FSEvents and Linux inotify both have quirks around files that are
move-renamed into a directory (atomic vs non-atomic). A 60-second
`os.scandir` poll is dead-simple, kills no batteries, and a monthly drop
doesn't need sub-second latency.

## WhatsApp self-DM backfill (Export Chat ZIP) — setup runbook

The live WhatsApp Cloud API webhook only delivers messages received
*after* the integration is wired up. To backfill years of saved links,
images, voice notes, and PDFs you've been DM'ing to yourself, use
WhatsApp's in-app **Export Chat (with Media)** feature on the "Message
yourself" chat and drop the resulting ZIP into the watched folder.

### 1. Export the self-DM from WhatsApp

On your phone:

1. Open WhatsApp → tap your own chat (the **Message yourself** /
   self-DM chat).
2. Tap the contact name at the top → scroll down → **Export Chat**.
   (Android: ⋮ menu → More → Export chat.)
3. Choose **Attach Media**. The export will be larger but will include
   every image / voice note / PDF — exactly the corpus we want.
4. WhatsApp creates a ZIP and offers a share sheet. Save it to Files /
   Drive / email it to yourself — whatever gets it onto the laptop.

### 2. Drop the ZIP into the inbox

```bash
mkdir -p data/whatsapp-exports
mv ~/Downloads/'WhatsApp Chat - You.zip' data/whatsapp-exports/
```

Any filename works — the watcher polls for `*.zip` at the inbox root.

### 3. Run the watcher

One-shot (cron, manual, or first-run sanity check):

```bash
python -m workers.whatsapp_export_watcher once
```

Long-running daemon (60s polling, SIGTERM-safe):

```bash
python -m workers.whatsapp_export_watcher
# or, after `pip install -e .`:
whatsapp-export-watcher
```

### What it does to each ZIP

1. Validates that the archive contains a `*.txt` transcript at its root
   (filename varies: `_chat.txt`, `WhatsApp Chat with Me.txt`, …).
2. Safely extracts to
   `data/whatsapp-exports/.unpacked/<utc-timestamp>_<zipname>/` with
   zip-slip, zip-bomb (2 GB total / 200 MB per file), and symlink
   protections.
3. Parses the transcript line by line. Both iOS (12-hour, U+200E
   left-to-right marks, `[bracket]` separator) and Android (24-hour,
   `-` separator) formats are supported. Date-locale ambiguity
   (DD/MM/YYYY vs MM/DD/YYYY) is resolved by preferring `dayfirst=True`
   (India), with a fallback to `dayfirst=False` for US locale exports.
4. Multi-line messages, "Forwarded" markers, and system/banner lines
   ("Messages and calls are end-to-end encrypted", "This message was
   deleted", `<Media omitted>`) are handled as expected.
5. Each message is dispatched as an `InboundEnvelope`:
   - **URL in the body** → `message_type="url"`, routed through the
     existing per-domain handlers (YouTube / Instagram / LinkedIn /
     web fallback) just like a live WhatsApp message.
   - **Plain text** → `message_type="text"`, raw handler writes it to
     `vault/inbox/_raw/`.
   - **Media** (image / video / audio voice note / document) →
     `message_type=<type>`, raw handler writes a note carrying
     `local_media_path` pointing at the extracted file in
     `.unpacked/…/`. Component #5 will fetch + enrich those later.
   - **Stickers** are dropped (parity with the live webhook).
6. The processed ZIP is moved to
   `data/whatsapp-exports/.processed/<utc-timestamp>/<zipname>`.

### Idempotency

Every message gets a deterministic
`message_id = whatsapp_export:<sha256(sender|captured_at|body_or_filename)[:16]>`.
Re-importing the same ZIP — or re-exporting the same chat next month —
is a no-op: the shared SQLite dedupe table at `data/dedupe.db` absorbs
the replay.

### Env vars

| Var                                 | Default              | Notes                                                                                  |
|-------------------------------------|----------------------|----------------------------------------------------------------------------------------|
| `WHATSAPP_EXPORT_INBOX_DIR`         | `data/whatsapp-exports` | Folder the watcher polls.                                                              |
| `WHATSAPP_EXPORT_POLL_INTERVAL_S`   | `60`                 | Poll cadence. Monthly drops don't need sub-minute latency.                             |
| `WHATSAPP_EXPORT_TZ`                | `Asia/Kolkata`       | IANA timezone applied to naive timestamps. The export file has no zone metadata — set this to your phone's timezone when the chat happened. |
