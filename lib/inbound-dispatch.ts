/**
 * Inbound message dispatcher — clean handoff interface for component #2 (URL dispatcher).
 *
 * Day-1 implementation: append to a local JSONL log file.
 * Component #2 will swap the body of `dispatchInboundMessage` to enqueue/parse URLs without
 * changing the call site in the webhook route.
 */
import fs from "node:fs/promises";
import path from "node:path";

export interface InboundMessageEnvelope {
  /** ISO timestamp when our webhook received it. */
  receivedAt: string;
  /** Meta's phone_number_id this message was sent to. */
  phoneNumberId: string;
  /** Sender's WhatsApp ID (E.164 without `+`). */
  from: string;
  /** Message id from Meta — use for idempotency. */
  messageId: string;
  /** "text" | "image" | "audio" | "video" | "document" | "interactive" | ... */
  type: string;
  /** Raw `messages[i]` object straight from Meta. Untouched. */
  raw: Record<string, unknown>;
}

const LOG_DIR = path.resolve(process.cwd(), "logs");
const LOG_FILE = path.join(LOG_DIR, "inbound.jsonl");

export async function dispatchInboundMessage(env: InboundMessageEnvelope): Promise<void> {
  // Component #2 swap point: parse env.raw for URLs and enqueue. For now, just log.
  await fs.mkdir(LOG_DIR, { recursive: true });
  await fs.appendFile(LOG_FILE, JSON.stringify(env) + "\n", "utf8");
}

/**
 * Extract `messages[]` from a Meta webhook payload, flattening across entry/changes.
 * Status updates (delivery/read receipts) are intentionally skipped — they live in
 * `value.statuses`, not `value.messages`.
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
