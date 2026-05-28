# Dispatch Playbook

A protocol for hand-off from a fresh chat to an AI Engineer subagent in a worktree
without burning tokens on re-reading the codebase. Born from the Bundle 1 v2
session-limit recovery; battle-tested.

---

## The one-line prompt

For ANY dispatch spec file in this folder, open a fresh Claude Code chat in the
project, switch the model to **Sonnet 4.6** (NOT Opus — anti-pattern flagged by
token-coach), and paste exactly this:

```
Read docs/dispatches/<file>.md in full, then dispatch the AI Engineer subagent
per §10 with isolation=worktree and model=sonnet. Pass the whole file as the
agent's brief. When the agent reports back, show me the PR URL and a 3-line summary.
```

Replace `<file>` with one of: `bundle-3-wikilinks`, `tag-dedup`, `untitled-fix`,
`body-cleanup`, `vault-readme`, `bundle-2-digest`.

---

## Available dispatches (Obsidian polish backlog)

| File | What it does | Parallel-safe? | Dispatch when |
|---|---|---|---|
| `bundle-3-wikilinks.md` | Cross-source wikilink writer — transforms graph view from firework to real clusters | ✅ yes | Anytime after Bundle 1 is in main |
| `tag-dedup.md` | Collapses 134 `#*ai*` variants etc. into canonical tags via LLM-as-judge | ✅ yes | Anytime after Bundle 1 is in main |
| `untitled-fix.md` | Re-rewrites the 352 "Untitled Note" titles using filename + entity fallbacks | ✅ yes | Anytime after Bundle 1 is in main |
| `body-cleanup.md` | Strips ad/nav cruft from web-scraped notes | ✅ yes | Anytime after Bundle 1 is in main |
| `vault-readme.md` | Curated vault root index + sidebar convention | ✅ yes | Anytime after Bundle 1 is in main |
| `bundle-2-digest.md` | Daily WhatsApp digest — resurfacing algorithm + WA outbound + Vercel cron | ⚠️ **AFTER** Bundle 3 + tag-dedup merge | Last; ~30-min single dispatch |

**Recommended order:**
1. Open chats 1-5 in parallel (or staggered — your call). Each dispatches one of
   the first five. They land independently as PRs #N..N+4.
2. **Merge in this order**: Bundle 3 → tag-dedup → untitled-fix → body-cleanup → vault-readme.
   No PR depends on another except cosmetically, but merging in this order keeps
   diffs clean.
3. **After 1-5 are all merged**: dispatch Bundle 2. It expects the wikilinks and
   deduplicated tags to be in place.

---

## Why this protocol exists (token-coach data, summarised)

| Anti-pattern flagged by token-coach | Mitigation baked in |
|---|---|
| 95% Opus usage | All specs request `model: "sonnet"` |
| Agents re-Read skills + code on every dispatch | Each spec **inlines** the relevant skill docs + code snippets the agent needs |
| Subagent transcripts back-fill orchestrator context | Orchestrator only reads the spec file once and dispatches; doesn't tail the agent's output |
| Cold-restart prompts cost $4-5 each (we hit 5×) | Worktree isolation + WIP-fallback at 35 tool calls means partial work always lands |
| Unbounded agent exploration | Hard caps: ≤7 new files, ≤25 tests, ≤45 tool calls, ≤800 LOC |

---

## Hard caps every spec enforces

These apply to every dispatch unless the spec overrides:

- **Model:** `sonnet` (Sonnet 4.6 or later)
- **Isolation:** `worktree`
- **Tool calls:** ≤ 45
- **New files:** ≤ 7
- **Modified files:** ≤ 3
- **Test count:** ≤ 25
- **LOC (excluding tests):** ≤ 800
- **WIP fallback at 35 tool calls or 600 LOC** → commit + push as `WIP:` PR

---

## What happens if a chat hits context limit

The worktree at the bottom of the chat is your saved progress. Push it
**BEFORE** opening another chat. Specifically:

```bash
cd "/Users/pw/Connecting Dots"
git fetch
# replace BRANCH with what the chat shows at the bottom
git push origin BRANCH
gh pr create --draft --title "WIP: <task name>" --body "Partial — see spec docs/dispatches/<file>.md"
```

Then open a fresh chat and tell it:
> PR #N has a partial implementation of <task>. Read the diff, see what's missing
> per `docs/dispatches/<file>.md`, dispatch a small follow-up to complete only the
> missing bits.

Follow-up dispatches read the diff (small) instead of redoing everything. They
cost ~$0.30 vs ~$3 for a fresh re-dispatch.

---

## Token cost expectations

| | Per dispatch |
|---|---|
| Orchestrator chat (reads 1 spec file, dispatches once, reports) | ~$0.05-0.10 |
| Dispatched AI Engineer agent (Sonnet, in worktree) | ~$1.50-2.50 |
| **Total Claude per dispatch** | **~$1.60-2.60** |
| Total Claude for full backlog (6 dispatches) | **~$10-15** |
| Total Azure (gpt-4.1) for backfill runs after merge | ~$15-25 |

---

## Anti-patterns to refuse if the agent proposes them

- "Let me first audit the whole codebase" — no, agent has the spec
- "I'll WebFetch the latest docs" — no, spec inlines the API patterns
- "Let me run the full test suite to check coverage" — no, hard-capped tests only
- "Refactor `connecting_dots/dispatcher.py` while I'm here" — no, out of scope
- "Open a follow-up PR to clean up tech debt" — no, only the spec
