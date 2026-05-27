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
)
```

Component #2 calls `register_dispatcher(real_impl)` at startup to replace the
in-memory mock dispatcher with the real router.
