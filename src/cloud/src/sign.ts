/**
 * sign.ts — collector-ingest HMAC, the frozen wire spec.
 *
 * 🚨 BUILD-SPEC I2: `canonicalString` + `verify` reproduce the byte construction in
 * server/agentlamp_server/ingest.py (`canonical_string` + `sign`) EXACTLY, verified against
 * tests/fixtures/parity/hmac_vectors.json. Uses WebCrypto `crypto.subtle` (available in
 * workerd) — no Node `crypto` dependency in the Worker path.
 *
 * canonical_string(method, path, kid, ts, nonce, sha256):
 *   "v1\n" + method + "\n" + path + "\n" + kid + "\n" + ts + "\n" + nonce + "\n" + sha256
 *   (single 0x0A separators, NO trailing newline; every field charset-restricted upstream so
 *    none can contain a newline — the parse is unambiguous).
 */

// Charset gates (collector_ingest_api.md → Limits / Replay; mirror ingest.py regexes).
export const COLLECTOR_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;
export const KID_RE = /^[A-Za-z0-9_-]{1,64}$/;
export const NONCE_RE = /^[0-9a-f]{16,128}$/; // lowercase hex, 64..512 bits
export const SHA256_HEX_RE = /^[0-9a-f]{64}$/;

export const TIMESTAMP_WINDOW_S = 300; // ±300 s
export const NONCE_TTL_S = 720; // > window + buffer
export const IDEMPOTENCY_TTL_S = 7 * 24 * 3600; // 7 days
export const MAX_EVENTS_PER_REQUEST = 50;
export const MAX_BODY_BYTES = 100 * 1024; // 100 KB
export const SUPPORTED_SCHEMA_VERSION = 1;

const HEX = "0123456789abcdef";

function toHex(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let out = "";
  for (let i = 0; i < bytes.length; i++) {
    const b = bytes[i]!;
    out += HEX[b >> 4]! + HEX[b & 0x0f]!;
  }
  return out;
}

/** SHA-256 hex of the EXACT raw request body bytes (matches payload_sha256_hex). */
export async function payloadSha256Hex(raw: Uint8Array): Promise<string> {
  // Copy into a fresh ArrayBuffer so a Uint8Array view with a non-zero byteOffset
  // (or a SharedArrayBuffer backing) is always hashed over exactly its own bytes.
  const copy = raw.slice();
  const digest = await crypto.subtle.digest("SHA-256", copy);
  return toHex(digest);
}

/** The authoritative canonical string (byte-for-byte ingest.py.canonical_string). */
export function canonicalString(
  method: string,
  path: string,
  kid: string,
  timestamp: string,
  nonce: string,
  payloadSha256: string,
): string {
  return ["v1", method, path, kid, timestamp, nonce, payloadSha256].join("\n");
}

/** hex(HMAC-SHA256(secret, utf8(canonical))) — matches ingest.py.sign. */
export async function sign(secret: Uint8Array, canonical: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    secret.slice(),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(canonical));
  return toHex(mac);
}

/** Constant-time hex-string compare (both inputs are fixed-length lowercase hex). */
export function timingSafeEqualHex(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/**
 * Recompute the expected signature for (method, path, kid, ts, nonce, sha) under `secret`
 * and constant-time compare to the provided lowercased signature. Pure crypto — the caller
 * (DO) owns charset / timestamp-window / nonce-replay / kid-lookup.
 */
export async function verify(
  secret: Uint8Array,
  args: { method: string; path: string; kid: string; timestamp: string; nonce: string; payloadSha256: string },
  signature: string,
): Promise<boolean> {
  const canonical = canonicalString(
    args.method,
    args.path,
    args.kid,
    args.timestamp,
    args.nonce,
    args.payloadSha256,
  );
  const expected = await sign(secret, canonical);
  return timingSafeEqualHex(expected, (signature || "").toLowerCase());
}

export function utf8(s: string): Uint8Array {
  return new TextEncoder().encode(s);
}
