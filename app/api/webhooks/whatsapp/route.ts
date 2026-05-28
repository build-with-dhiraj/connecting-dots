/**
 * Meta WhatsApp Cloud API webhook.
 *
 * GET  -> verify-token handshake (Meta calls this once when you register the URL).
 *         The compare is constant-time so a network attacker cannot probe the
 *         token byte-by-byte via response-timing differences.
 * POST -> inbound message + status events. We HMAC-validate (constant-time
 *         in `verifyMetaSignature`), extract messages, dispatch.
 *
 * Notes:
 * - Runs on Node.js runtime (need crypto + fs).
 * - We must read the raw body BEFORE JSON.parse so the HMAC matches Meta's signed bytes.
 * - Always respond 200 quickly. Meta retries on non-2xx and can disable the webhook on
 *   sustained failures. Heavy work belongs in component #2 (queue/dispatcher).
 */
import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { NextRequest, NextResponse } from "next/server";
import { verifyMetaSignature } from "@/lib/wa-signature";
import {
  dispatchInboundMessage,
  extractInboundMessages,
  whatsappToEnvelope,
} from "@/lib/inbound-dispatch";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Constant-time string compare. Short-circuits on length mismatch (which is
 * already a leak-safe early return — length is not the secret), then uses
 * `crypto.timingSafeEqual` over equal-length byte buffers.
 */
function constantTimeEqual(received: string, expected: string): boolean {
  if (received.length !== expected.length) return false;
  const a = Buffer.from(received);
  const b = Buffer.from(expected);
  try {
    return crypto.timingSafeEqual(a, b);
  } catch {
    return false;
  }
}

export async function GET(req: NextRequest) {
  const url = new URL(req.url);
  const mode = url.searchParams.get("hub.mode");
  const token = url.searchParams.get("hub.verify_token");
  const challenge = url.searchParams.get("hub.challenge");

  const expected = process.env.WA_VERIFY_TOKEN;
  if (
    mode === "subscribe" &&
    token &&
    expected &&
    constantTimeEqual(token, expected)
  ) {
    // Meta requires the raw challenge string echoed back, 200 OK, text/plain.
    return new NextResponse(challenge ?? "", {
      status: 200,
      headers: { "content-type": "text/plain" },
    });
  }
  return new NextResponse("forbidden", { status: 403 });
}

export async function POST(req: NextRequest) {
  const appSecret = process.env.WA_APP_SECRET;
  if (!appSecret) {
    console.error("[wa/webhook] WA_APP_SECRET not configured");
    return NextResponse.json({ error: "server misconfigured" }, { status: 500 });
  }

  const raw = await req.text();
  const signature = req.headers.get("x-hub-signature-256");

  if (!verifyMetaSignature(raw, signature, appSecret)) {
    console.warn("[wa/webhook] signature mismatch", { hasHeader: Boolean(signature) });
    return NextResponse.json({ error: "invalid signature" }, { status: 401 });
  }

  let payload: any;
  try {
    payload = JSON.parse(raw);
  } catch (err) {
    console.error("[wa/webhook] invalid json", { err: String(err) });
    return NextResponse.json({ error: "invalid json" }, { status: 400 });
  }

  const messages = extractInboundMessages(payload);
  console.log("[wa/webhook] received", { messageCount: messages.length, object: payload?.object });

  // Adapt to cross-channel envelope. Messages without URLs are skipped (e.g. media-only).
  // Dispatch in parallel; downstream failures never bubble — webhook always acks 200.
  await Promise.allSettled(
    messages.map(async (m) => {
      try {
        const env = whatsappToEnvelope(m);
        if (!env) {
          console.log("[wa/webhook] no url in message — skipping", { messageId: m.messageId });
          return;
        }

        // Handle digest reaction replies: interactive list_reply with a row ID
        // encoding "<slug>__<reaction>". Write to data/labels.jsonl directly
        // (local deployment — Option A, no Vercel cron / Upstash for labels).
        if (env.message_type === "interactive" && env.interactive_id) {
          const handled = await _handleDigestReaction(
            env.interactive_id,
            m.from,
            env.captured_at,
          );
          if (handled) {
            console.log("[wa/webhook] digest reaction recorded", {
              messageId: m.messageId,
              interactiveId: env.interactive_id,
            });
            return; // reaction handled — no need to dispatch to stream
          }
        }

        await dispatchInboundMessage(env);
      } catch (err) {
        console.error("[wa/webhook] dispatch failed", { messageId: m.messageId, err: String(err) });
      }
    }),
  );

  return NextResponse.json({ ok: true });
}

/**
 * Attempt to parse a digest reaction from an interactive list row ID.
 *
 * Row ID format: "<slug>__<short_reaction>" where short_reaction in {up, shrug, down}.
 * Writes a row to data/labels.jsonl.
 *
 * Returns true if the row ID was a valid digest reaction (even if the write failed),
 * so the caller can skip normal dispatch. Returns false if the ID doesn't match the
 * digest reaction format (caller should dispatch normally).
 *
 * This function is intentionally async-safe and never throws — errors are logged only.
 */
async function _handleDigestReaction(
  rowId: string,
  from: string,
  capturedAt: string,
): Promise<boolean> {
  const SEP = "__";
  const idx = rowId.lastIndexOf(SEP);
  if (idx === -1) return false;

  const slug = rowId.slice(0, idx);
  const short = rowId.slice(idx + SEP.length);

  const REACTION_MAP: Record<string, string> = {
    up: "thumbs_up",
    shrug: "shrug",
    down: "thumbs_down",
  };
  const reaction = REACTION_MAP[short];
  if (!reaction || !slug) return false;

  // Write to data/labels.jsonl. On Vercel this would be /tmp; locally it's data/.
  const labelsDir =
    process.env.VERCEL === "1"
      ? "/tmp"
      : path.resolve(process.cwd(), "data");
  const labelsFile = path.join(labelsDir, "labels.jsonl");

  try {
    await fs.mkdir(labelsDir, { recursive: true });
    const row = JSON.stringify({
      timestamp: capturedAt,
      item_slug: slug,
      reaction,
      user: from,
    });
    await fs.appendFile(labelsFile, row + "\n", "utf8");
    console.log("[wa/webhook] label written", { slug, reaction, user: from });
  } catch (err) {
    console.error("[wa/webhook] label write failed", { err: String(err), slug, reaction });
    // Still return true — we identified this as a digest reaction, just couldn't persist it.
  }

  return true;
}
