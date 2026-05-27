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

/**
 * P0-3: Vercel's serverless filesystem is read-only EXCEPT `/tmp`. Writing the
 * JSONL fallback under `process.cwd()/logs` raises EROFS at runtime and we
 * silently lose the message. On Vercel we write to `/tmp` (ephemeral, but at
 * least the message_id surfaces in `vercel logs`); locally we keep the
 * repo-rooted path so devs can inspect drops.
 */
function _fallbackPaths(): { dir: string; file: string } {
  if (process.env.VERCEL === "1") {
    return { dir: "/tmp", file: "/tmp/inbound-fallback.jsonl" };
  }
  const dir = path.resolve(process.cwd(), "logs");
  return { dir, file: path.join(dir, "inbound-fallback.jsonl") };
}

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
  const { dir, file } = _fallbackPaths();
  try {
    await fs.mkdir(dir, { recursive: true });
    await fs.appendFile(file, JSON.stringify(env) + "\n", "utf8");
  } catch (err) {
    // Last-resort: surface the dropped message_id in the logs so it isn't lost
    // silently if even /tmp write fails (e.g. permissions, full disk).
    console.error("[inbound-dispatch] fallback write failed, dropping message", {
      message_id: env.message_id,
      err: String(err),
    });
    throw err;
  }
  if (process.env.VERCEL === "1") {
    // On Vercel, /tmp is wiped between invocations — also log so the message_id
    // is retrievable via `vercel logs` even after the file is gone.
    console.error("[inbound-dispatch] message written to ephemeral /tmp fallback", {
      message_id: env.message_id,
    });
  }
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

/**
 * P1-URL_RE: require at least one dot in the host portion. The previous regex
 * `https?:\/\/[^\s<>"'\])}]+` happily matched bare-scheme strings like
 * `https://` (with nothing after) or `https://foo` (no TLD), which then choke
 * the pydantic `AnyUrl` validator downstream and surface as a 500 instead of
 * a clean "no URL" skip.
 *
 * We still validate with `new URL()` after the regex match — the dot rule is
 * cheap pre-filtering; the constructor is the source of truth.
 */
const URL_RE = /https?:\/\/[^\s<>"'\])}]*\.[^\s<>"'\])}]+/i;

function _validUrl(candidate: string): string | null {
  try {
    return new URL(candidate).toString();
  } catch {
    return null;
  }
}

function extractUrlFromWaMessage(msg: any): string | null {
  if (msg?.type === "text" && typeof msg?.text?.body === "string") {
    const m = msg.text.body.match(URL_RE);
    if (m) {
      const v = _validUrl(m[0]);
      if (v) return v;
    }
  }
  try {
    const s = JSON.stringify(msg);
    const m = s.match(URL_RE);
    if (m) {
      const v = _validUrl(m[0]);
      if (v) return v;
    }
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

/**
 * Adapt a WhatsApp envelope (legacy shape) to the cross-channel envelope.
 *
 * P1-AnyUrl: the TS side previously emitted URLs verbatim while the Python
 * side parses them through `pydantic.AnyUrl`, which normalizes (adds trailing
 * slash to bare hosts, lowercases scheme, etc.). The drift caused round-trip
 * mismatches. We pre-normalize via `new URL(url).toString()` so the string
 * the Python side validates is identical to what we serialized.
 */
export function whatsappToEnvelope(m: InboundMessageEnvelope): InboundEnvelope | null {
  const raw = extractUrlFromWaMessage(m.raw);
  if (!raw) return null;
  const url = _validUrl(raw);
  if (!url) return null;
  return {
    message_id: m.messageId,
    url,
    source: "whatsapp",
    captured_at: m.receivedAt,
    raw_payload: m.raw,
  };
}
