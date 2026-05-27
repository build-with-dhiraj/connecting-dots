import crypto from "node:crypto";

/**
 * Verify Meta's x-hub-signature-256 header against the raw request body.
 * Meta signs the exact bytes of the POST body using HMAC-SHA256 with WA_APP_SECRET.
 *
 * IMPORTANT: pass the raw body string (or Buffer). Re-serializing JSON will break the HMAC.
 */
export function verifyMetaSignature(rawBody: string | Buffer, headerValue: string | null, appSecret: string): boolean {
  if (!headerValue || !appSecret) return false;
  const expected =
    "sha256=" +
    crypto.createHmac("sha256", appSecret).update(rawBody).digest("hex");
  const a = Buffer.from(headerValue);
  const b = Buffer.from(expected);
  if (a.length !== b.length) return false;
  try {
    return crypto.timingSafeEqual(a, b);
  } catch {
    return false;
  }
}
