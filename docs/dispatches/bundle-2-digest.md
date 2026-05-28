# Bundle 2 — Daily WhatsApp digest (product loop)

> See `docs/dispatches/README.md`. **Dispatch this LAST** — after Bundle 3
> (wikilinks) and tag-dedup are merged.

## 1. Goal

The product experience. At 8am every morning, a WhatsApp message arrives on
your phone with **5 curated items from your vault** that you should re-look at
today, picked by a hybrid resurfacing algorithm. Each item has a one-sentence
"why" reason. You react 👍 / 🤷 / 👎 — those labels train the algorithm.

This is when Connecting Dots stops being "a vault you opened once" and starts
being "a product you experience every morning".

**Cost:** ~$0.05/day Azure (one digest generation), ~$3-5 Claude for the
dispatch itself.

## 2. Why this scope is bigger

Unlike the other dispatches, this one touches:

- New Python: resurfacing algorithm + digest builder
- New TS / Next.js: cron endpoint, WA outbound API call
- New Vercel config: cron schedule
- New labels parquet: 👍 / 🤷 / 👎 storage
- WhatsApp interactive message format

Hard caps are slightly higher because of the breadth (see §9).

## 3. Codebase context (no need to re-Read; inlined)

### Existing WA send pattern

There's no outbound WA helper yet. You'll create it. The pattern matches the
inbound webhook validation in `app/api/webhooks/whatsapp/route.ts`. Outbound
goes to `https://graph.facebook.com/v22.0/<PHONE_NUMBER_ID>/messages` with
`Authorization: Bearer <WA_ACCESS_TOKEN>`.

Env vars already present in Vercel + local `.env`:
- `WA_PHONE_NUMBER_ID`
- `WA_ACCESS_TOKEN`
- `WA_VERIFY_TOKEN`
- `WA_APP_SECRET`

The user's WA number to send to is the same one that messages the test number
inbound — you can pin it as `WA_OWNER_NUMBER` env var (the user will add it).

### The vault state you can rely on

After Bundles 1-5 land:
- Every note has `entities`, `topics`, `tags` populated
- Every note has wikilinks to related notes
- Tags are deduplicated
- Web articles are body-cleaned
- 30 MoC pages exist in `vault/themes/`
- 5,800 unique entities, ~1,500 after dedup

You can read the vault from disk. No need for DB queries — markdown
frontmatter is enough.

### The hybrid resurfacing formula (canonical, do NOT modify)

From `docs/algorithm-reconciliation.md` (already in repo):

```
score(item) = w_t · time_decay(item)
            + w_r · activity_relevance(item, recent_activity)
            + w_p · static_profile_match(item, profile)
            - λ · diversity_penalty(item, already_selected_today)
```

Phase A defaults: `w_t=0.3, w_r=0.4, w_p=0.3, λ=0.7`.

- **time_decay** — bigger for older notes that haven't been resurfaced recently;
  smaller for very recent saves. Standard exponential `exp(-k * days_since_capture)`.
- **activity_relevance** — measure how related the note is to what the user has
  been engaging with this week. Use entity overlap with recently-labeled-👍 notes
  in the last 7 days. If no recent labels, fall back to recently-captured notes.
- **static_profile_match** — load from `~/.connecting_dots/active_themes.yaml`
  if exists (user-edited list of ≤5 themes they care about); compute Jaccard
  between note's topics and these themes. If file doesn't exist, fall back to
  top-5 most-common topics in the vault.
- **diversity_penalty** — MMR-style: each subsequent selected item penalises
  candidates that share entities with already-selected items in today's digest.

## 4. The flow

### Step 1: Python — `connecting_dots/digest/resurface.py`

Pure function:
```python
def select_digest_items(
    vault_root: Path,
    labels_db: Path,
    *,
    k: int = 5,
    weights: dict = DEFAULT_WEIGHTS,
    today: date | None = None,
) -> list[DigestItem]:
    """Return top-k items for today's digest using hybrid scoring."""
```

`DigestItem = NamedTuple("DigestItem", [("slug", str), ("title", str), ("score", float), ("reason", str), ("url", str | None)])`.

### Step 2: Python — `connecting_dots/digest/why_reason.py`

