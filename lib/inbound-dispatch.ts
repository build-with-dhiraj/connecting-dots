/**
 * Inbound message dispatcher — cross-language bridge to the Python pipeline.
 *
 * Appends an `InboundEnvelope` to the Upstash Redis Stream `inbound-stream`.
 * The Python `workers.stream_consumer` reads from the same stream and calls
 * `connecting_dots.dispatcher.dispatch_url`.
 *
 * On any Upstash failure we fall back to a local JSONL log so we never drop a
 * message — Meta only retries on non-2xx, and the webhook always returns 200.
 */
import fs from "node:fs/promises";
import path from "node:path";
import { Redis } from "@upstash/redis";
import type { InboundEnvelope } from "@/lib/generated/inbound-envelope";

export type { InboundEnvelope } from "@/lib/generated/inbound-envelope";

/** Legacy interface — kept for the WhatsApp webhook's extraction helper. */
export interface InboundMessageEnvelope {
  receivedAt: string;
  phoneNumberId: string;
  from: string;
  messageId: string;
  type: string;
  raw: Record<string, unknown>;
}

const STREAM_KEY = "inbound-stream";
const LOG_DIR = path.resolve(process.cwd(), "logs");
const FALLBACK_FILE = path.join(LOG_DIR, "inbound-fallback.jsonl");

let _redis: Redis | null | undefined; // undefined = not yet probed; null = unavailable

function getRedis(): Redis | null {
  if (_redis !== undefined) return _redis;
  const url = process.env.UPSTASH_REDIS_REST_URL;
  const token = process.env.UPSTASH_REDIS_REST_TOKEN;
  if (!url || !token) {
    console.warn("[inbound-dispatch] UPSTASH_REDIS_REST_URL/TOKEN not set — using JSONL fallback");
    _redis = null;
    return null;
  }
  _redis = new Redis({ url, token });
  return _redis;
}

/** Lightweight runtime validation (schema-equivalent). Avoids pulling AJV at the edge. */
function validateEnvelope(env: InboundEnvelope): void {
  const allowedSources = ["whatsapp", "mailto", "linkedin", "manual"] as const;
  if (!env || typeof env !== "object") throw new Error("envelope must be an object");
  if (typeof env.message_id !== "string" || env.message_id.length === 0)
    throw new Error("message_id required");
  if (typeof env.url !== "string" || env.url.length === 0) throw new Error("url required");
  if (!allowedSources.includes(env.source as (typeof allowedSources)[number]))
    throw new Error(`source must be one of ${allowedSources.join(",")}`);
  if (typeof env.captured_at !== "string" || Number.isNaN(Date.parse(env.captured_at)))
    throw new Error("captured_at must be ISO 8601");
  if (!env.raw_payload || typeof env.raw_payload !== "object")
    throw new Error("raw_payload must be an object");
}

async function appendFallback(env: InboundEnvelope): Promise<void> {
  await fs.mkdir(LOG_DIR, { recursive: true });
  await fs.appendFile(FALLBACK_FILE, JSON.stringify(env) + "\n", "utf8");
}

/**
 * Append an envelope to the Upstash stream. On any failure, falls back to JSONL.
 * Channel relays (WhatsApp webhook today) call this; the consumer calls
 * `dispatch_url` on the Python side.
 */
export async function dispatchInboundMessage(env: InboundEnvelope): Promise<void> {
  validateEnvelope(env);
  const redis = getRedis();
  if (!redis) {
    await appendFallback(env);
    return;
  }
  try {
    // XADD inbound-stream * envelope <json>
    await redis.xadd(STREAM_KEY, "*", { envelope: JSON.stringify(env) });
  } catch (err) {
    console.error("[inbound-dispatch] xadd failed, falling back to JSONL", { err: String(err) });
    await appendFallback(env);
  }
}

const URL_RE = /https?:\/\/[^\s<>"'\])}]+/i;

function extractUrlFromWaMessage(msg: any): string | null {
  if (msg?.type === "text" && typeof msg?.text?.body === "string") {
    const m = msg.text.body.match(URL_RE);
    if (m) return m[0];
  }
  try {
    const s = JSON.stringify(msg);
    const m = s.match(URL_RE);
    if (m) return m[0];
  } catch {
    /* ignore */
  }
  return null;
}

/**
 * Extract `messages[]` from a Meta webhook payload, flattening across entry/changes.
 * Status updates (delivery/read receipts) are intentionally skipped.
 */
export function extractInboundMessages(payload: any): InboundMessageEnvelope[] {
  const out: InboundMessageEnvelope[] = [];
  const receivedAt = new Date().toISOString();
  const entries = Array.isArray(payload?.entry) ? payload.entry : [];
  for (const entry of entries) {
    const changes = Array.isArray(entry?.changes) ? entry.changes : [];
    for (const change of changes) {
      const value = change?.value;
      if (!value || !Array.isArray(value.messages)) continue;
      const phoneNumberId: string = value?.metadata?.phone_number_id ?? "";
      for (const msg of value.messages) {
        out.push({
          receivedAt,
          phoneNumberId,
          from: msg?.from ?? "",
          messageId: msg?.id ?? "",
          type: msg?.type ?? "unknown",
          raw: msg,
        });
      }
    }
  }
  return out;
}

/** Adapt a WhatsApp envelope (legacy shape) to the cross-channel envelope. */
export function whatsappToEnvelope(m: InboundMessageEnvelope): InboundEnvelope | null {
  const url = extractUrlFromWaMessage(m.raw);
  if (!url) return null;
  return {
    message_id: m.messageId,
    url,
    source: "whatsapp",
    captured_at: m.receivedAt,
    raw_payload: m.raw,
  };
}
