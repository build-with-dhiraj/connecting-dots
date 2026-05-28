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

        await dispatchInboundMessage(env);
      } catch (err) {
        console.error("[wa/webhook] dispatch failed", { messageId: m.messageId, err: String(err) });
      }
    }),
  );

  return NextResponse.json({ ok: true });
}