Per selected item, call gpt-4.1 to write a one-sentence "why you should
re-look at this today" reason. Pass the note's title, topics, and a hint about
why the algorithm picked it (e.g., "high activity_relevance because you reacted
👍 to 3 anthropic notes this week"). Tool-calling for structured output.

### Step 3: Python — `workers/digest_builder.py`

```bash
python -m workers.digest_builder [--date YYYY-MM-DD] [--dry-run] [--k 5]
```

- Selects items
- Generates reasons
- Composes a markdown digest at `vault/digests/<date>.md`
- Writes the digest payload to `data/digest_queue.jsonl` for the WA sender to
  pick up
- Stamps `data/digest_log.jsonl` with what was selected (for next-day
  diversity penalty)

### Step 4: TypeScript — `app/api/cron/digest/route.ts`

Next.js cron endpoint hit by Vercel cron at 0250 UTC (8:00 IST):

```ts
export const config = { runtime: 'nodejs' };
// Reads data/digest_queue.jsonl, sends WhatsApp message, archives queue
```

But wait — Vercel cron runs on Vercel infra, not your laptop, so it CAN'T
read your local `digest_queue.jsonl`. Two options:

**Option A (recommended for v1)**: Cron lives on your laptop via `cron` /
`launchd`. The Python digest builder runs locally and shells out to a WA-send
script (also Python). Skip the Next.js / Vercel cron entirely.

**Option B (proper, later)**: Push the digest payload to Upstash Redis from the
Python builder; Vercel cron reads from Upstash and sends. More moving parts;
defer to a follow-up PR.

**For this PR: implement Option A.** Generate a `launchd` plist or document the
crontab line. Document the upgrade path to Option B in the README.

### Step 5: Python — `connecting_dots/digest/wa_send.py`

```python
def send_digest(items: list[DigestItem], to: str, access_token: str, phone_number_id: str) -> dict:
    """POST to graph.facebook.com/v22.0/<phone_id>/messages with an interactive
    list message containing 5 items + reply buttons (👍 / 🤷 / 👎)."""
```

WA interactive message shape: use Meta's "interactive list" with each row
being a digest item; row IDs encode `<item_slug>:<reaction>`. When user taps,
the webhook receives the reply with the row ID; route to a label collector.

### Step 6: Python — extend `lib/inbound-dispatch.ts` to handle interactive replies

When the inbound webhook sees `message.type == "interactive"`, parse the row
ID, write a row to `data/labels.parquet` (or jsonl if parquet adds too much
LOC), then bubble through the normal pipeline. Don't break existing flows.

### Step 7: Labels storage — `data/labels.jsonl`

Per row:
```json
{"timestamp": "...", "item_slug": "...", "reaction": "thumbs_up", "user": "918595087697"}
```

(JSONL for simplicity; can migrate to parquet later for analytics.)

## 5. Files to create / modify

NEW:
- `connecting_dots/digest/__init__.py`
- `connecting_dots/digest/resurface.py`
- `connecting_dots/digest/why_reason.py`
- `connecting_dots/digest/wa_send.py`
- `connecting_dots/digest/labels.py` — label reader/writer
- `workers/digest_builder.py`
- `tests/digest/test_resurface.py`
- `tests/digest/test_why_reason.py`
- `tests/digest/test_wa_send.py`
- `tests/workers/test_digest_builder.py`

MODIFIED:
- `lib/inbound-dispatch.ts` — handle `type == "interactive"` reply rows
- `app/api/webhooks/whatsapp/route.ts` — pass interactive replies to dispatch
- `tests/route_whatsapp.test.ts` — add interactive-reply test
- `.env.example` — add `WA_OWNER_NUMBER`

DOC:
- `docs/digest-setup.md` — launchd plist or crontab line for daily 8am IST trigger

## 6. CLI quickstart (after merge)

```bash
# One-time setup
echo "WA_OWNER_NUMBER=918595087697" >> .env

# Test today's digest in dry-run
python -m workers.digest_builder --dry-run

# Generate + send today's digest
python -m workers.digest_builder

# Schedule daily 8am IST via launchd (instructions in docs/digest-setup.md)
launchctl load ~/Library/LaunchAgents/com.connectingdots.digest.plist
```

## 7. Tests (≤ 22 — slightly higher than other dispatches due to scope)

- 6 resurface tests (each weight component, full hybrid, cold-start, diversity)
- 3 why_reason tests (LLM mocked)
- 3 wa_send tests (request shape, error handling, retry)
- 4 digest_builder tests (end-to-end, idempotency, queue write, log)
- 3 labels tests (write, read, dedupe)
- 3 inbound-interactive tests (TS — parse, dispatch, no double-process)

## 8. Cold start — when no labels exist yet

For the first 7 days, `recent_activity` is empty (no 👍 history). Fall back to
pure recency-decay (w_t=1.0, w_r=0, w_p=0) per the kernel's cold-start spec.
After 7 days of digests + reactions, ramp to the hybrid defaults over days
8-14.

The bootstrap timing is read from `data/digest_log.jsonl` (first entry's
timestamp). Document in resurface.py.

## 9. Hard constraints — slightly relaxed for this dispatch

- Model: **sonnet**, isolation: **worktree**
- ≤ 60 tool calls (vs 45 elsewhere — bigger scope)
- ≤ 12 new files
- ≤ 4 modified files
- ≤ 22 tests
- ≤ 1200 LOC
- WIP fallback at **50 tool calls or 1000 LOC** — slightly higher because the
  digest is the product loop and shipping partial is worse than usual
- **Hard line:** if you can ship resurface + why_reason + digest_builder
  (steps 1-3) but not the WA send (step 5), STOP there and open a WIP PR.
  The user can run the worker manually and read the digest file in vault
  while we add WA outbound in a follow-up.

## 10. Commit + PR

```
feat(digest): daily WhatsApp digest with hybrid resurfacing (product loop)
```

PR title: `Daily WA digest: hybrid resurfacing + interactive label collection`

PR body should include:
- The hybrid formula (cite docs/algorithm-reconciliation.md)
- Cold-start behaviour
- Option A (local cron) vs Option B (Vercel cron + Upstash) tradeoffs
- Setup steps for the user (env vars + launchd plist)

## 11. Dispatch parameters

```yaml
subagent_type: "AI Engineer"
model: "sonnet"
isolation: "worktree"
description: "Bundle 2 — daily digest"
prompt: <full file>
```

## 12. After merge

User adds `WA_OWNER_NUMBER` to `.env` (and Vercel env), runs the manual test:

```bash
python -m workers.digest_builder
```

Should receive a WA interactive message on their phone within 10 seconds.
React 👍 / 🤷 / 👎 — labels start accumulating. Then load the launchd plist
for 8am-daily automation.

Day 8: first hybrid-scored digest fires.
Day 30: weight regression kicks in based on accumulated labels.
