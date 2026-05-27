/**
 * Tests for `lib/wa-signature.ts` — Meta's x-hub-signature-256 HMAC verifier.
 *
 * Security-critical: a bug here = anyone on the internet can POST forged
 * inbound messages into the dispatch stream. We pin:
 *   - happy path (valid sig → true)
 *   - tampered sig / wrong secret / different body → false
 *   - missing or malformed header → false (no crash)
 *   - length-mismatched signature → false BEFORE timingSafeEqual is reached
 *   - case variation in hex → handled (timingSafeEqual is byte-exact, so an
 *     uppercase-hex signature should NOT validate against lowercase-hex
 *     output — Meta always sends lowercase; this locks that contract in)
 *   - empty body → still produces a deterministic, non-throwing answer
 */
import { describe, expect, it } from "vitest";
import crypto from "node:crypto";
import { verifyMetaSignature } from "@/lib/wa-signature";

const SECRET = "test-app-secret-do-not-use-in-prod";

function sign(body: string, secret = SECRET): string {
  return "sha256=" + crypto.createHmac("sha256", secret).update(body).digest("hex");
}

describe("verifyMetaSignature", () => {
  it("returns true for a valid signature over the exact body", () => {
    const body = JSON.stringify({ object: "whatsapp_business_account", entry: [] });
    expect(verifyMetaSignature(body, sign(body), SECRET)).toBe(true);
  });

  it("returns false for a tampered body", () => {
    const body = '{"x":1}';
    const sig = sign(body);
    expect(verifyMetaSignature('{"x":2}', sig, SECRET)).toBe(false);
  });

  it("returns false when the wrong secret is used to verify", () => {
    const body = "hello";
    const sig = sign(body, "other-secret");
    expect(verifyMetaSignature(body, sig, SECRET)).toBe(false);
  });

  it("returns false on missing header", () => {
    expect(verifyMetaSignature("anything", null, SECRET)).toBe(false);
  });

  it("returns false on empty appSecret (defence-in-depth: never accept blank)", () => {
    const body = "hello";
    expect(verifyMetaSignature(body, sign(body), "")).toBe(false);
  });

  it("short-circuits cleanly on a length-mismatched signature header", () => {
    // 'sha256=' + 1 hex char — wildly wrong length.
    expect(verifyMetaSignature("hello", "sha256=a", SECRET)).toBe(false);
  });

  it("does not crash on garbage in the header", () => {
    // Same-length string but not hex — must just return false, never throw.
    const valid = sign("hello");
    const garbage = "X".repeat(valid.length);
    expect(verifyMetaSignature("hello", garbage, SECRET)).toBe(false);
  });

  it("rejects an uppercase-hex signature (Meta sends lowercase; lock the contract)", () => {
    const body = "hello";
    const sig = sign(body); // lowercase hex
    const upper = sig.replace(/sha256=/, "sha256=").toUpperCase().replace("SHA256=", "sha256=");
    expect(verifyMetaSignature(body, upper, SECRET)).toBe(false);
  });

  it("handles an empty body deterministically (HMAC over empty string)", () => {
    expect(verifyMetaSignature("", sign(""), SECRET)).toBe(true);
    expect(verifyMetaSignature("", "sha256=deadbeef", SECRET)).toBe(false);
  });

  it("accepts a Buffer body that matches the signed bytes", () => {
    const body = Buffer.from("raw bytes \xff\x00\x01");
    const sig =
      "sha256=" + crypto.createHmac("sha256", SECRET).update(body).digest("hex");
    expect(verifyMetaSignature(body, sig, SECRET)).toBe(true);
  });
});
