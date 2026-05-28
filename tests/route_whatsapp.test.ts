/**
 * Integration tests for `app/api/webhooks/whatsapp/route.ts`.
 *
 * The route fans inbound Meta webhook payloads into the Upstash Redis stream
 * via `dispatchInboundMessage`. We mock `@upstash/redis` so the test never
 * touches the network and we can assert exact xadd invocations.
 *
 * Covered:
 *  - POST with valid HMAC + a text message containing a URL → 200, xadd called
 *  - POST with bad HMAC → 401, no dispatch
 *  - POST with no `x-hub-signature-256` header → 401, no dispatch
 *  - POST with valid HMAC but empty `messages` → 200, no dispatch
 *  - GET handshake with matching verify_token → echoes challenge
 *  - GET handshake with mismatching verify_token → 403
 *  - GET handshake with length-mismatched token → 403 (constant-time guard)
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import crypto from "node:crypto";

// --- Mock @upstash/redis BEFORE importing anything that uses it ---
const xaddMock = vi.fn().mockResolvedValue("1-0");

vi.mock("@upstash/redis", () => {
  return {
    Redis: class {
      xadd = xaddMock;
    },
  };
});

const APP_SECRET = "test-app-secret";
const VERIFY_TOKEN = "test-verify-token-very-long";

function sign(body: string): string {
  return "sha256=" + crypto.createHmac("sha256", APP_SECRET).update(body).digest("hex");
}

function makeReq(init: {
  method: "GET" | "POST";
  url: string;
  body?: string;
  headers?: Record<string, string>;
}) {
  const headers = new Headers(init.headers ?? {});
  return new Request(init.url, {
    method: init.method,
    body: init.body,
    headers,
  });
}

let GET: any;
let POST: any;

beforeEach(async () => {
  vi.resetModules();
  xaddMock.mockClear();
  process.env.WA_APP_SECRET = APP_SECRET;
  process.env.WA_VERIFY_TOKEN = VERIFY_TOKEN;
  process.env.UPSTASH_REDIS_REST_URL = "https://example-upstash";
  process.env.UPSTASH_REDIS_REST_TOKEN = "test-token";
  // Silence the route's own console output during tests.
  vi.spyOn(console, "log").mockImplementation(() => {});
  vi.spyOn(console, "warn").mockImplementation(() => {});
  vi.spyOn(console, "error").mockImplementation(() => {});

  const mod = await import("@/app/api/webhooks/whatsapp/route");
  GET = mod.GET;
  POST = mod.POST;
});

afterEach(() => {
  vi.restoreAllMocks();
});

// --- POST tests --------------------------------------------------------------

describe("POST /api/webhooks/whatsapp", () => {
  const payloadWithUrl = {
    object: "whatsapp_business_account",
    entry: [
      {
        id: "WABA-1",
        changes: [
          {
            value: {
              metadata: { phone_number_id: "555" },
              messages: [
                {
                  id: "wamid.test1",
                  from: "15551234567",
                  type: "text",
                  text: { body: "Check this https://example.com/article" },
                },
              ],
            },
          },
        ],
      },
    ],
  };

  it("200s and dispatches when HMAC + envelope are valid", async () => {
    const body = JSON.stringify(payloadWithUrl);
    const res = await POST(
      makeReq({
        method: "POST",
        url: "https://app.example/api/webhooks/whatsapp",
        body,
        headers: { "x-hub-signature-256": sign(body), "content-type": "application/json" },
      }),
    );

    expect(res.status).toBe(200);
    expect(xaddMock).toHaveBeenCalledTimes(1);
    const [streamKey, id, payload] = xaddMock.mock.calls[0];
    expect(streamKey).toBe("inbound-stream");
    expect(id).toBe("*");
    expect(payload).toHaveProperty("envelope");
    const env = JSON.parse((payload as { envelope: string }).envelope);
    expect(env.source).toBe("whatsapp");
    expect(env.url).toBe("https://example.com/article");
    expect(env.message_id).toBe("wamid.test1");
  });

  it("401s and does not dispatch when the HMAC is wrong", async () => {
    const body = JSON.stringify(payloadWithUrl);
    const res = await POST(
      makeReq({
        method: "POST",
        url: "https://app.example/api/webhooks/whatsapp",
        body,
        headers: { "x-hub-signature-256": "sha256=" + "0".repeat(64) },
      }),
    );

    expect(res.status).toBe(401);
    expect(xaddMock).not.toHaveBeenCalled();
  });

  it("401s when the x-hub-signature-256 header is missing entirely", async () => {
    const body = JSON.stringify(payloadWithUrl);
    const res = await POST(
      makeReq({
        method: "POST",
        url: "https://app.example/api/webhooks/whatsapp",
        body,
        // no signature header
      }),
    );

    expect(res.status).toBe(401);
    expect(xaddMock).not.toHaveBeenCalled();
  });

  it("200s but does not dispatch when messages[] is empty (status-only update)", async () => {
    const statusOnly = {
      object: "whatsapp_business_account",
      entry: [
        {
          changes: [
            {
              value: { metadata: { phone_number_id: "555" }, statuses: [{ id: "x" }] },
            },
          ],
        },
      ],
    };
    const body = JSON.stringify(statusOnly);
    const res = await POST(
      makeReq({
        method: "POST",
        url: "https://app.example/api/webhooks/whatsapp",
        body,
        headers: { "x-hub-signature-256": sign(body) },
      }),
    );

    expect(res.status).toBe(200);
    expect(xaddMock).not.toHaveBeenCalled();
  });
});

// --- GET handshake -----------------------------------------------------------

describe("GET /api/webhooks/whatsapp (verify handshake)", () => {
  it("echoes hub.challenge when verify_token matches", async () => {
    const res = await GET(
      makeReq({
        method: "GET",
        url:
          "https://app.example/api/webhooks/whatsapp" +
          `?hub.mode=subscribe&hub.verify_token=${encodeURIComponent(VERIFY_TOKEN)}&hub.challenge=42`,
      }),
    );
    expect(res.status).toBe(200);
    expect(await res.text()).toBe("42");
    expect(res.headers.get("content-type")).toContain("text/plain");
  });

  it("returns 403 when verify_token mismatches (constant-time compare path)", async () => {
    // Same length to ensure we actually exercise timingSafeEqual rather than
    // short-circuiting on length.
    const wrong = "x".repeat(VERIFY_TOKEN.length);
    const res = await GET(
      makeReq({
        method: "GET",
        url:
          "https://app.example/api/webhooks/whatsapp" +
          `?hub.mode=subscribe&hub.verify_token=${wrong}&hub.challenge=42`,
      }),
    );
    expect(res.status).toBe(403);
  });

  it("returns 403 when verify_token has a different length (length-mismatch short-circuit)", async () => {
    const res = await GET(
      makeReq({
        method: "GET",
        url:
          "https://app.example/api/webhooks/whatsapp" +
          `?hub.mode=subscribe&hub.verify_token=short&hub.challenge=42`,
      }),
    );
    expect(res.status).toBe(403);
  });

  it("returns 403 when hub.mode is missing or not 'subscribe'", async () => {
    const res = await GET(
      makeReq({
        method: "GET",
        url:
          "https://app.example/api/webhooks/whatsapp" +
          `?hub.verify_token=${encodeURIComponent(VERIFY_TOKEN)}&hub.challenge=42`,
      }),
    );
    expect(res.status).toBe(403);
  });
});

// --- Interactive reply (digest reaction) tests ---

describe("POST /api/webhooks/whatsapp — interactive digest reactions", () => {
  const makeInteractivePayload = (rowId: string) => ({
    object: "whatsapp_business_account",
    entry: [
      {
        id: "WABA-1",
        changes: [
          {
            value: {
              metadata: { phone_number_id: "555" },
              messages: [
                {
                  id: "wamid.interactive1",
                  from: "918595087697",
                  type: "interactive",
                  interactive: {
                    type: "list_reply",
                    list_reply: {
                      id: rowId,
                      title: "👍 Loved it",
                    },
                  },
                },
              ],
            },
          },
        ],
      },
    ],
  });

  it("200s when an interactive list_reply with a digest row ID is received", async () => {
    // Row ID: "sources/web/note.md__up"
    const rowId = "sources/web/note.md__up";
    const body = JSON.stringify(makeInteractivePayload(rowId));
    const res = await POST(
      makeReq({
        method: "POST",
        url: "https://app.example/api/webhooks/whatsapp",
        body,
        headers: { "x-hub-signature-256": sign(body), "content-type": "application/json" },
      }),
    );
    expect(res.status).toBe(200);
  });

  it("forwards digest reactions to xadd so the local Python consumer can write labels", async () => {
    const rowId = "sources/web/note.md__shrug";
    const body = JSON.stringify(makeInteractivePayload(rowId));
    await POST(
      makeReq({
        method: "POST",
        url: "https://app.example/api/webhooks/whatsapp",
        body,
        headers: { "x-hub-signature-256": sign(body) },
      }),
    );
    // digest reactions must now be forwarded to the redis stream (Python consumer writes labels)
    expect(xaddMock).toHaveBeenCalledTimes(1);
  });

  it("forwards non-digest interactive messages to xadd normally", async () => {
    // A button_reply without the __ separator pattern falls through to normal dispatch
    const payload = {
      object: "whatsapp_business_account",
      entry: [
        {
          id: "WABA-1",
          changes: [
            {
              value: {
                metadata: { phone_number_id: "555" },
                messages: [
                  {
                    id: "wamid.interactive2",
                    from: "918595087697",
                    type: "interactive",
                    interactive: {
                      type: "button_reply",
                      button_reply: {
                        id: "some-regular-button-no-double-underscore",
                        title: "Yes",
                      },
                    },
                  },
                ],
              },
            },
          ],
        },
      ],
    };
    const body = JSON.stringify(payload);
    const res = await POST(
      makeReq({
        method: "POST",
        url: "https://app.example/api/webhooks/whatsapp",
        body,
        headers: { "x-hub-signature-256": sign(body) },
      }),
    );
    expect(res.status).toBe(200);
    // Non-digest interactive should be dispatched to stream
    expect(xaddMock).toHaveBeenCalledTimes(1);
  });
});
