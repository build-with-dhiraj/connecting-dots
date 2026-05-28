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
  const allowedTypes = [
    "url",
    "text",
    "image",
    "audio",
    "video",
    "document",
    "sticker",
    "location",
    "contacts",
    "interactive",
    "unknown",
  ] as const;
  if (!env || typeof env !== "object") throw new Error("envelope must be an object");
  if (typeof env.message_id !== "string" || env.message_id.length === 0)
    throw new Error("message_id required");
  if (!allowedTypes.includes(env.message_type as (typeof allowedTypes)[number]))
    throw new Error(`message_type must be one of ${allowedTypes.join(",")}`);
  // url is required ONLY when message_type == "url"; for every other type it
  // is optional. This is the schema regression fix: previously the validator
  // hard-required `url` and silently dropped text/media envelopes.
  if (env.message_type === "url") {
    if (typeof env.url !== "string" || env.url.length === 0)
      throw new Error('url required when message_type == "url"');
    if (!/^https?:\/\//.test(env.url))
      throw new Error("url must use http(s) scheme");
  }
  // Media types must carry a non-empty media_id so component #5 can fetch
  // the bytes via Meta's media-download endpoint.
  const mediaTypes = new Set(["image", "audio", "video", "document", "sticker"]);
  if (mediaTypes.has(env.message_type)) {
    if (typeof env.media_id !== "string" || env.media_id.length === 0)
      throw new Error(`media_id required for message_type=${env.message_type}`);
  }
  if (env.message_type === "text") {
    if (typeof env.text !== "string" || env.text.trim().length === 0)
      throw new Error('text required when message_type == "text"');
  }
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
 *
 * Scope (May 2026 fix): previously this returned null for ANY WhatsApp
 * message that didn't contain a URL — meaning plain text, images, audio,
 * video, and documents were silently dropped (webhook returned 200, no
 * XADD). The kernel MVP needs every share-to-WA payload preserved, so we
 * now produce an envelope for every supported `m.raw.type`:
 *   - text       -> url envelope (if a URL is found in the body) else text
 *   - image      -> image envelope (media_id, mime_type, optional caption)
 *   - audio      -> audio envelope (media_id, mime_type)
 *   - video      -> video envelope (media_id, mime_type, optional caption)
 *   - document   -> document envelope (media_id, mime_type, filename, caption)
 *   - location   -> location envelope (text = "<lat>,<lon>" + address if any)
 *   - contacts   -> contacts envelope (raw_payload only — component #5 parses)
 *   - interactive-> interactive envelope (raw_payload only)
 *   - unknown    -> unknown envelope (forwarded for inspection)
 *   - sticker    -> null (skipped — pure expression, no content signal)
 *
 * Media bytes are NOT fetched here. Component #5 will read `media_id` and
 * call Meta's media-download endpoint when it enriches each note.
 */
function _maybeStr(v: unknown): string | undefined {
  return typeof v === "string" && v.length > 0 ? v : undefined;
}

export function whatsappToEnvelope(m: InboundMessageEnvelope): InboundEnvelope | null {
  const raw = m.raw as Record<string, any>;
  const type = (raw?.type ?? "unknown") as string;

  // Sticker: pure expression, skip. Webhook returns 200 with no envelope.
  if (type === "sticker") return null;

  const base = {
    message_id: m.messageId,
    source: "whatsapp" as const,
    captured_at: m.receivedAt,
    raw_payload: m.raw,
  };

  if (type === "text") {
    // Text body may or may not contain a URL. If it does, this is a URL
    // capture (the original share-to-WA happy path). Otherwise it's a
    // standalone text note — still capture it.
    const body = typeof raw?.text?.body === "string" ? (raw.text.body as string) : "";
    const urlMatch = body.match(URL_RE);
    if (urlMatch) {
      const validated = _validUrl(urlMatch[0]);
      if (validated) {
        return { ...base, message_type: "url", url: validated };
      }
    }
    if (body.trim().length === 0) {
      // Empty text body — preserve as "unknown" so we don't violate the
      // text-requires-non-empty-text invariant on the Python side.
      return { ...base, message_type: "unknown" };
    }
    return { ...base, message_type: "text", text: body };
  }

  if (type === "image") {
    return {
      ...base,
      message_type: "image",
      media_id: String(raw?.image?.id ?? ""),
      ...(raw?.image?.mime_type ? { media_mime_type: String(raw.image.mime_type) } : {}),
      ...(_maybeStr(raw?.image?.caption) ? { text: String(raw.image.caption) } : {}),
    };
  }

  if (type === "audio") {
    return {
      ...base,
      message_type: "audio",
      media_id: String(raw?.audio?.id ?? ""),
      ...(raw?.audio?.mime_type ? { media_mime_type: String(raw.audio.mime_type) } : {}),
    };
  }

  if (type === "video") {
    return {
      ...base,
      message_type: "video",
      media_id: String(raw?.video?.id ?? ""),
      ...(raw?.video?.mime_type ? { media_mime_type: String(raw.video.mime_type) } : {}),
      ...(_maybeStr(raw?.video?.caption) ? { text: String(raw.video.caption) } : {}),
    };
  }

  if (type === "document") {
    return {
      ...base,
      message_type: "document",
      media_id: String(raw?.document?.id ?? ""),
      ...(raw?.document?.mime_type
        ? { media_mime_type: String(raw.document.mime_type) }
        : {}),
      ...(_maybeStr(raw?.document?.filename)
        ? { media_filename: String(raw.document.filename) }
        : {}),
      ...(_maybeStr(raw?.document?.caption) ? { text: String(raw.document.caption) } : {}),
    };
  }

  if (type === "location") {
    const lat = raw?.location?.latitude;
    const lon = raw?.location?.longitude;
    const address = _maybeStr(raw?.location?.address);
    const name = _maybeStr(raw?.location?.name);
    let text = "";
    if (typeof lat === "number" && typeof lon === "number") {
      text = `${lat},${lon}`;
    } else if (lat != null && lon != null) {
      text = `${lat},${lon}`;
    }
    if (name) text = text ? `${name} (${text})` : name;
    if (address) text = text ? `${text} — ${address}` : address;
    return { ...base, message_type: "location", ...(text ? { text } : {}) };
  }

  if (type === "contacts") {
    return { ...base, message_type: type };
  }

  if (type === "interactive") {
    // Extract interactive reply data (button_reply or list_reply)
    const interactiveData = raw?.interactive;
    const interactiveType = interactiveData?.type;
    let interactiveId: string | undefined;
    let interactiveTitle: string | undefined;

    if (interactiveType === "list_reply") {
      interactiveId = _maybeStr(interactiveData?.list_reply?.id);
      interactiveTitle = _maybeStr(interactiveData?.list_reply?.title);
    } else if (interactiveType === "button_reply") {
      interactiveId = _maybeStr(interactiveData?.button_reply?.id);
      interactiveTitle = _maybeStr(interactiveData?.button_reply?.title);
    }

    return {
      ...base,
      message_type: "interactive",
      ...(interactiveId ? { interactive_id: interactiveId } : {}),
      ...(interactiveTitle ? { interactive_title: interactiveTitle } : {}),
    };
  }

  // Anything else — including future Meta types we don't know about yet —
  // gets forwarded as unknown so the raw_payload reaches component #5.
  return { ...base, message_type: "unknown" };
}
