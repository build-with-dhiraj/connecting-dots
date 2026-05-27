/**
 * Unit tests for `whatsappToEnvelope` + `dispatchInboundMessage`.
 *
 * The original `whatsappToEnvelope` returned null for any WhatsApp message
 * without a URL — silently dropping text/image/audio/video/document payloads.
 * The May 2026 fix makes the function emit an envelope for every supported
 * message type so the share-to-WA pivot (kernel component #5) actually
 * receives all the user's inbound content.
 *
 * These tests cover one case per message type plus the `null` return for
 * stickers, and an end-to-end check that `dispatchInboundMessage` performs
 * the correct XADD shape for a non-URL envelope.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mock @upstash/redis BEFORE importing anything that uses it.
const xaddMock = vi.fn().mockResolvedValue("1-0");

vi.mock("@upstash/redis", () => {
  return {
    Redis: class {
      xadd = xaddMock;
    },
  };
});

let whatsappToEnvelope: typeof import("@/lib/inbound-dispatch").whatsappToEnvelope;
let dispatchInboundMessage: typeof import("@/lib/inbound-dispatch").dispatchInboundMessage;
let extractInboundMessages: typeof import("@/lib/inbound-dispatch").extractInboundMessages;

beforeEach(async () => {
  vi.resetModules();
  xaddMock.mockClear();
  process.env.UPSTASH_REDIS_REST_URL = "https://example-upstash";
  process.env.UPSTASH_REDIS_REST_TOKEN = "test-token";

  const mod = await import("@/lib/inbound-dispatch");
  whatsappToEnvelope = mod.whatsappToEnvelope;
  dispatchInboundMessage = mod.dispatchInboundMessage;
  extractInboundMessages = mod.extractInboundMessages;
});

afterEach(() => {
  vi.restoreAllMocks();
});

function baseMessage(type: string, raw: Record<string, unknown>) {
  return {
    receivedAt: "2026-05-27T12:00:00.000Z",
    phoneNumberId: "555",
    from: "15551234567",
    messageId: `wamid.${type}-test`,
    type,
    raw: { type, ...raw },
  };
}

// --- whatsappToEnvelope: shape per message_type ------------------------------

describe("whatsappToEnvelope by message_type", () => {
  it("text with URL -> message_type='url' + url field", () => {
    const m = baseMessage("text", {
      text: { body: "see this https://example.com/article it's good" },
    });
    const env = whatsappToEnvelope(m);
    expect(env).not.toBeNull();
    expect(env!.message_type).toBe("url");
    expect(env!.url).toBe("https://example.com/article");
    expect(env!.message_id).toBe("wamid.text-test");
    expect(env!.source).toBe("whatsapp");
  });

  it("text without URL -> message_type='text' + text field, no url", () => {
    const m = baseMessage("text", { text: { body: "just a plain note" } });
    const env = whatsappToEnvelope(m);
    expect(env).not.toBeNull();
    expect(env!.message_type).toBe("text");
    expect(env!.text).toBe("just a plain note");
    expect(env!.url).toBeUndefined();
  });

  it("image -> message_type='image' + media_id + mime_type + caption", () => {
    const m = baseMessage("image", {
      image: { id: "meta-img-xyz", mime_type: "image/jpeg", caption: "my receipt" },
    });
    const env = whatsappToEnvelope(m);
    expect(env).not.toBeNull();
    expect(env!.message_type).toBe("image");
    expect(env!.media_id).toBe("meta-img-xyz");
    expect(env!.media_mime_type).toBe("image/jpeg");
    expect(env!.text).toBe("my receipt");
    expect(env!.url).toBeUndefined();
  });

  it("audio -> message_type='audio' + media_id + mime_type", () => {
    const m = baseMessage("audio", {
      audio: { id: "meta-audio-abc", mime_type: "audio/ogg" },
    });
    const env = whatsappToEnvelope(m);
    expect(env).not.toBeNull();
    expect(env!.message_type).toBe("audio");
    expect(env!.media_id).toBe("meta-audio-abc");
    expect(env!.media_mime_type).toBe("audio/ogg");
    expect(env!.text).toBeUndefined();
  });

  it("video -> message_type='video' + media_id + mime_type + optional caption", () => {
    const m = baseMessage("video", {
      video: { id: "meta-vid-1", mime_type: "video/mp4", caption: "look" },
    });
    const env = whatsappToEnvelope(m);
    expect(env).not.toBeNull();
    expect(env!.message_type).toBe("video");
    expect(env!.media_id).toBe("meta-vid-1");
    expect(env!.media_mime_type).toBe("video/mp4");
    expect(env!.text).toBe("look");
  });

  it("document -> message_type='document' + media_id + filename + mime_type", () => {
    const m = baseMessage("document", {
      document: {
        id: "meta-doc-pdf",
        mime_type: "application/pdf",
        filename: "receipt.pdf",
      },
    });
    const env = whatsappToEnvelope(m);
    expect(env).not.toBeNull();
    expect(env!.message_type).toBe("document");
    expect(env!.media_id).toBe("meta-doc-pdf");
    expect(env!.media_mime_type).toBe("application/pdf");
    expect(env!.media_filename).toBe("receipt.pdf");
  });

  it("location -> message_type='location' + text='<lat>,<lon>'", () => {
    const m = baseMessage("location", {
      location: { latitude: 37.7749, longitude: -122.4194, address: "SF" },
    });
    const env = whatsappToEnvelope(m);
    expect(env).not.toBeNull();
    expect(env!.message_type).toBe("location");
    expect(env!.text).toContain("37.7749,-122.4194");
    expect(env!.text).toContain("SF");
  });

  it("sticker -> null (skipped intentionally)", () => {
    const m = baseMessage("sticker", { sticker: { id: "meta-stk-1" } });
    expect(whatsappToEnvelope(m)).toBeNull();
  });

  it("interactive -> message_type='interactive', raw_payload preserved", () => {
    const m = baseMessage("interactive", {
      interactive: { type: "button_reply", button_reply: { id: "b1", title: "Yes" } },
    });
    const env = whatsappToEnvelope(m);
    expect(env).not.toBeNull();
    expect(env!.message_type).toBe("interactive");
    expect(env!.url).toBeUndefined();
    expect(env!.raw_payload).toHaveProperty("interactive");
  });

  it("unknown future type -> message_type='unknown'", () => {
    const m = baseMessage("brand_new_type_2030", { brand_new_type_2030: { foo: "bar" } });
    const env = whatsappToEnvelope(m);
    expect(env).not.toBeNull();
    expect(env!.message_type).toBe("unknown");
  });
});

// --- dispatchInboundMessage XADD payload shape -------------------------------

describe("dispatchInboundMessage xadd payload", () => {
  it("xadds a text envelope with no url field", async () => {
    const m = baseMessage("text", { text: { body: "saved this thought" } });
    const env = whatsappToEnvelope(m)!;
    expect(env).not.toBeNull();

    await dispatchInboundMessage(env);

    expect(xaddMock).toHaveBeenCalledTimes(1);
    const [streamKey, id, payload] = xaddMock.mock.calls[0];
    expect(streamKey).toBe("inbound-stream");
    expect(id).toBe("*");
    const parsed = JSON.parse((payload as { envelope: string }).envelope);
    expect(parsed.message_type).toBe("text");
    expect(parsed.text).toBe("saved this thought");
    expect(parsed.url).toBeUndefined();
    expect(parsed.source).toBe("whatsapp");
  });

  it("xadds an image envelope with media_id but no url", async () => {
    const m = baseMessage("image", {
      image: { id: "meta-img-99", mime_type: "image/png" },
    });
    const env = whatsappToEnvelope(m)!;
    await dispatchInboundMessage(env);

    expect(xaddMock).toHaveBeenCalledTimes(1);
    const parsed = JSON.parse(
      (xaddMock.mock.calls[0][2] as { envelope: string }).envelope,
    );
    expect(parsed.message_type).toBe("image");
    expect(parsed.media_id).toBe("meta-img-99");
    expect(parsed.media_mime_type).toBe("image/png");
    expect(parsed.url).toBeUndefined();
  });

  it("rejects an envelope with message_type=url but missing url field", async () => {
    // Force a malformed envelope to make sure validation catches the bad shape
    // before XADD. This is the inverse of the previous bug: we want to fail
    // loudly when a `url`-typed envelope lacks the url field.
    const bad = {
      message_id: "bad-1",
      message_type: "url" as const,
      source: "whatsapp" as const,
      captured_at: "2026-05-27T12:00:00.000Z",
      raw_payload: {},
    };
    await expect(dispatchInboundMessage(bad as never)).rejects.toThrow(/url required/);
    expect(xaddMock).not.toHaveBeenCalled();
  });
});

// --- extractInboundMessages still flattens every type ------------------------

describe("extractInboundMessages preserves media-only payloads", () => {
  it("returns image messages alongside text messages", () => {
    const payload = {
      object: "whatsapp_business_account",
      entry: [
        {
          changes: [
            {
              value: {
                metadata: { phone_number_id: "555" },
                messages: [
                  { id: "1", type: "text", text: { body: "hi" }, from: "x" },
                  { id: "2", type: "image", image: { id: "i1" }, from: "x" },
                ],
              },
            },
          ],
        },
      ],
    };
    const out = extractInboundMessages(payload);
    expect(out).toHaveLength(2);
    expect(out[0].type).toBe("text");
    expect(out[1].type).toBe("image");
  });
});
