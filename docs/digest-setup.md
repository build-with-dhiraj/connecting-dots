# Digest Setup — Daily 8am WhatsApp digest (Option A: local cron)

## Prerequisites

1. Bundle 1 has run: vault has enriched notes with `entities`, `topics`, `tags`.
2. Set the required env var:
   ```bash
   echo "WA_OWNER_NUMBER=918595087697" >> .env
   ```
3. Confirm these env vars are present in `.env` (already set from earlier bundles):
   - `WA_ACCESS_TOKEN`
   - `WA_PHONE_NUMBER_ID`
   - `AZURE_OPENAI_ENDPOINT`
   - `AZURE_OPENAI_API_KEY`

## One-time test

```bash
# Preview without sending or writing files
python -m workers.digest_builder --dry-run

# Full run: write digest + send WA message
python -m workers.digest_builder
```

You should receive a WhatsApp interactive list message within 10 seconds.
React 👍 / 🤷 / 👎 by tapping the sections — labels accumulate in `data/labels.jsonl`.

## Schedule via launchd (macOS) — 8am IST = 2:30am UTC

### 1. Create the plist

Save as `~/Library/LaunchAgents/com.connectingdots.digest.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.connectingdots.digest</string>

    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/env</string>
        <string>-i</string>
        <string>HOME=/Users/YOURUSERNAME</string>
        <string>/Users/YOURUSERNAME/path/to/Connecting Dots/.venv/bin/python</string>
        <string>-m</string>
        <string>workers.digest_builder</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/Users/YOURUSERNAME/path/to/Connecting Dots</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>VAULT_ROOT</key>
        <string>/Users/YOURUSERNAME/path/to/Connecting Dots/vault</string>
    </dict>

    <!-- 2:30am UTC = 8:00am IST -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>2</integer>
        <key>Minute</key>
        <integer>30</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>/Users/YOURUSERNAME/Library/Logs/connectingdots-digest.log</string>

    <key>StandardErrorPath</key>
    <string>/Users/YOURUSERNAME/Library/Logs/connectingdots-digest-error.log</string>

    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
```

Replace `YOURUSERNAME` and the `WorkingDirectory` / `ProgramArguments` paths.

### 2. Load the job

```bash
launchctl load ~/Library/LaunchAgents/com.connectingdots.digest.plist
```

### 3. Verify

```bash
launchctl list | grep connectingdots
# Should show the job with PID 0 (dormant) and exit code 0

# Test fire immediately (runs the job right now)
launchctl start com.connectingdots.digest
```

### 4. Check logs

```bash
tail -f ~/Library/Logs/connectingdots-digest.log
tail -f ~/Library/Logs/connectingdots-digest-error.log
```

### 5. Unload

```bash
launchctl unload ~/Library/LaunchAgents/com.connectingdots.digest.plist
```

## Alternative: crontab (Linux / macOS)

```bash
# Edit crontab
crontab -e

# Add this line (2:30am UTC = 8:00am IST):
30 2 * * * cd "/path/to/Connecting Dots" && .venv/bin/python -m workers.digest_builder >> logs/digest.log 2>&1
```

## Cold-start behaviour

- **Days 0–7**: No label history exists. The algorithm uses pure recency-decay
  (`w_t=1.0, w_r=0.0, w_p=0.0`). You'll see the most recently captured notes.
- **Days 8–14**: Linearly ramping to hybrid weights. Activity relevance
  (based on your 👍 reactions) begins contributing.
- **Day 14+**: Full hybrid scoring (`w_t=0.3, w_r=0.4, w_p=0.3, λ=0.7`).
  The digest becomes increasingly personalised to your reaction history and
  the themes in `~/.connecting_dots/active_themes.yaml`.

## Option B (future upgrade): Vercel cron + Upstash

For production deployment where the laptop is not always on:

1. The Python builder pushes the `digest_queue.jsonl` payload to **Upstash Redis**
   at the end of its run.
2. A Vercel cron endpoint (`app/api/cron/digest/route.ts`) fires at 02:30 UTC,
   reads from Upstash, and calls the WhatsApp Graph API.
3. This requires: `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN` (already
   used by the inbound webhook), and adding the cron route + `vercel.json` schedule.

The Python builder already writes `data/digest_queue.jsonl` — Option B just adds
a Redis push step and a Vercel cron reader. This is a ~50 LOC addition and
suitable for a follow-up PR.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "No items selected" | Vault may have no enriched notes. Run Bundle 1 backfills first. |
| WA message not received | Check `WA_OWNER_NUMBER`, `WA_ACCESS_TOKEN`, `WA_PHONE_NUMBER_ID` in `.env` |
| Reactions not saving | Check `data/labels.jsonl` is writable; verify the webhook is live |
| Same notes every day | Labels haven't accumulated yet (days 0–7 cold-start). React to build history. |
| Digest too topically narrow | Edit `~/.connecting_dots/active_themes.yaml` (list of ≤5 themes) |
