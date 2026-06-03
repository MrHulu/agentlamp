/**
 * index.ts — the AgentLamp relay Worker (edge entry).
 *
 * Routes:
 *   POST /api/v1/collectors/:id/events  — signed sanitized ingest (relay only).
 *     Charset gate on :id BEFORE anything (canonical-string ambiguity); edge rate-limit
 *     (60/min per collector); forward to the RelayDO singleton, which does HMAC verify +
 *     replay + idempotency + the validate-only gate (I1) + apply.
 *   GET  /api/v1/device/:id/frame       — read-only frame pull.
 *     Bearer extraction; edge rate-limit (20/min per device); forward to the DO for token
 *     verify + frame build.
 *   GET  /api/v1/device/:id/cacerts     — pinned ROOT CA bundle refresh (firmware relay.h).
 *     Bearer-authed EXACTLY like /frame (revocation applies, I4); forwards to the DO which
 *     verifies the token then returns the current pinned-CA PEM bundle (CA_BUNDLE var → KV
 *     CONFIG["ca_bundle"] → embedded default). Lets a CA rotation reach the device with no
 *     reflash. Same edge rate-limit bucket as /frame.
 *
 * 🚨 BUILD-SPEC I4: the DO owns ALL revocation-critical/strongly-consistent state. The Worker
 *   is stateless except a best-effort edge rate-limit counter (eventually consistent — a
 *   bypass only reaches the DO, which re-rate-limits / re-verifies). Auth errors are uniform
 *   (401/403/404) so the edge is not an oracle.
 * 🚨 BUILD-SPEC I3: no host/account hardcodes — RELAY_HOST + secrets come from env/vars.
 */

export { RelayDO } from "./relay_do";

const COLLECTOR_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;
const DEVICE_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;

interface Env {
  RELAY: DurableObjectNamespace;
  CONFIG?: KVNamespace;
  RELAY_HOST?: string;
  DEVICE_RATE_PER_MIN?: string;
  COLLECTOR_RATE_PER_MIN?: string;
  // Optional operator-set pinned CA bundle (read by the DO; documented default ships in ca.ts).
  CA_BUNDLE?: string;
  // Bearer for the in-Worker /admin gate (revoke routes). If UNSET, /admin is fail-CLOSED (403)
  // — the routes never open. Cloudflare Access can ALSO gate /admin at the edge (defense in
  // depth, no ESP32 in this path); this bearer check is the in-Worker gate. Set via
  // `wrangler secret put AGENTLAMP_ADMIN_TOKEN`.
  AGENTLAMP_ADMIN_TOKEN?: string;
}

// Admin-token rate limit: the /admin surface is human-driven (revoke a kid/device), so a tight
// bucket both throttles a leaked-token brute force and matches the device/collector pattern.
// Per-IP cap (a single source); a separate aggregate cap bounds a distributed wrong-token storm.
const ADMIN_RATE_PER_MIN = 30;
const ADMIN_GLOBAL_RATE_PER_MIN = 120;

/**
 * Constant-time string compare (length-independent in the equality of the COMPARED bytes — it
 * still leaks the *length* difference, which is acceptable for a high-entropy bearer). Avoids the
 * early-return timing oracle of `===`. We compare UTF-8 byte views via WebCrypto-free XOR-accumulate
 * (no async, runs at the edge). Both sides are hashed to a fixed 32-byte digest first so the loop
 * length never depends on the secret length.
 */
async function constantTimeEqual(a: string, b: string): Promise<boolean> {
  const enc = new TextEncoder();
  const [da, db] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(a)),
    crypto.subtle.digest("SHA-256", enc.encode(b)),
  ]);
  const ua = new Uint8Array(da);
  const ub = new Uint8Array(db);
  let diff = 0;
  for (let i = 0; i < ua.length; i++) diff |= ua[i]! ^ ub[i]!;
  return diff === 0;
}

// Best-effort in-isolate edge rate limiter (per isolate; the DO re-checks authoritative state).
// Eventually consistent by design — NOT a revocation/security control (I4 keeps those in the DO).
const rateBuckets = new Map<string, { windowStart: number; count: number }>();
function edgeRateLimit(key: string, perMin: number, now: number): boolean {
  const bucket = rateBuckets.get(key);
  const windowStart = Math.floor(now / 60) * 60;
  if (bucket === undefined || bucket.windowStart !== windowStart) {
    rateBuckets.set(key, { windowStart, count: 1 });
    return true;
  }
  if (bucket.count >= perMin) return false;
  bucket.count += 1;
  return true;
}

function relayStub(env: Env): DurableObjectStub {
  // Singleton: one named instance "relay" owns the whole materialized state machine (I4).
  const id = env.RELAY.idFromName("relay");
  return env.RELAY.get(id);
}

function rateLimited(retryAfter = 60): Response {
  return Response.json(
    { ok: false, reason: "rate_limited", error: "rate_limited", retry: true },
    { status: 429, headers: { "Retry-After": String(retryAfter) } },
  );
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const now = Date.now() / 1000;
    const parts = url.pathname.split("/").filter(Boolean); // e.g. ["api","v1","collectors","ID","events"]

    // POST /api/v1/collectors/:id/events
    if (
      request.method === "POST" &&
      parts.length === 5 &&
      parts[0] === "api" &&
      parts[1] === "v1" &&
      parts[2] === "collectors" &&
      parts[4] === "events"
    ) {
      const collectorId = decodeURIComponent(parts[3]!);
      // Charset gate BEFORE signature verification (collector_ingest_api.md).
      if (!COLLECTOR_ID_RE.test(collectorId)) {
        return Response.json({ ok: false, reason: "bad_collector_id", server_time: Math.trunc(now) }, { status: 400 });
      }
      const perMin = Number.parseInt(env.COLLECTOR_RATE_PER_MIN ?? "60", 10) || 60;
      if (!edgeRateLimit(`c:${collectorId}`, perMin, now)) return rateLimited();

      const raw = await request.arrayBuffer();
      const stub = relayStub(env);
      // Forward to the DO with the original auth headers + the canonical path it must sign over.
      const headers = new Headers();
      for (const h of ["x-aco-key-id", "x-aco-timestamp", "x-aco-nonce", "x-aco-payload-sha256", "x-aco-signature", "idempotency-key"]) {
        const val = request.headers.get(h);
        if (val !== null) headers.set(h, val);
      }
      headers.set("x-aco-collector-id", collectorId);
      headers.set("x-aco-path", `/api/v1/collectors/${collectorId}/events`);
      headers.set("content-type", "application/json");
      return stub.fetch("https://do/do/ingest", { method: "POST", body: raw, headers });
    }

    // GET /api/v1/device/:id/frame
    if (
      request.method === "GET" &&
      parts.length === 5 &&
      parts[0] === "api" &&
      parts[1] === "v1" &&
      parts[2] === "device" &&
      parts[4] === "frame"
    ) {
      const deviceId = decodeURIComponent(parts[3]!);
      if (!DEVICE_ID_RE.test(deviceId)) {
        return Response.json({ error: "unknown_device", retry: false }, { status: 404 });
      }
      const perMin = Number.parseInt(env.DEVICE_RATE_PER_MIN ?? "20", 10) || 20;
      if (!edgeRateLimit(`d:${deviceId}`, perMin, now)) return rateLimited();

      const auth = request.headers.get("authorization") ?? "";
      const token = auth.toLowerCase().startsWith("bearer ") ? auth.slice(7).trim() : "";
      const schemaVersion = request.headers.get("x-frame-schema-version") ?? "1";

      const stub = relayStub(env);
      const headers = new Headers();
      headers.set("x-device-token", token);
      const doUrl = new URL("https://do/do/frame");
      doUrl.searchParams.set("device_id", deviceId);
      doUrl.searchParams.set("schema_version", schemaVersion);
      return stub.fetch(doUrl.toString(), { method: "GET", headers });
    }

    // GET /api/v1/device/:id/cacerts — pinned ROOT CA bundle refresh (firmware/src/relay.h).
    // Bearer-authed identically to /frame (the DO verifies the token + applies revocation, I4);
    // returns the current pinned-CA PEM bundle so a CA rotation reaches the device with no
    // reflash. Shares the device rate-limit bucket — the firmware only calls this on TLS failure.
    if (
      request.method === "GET" &&
      parts.length === 5 &&
      parts[0] === "api" &&
      parts[1] === "v1" &&
      parts[2] === "device" &&
      parts[4] === "cacerts"
    ) {
      const deviceId = decodeURIComponent(parts[3]!);
      if (!DEVICE_ID_RE.test(deviceId)) {
        return Response.json({ error: "unknown_device", retry: false }, { status: 404 });
      }
      const perMin = Number.parseInt(env.DEVICE_RATE_PER_MIN ?? "20", 10) || 20;
      if (!edgeRateLimit(`d:${deviceId}`, perMin, now)) return rateLimited();

      const auth = request.headers.get("authorization") ?? "";
      const token = auth.toLowerCase().startsWith("bearer ") ? auth.slice(7).trim() : "";

      const stub = relayStub(env);
      const headers = new Headers();
      headers.set("x-device-token", token);
      const doUrl = new URL("https://do/do/cacerts");
      doUrl.searchParams.set("device_id", deviceId);
      return stub.fetch(doUrl.toString(), { method: "GET", headers });
    }

    // POST /admin/collectors/:kid/revoke   — revoke a collector kid (strongly-consistent, I4).
    // POST /admin/devices/:id/revoke        — revoke a device token (strongly-consistent, I4).
    // POST /admin/collectors/:kid/enroll    — runtime-enroll a collector kid (I5; body = secret).
    // POST /admin/devices/:id/enroll        — runtime-enroll a device token (I5; body = token).
    //
    // 🚨 AUTH MODEL (build-spec §Auth model): the in-Worker gate is a CONSTANT-TIME bearer check
    //   against AGENTLAMP_ADMIN_TOKEN. If that secret is UNSET the route is FAIL-CLOSED (403) — it
    //   never opens. Cloudflare Access can ALSO gate /admin at the edge (MFA/TOTP, defense in
    //   depth, no ESP32 in this path); the bearer here is the in-Worker gate, not a replacement.
    //   Uniform errors: 401 missing/bad token · 403 disabled-or-forbidden · 200 on success.
    //   The enroll routes share the IDENTICAL gate (I5: "switch computer in one line" is the
    //   headline — adding a computer needs no `wrangler deploy`, just one authed POST here).
    if (request.method === "POST" && parts[0] === "admin") {
      // collectors/:kid/{revoke,enroll}  ·  devices/:id/{revoke,enroll}  (exactly 4 segments).
      const action = parts.length === 4 ? parts[3] : "";
      const isRevoke = action === "revoke";
      const isEnroll = action === "enroll";
      const isCollector = parts.length === 4 && parts[1] === "collectors" && (isRevoke || isEnroll);
      const isDevice = parts.length === 4 && parts[1] === "devices" && (isRevoke || isEnroll);
      if (!isCollector && !isDevice) {
        return Response.json({ error: "not_found", retry: false }, { status: 404 });
      }

      // 🚨 docs/devlog/16 MED: rate-limit BEFORE the token comparison. Previously the bucket was
      //   consumed only AFTER a successful auth, so wrong-token brute-force attempts were NOT
      //   throttled at all (the early 401 returned before the limiter ran). Throttle the surface
      //   FIRST, keyed by cf-connecting-ip (per-source) AND a global admin bucket (aggregate cap),
      //   so a distributed wrong-token storm is also bounded. A legit operator's small burst of
      //   authed ops still fits under both caps; a post-auth limit is kept too (step 5).
      const adminIp = request.headers.get("cf-connecting-ip") ?? "unknown";
      if (!edgeRateLimit(`admin:ip:${adminIp}`, ADMIN_RATE_PER_MIN, now)) return rateLimited();
      if (!edgeRateLimit("admin:global", ADMIN_GLOBAL_RATE_PER_MIN, now)) return rateLimited();

      // 1. Fail-CLOSED if the admin token is unset/empty — the route is NEVER open (403).
      const adminToken = (env.AGENTLAMP_ADMIN_TOKEN ?? "").trim();
      if (adminToken === "") {
        return Response.json({ error: "admin_disabled", retry: false }, { status: 403 });
      }
      // 2. Bearer presence (uniform 401 for missing/malformed — no oracle on which part failed).
      const auth = request.headers.get("authorization") ?? "";
      if (!auth.toLowerCase().startsWith("bearer ")) {
        return Response.json({ error: "admin_unauthorized", retry: false }, { status: 401 });
      }
      const presented = auth.slice(7).trim();
      // 3. Constant-time compare (no early-return timing oracle). Wrong token → 401.
      if (presented === "" || !(await constantTimeEqual(presented, adminToken))) {
        return Response.json({ error: "admin_unauthorized", retry: false }, { status: 401 });
      }
      // 4. Post-auth rate-limit too (a leaked-token holder is still bounded to a sane authed rate).
      if (!edgeRateLimit("admin:authed", ADMIN_RATE_PER_MIN, now)) return rateLimited();

      // 5. Charset gate on the path id BEFORE forwarding (canonical-string ambiguity; mirrors the
      //    ingest/frame edge gates). A bad id is a 400, never reaches the DO.
      const targetId = decodeURIComponent(parts[2]!);
      const idRe = isCollector ? COLLECTOR_ID_RE : DEVICE_ID_RE;
      if (!idRe.test(targetId)) {
        return Response.json({ error: "bad_id", retry: false }, { status: 400 });
      }

      // 6. For an enroll, parse the body for the secret/token (the credential being installed). A
      //    missing/empty credential is a 400 — never enroll an empty credential.
      let secret = "";
      if (isEnroll) {
        let parsed: Record<string, unknown> = {};
        try {
          parsed = (await request.json()) as Record<string, unknown>;
        } catch {
          return Response.json({ error: "bad_request", retry: false }, { status: 400 });
        }
        secret = String((isCollector ? parsed["secret"] : parsed["token"]) ?? "").trim();
        if (secret === "") {
          return Response.json({ error: "bad_request", retry: false }, { status: 400 });
        }
      }

      // 7. Forward to the DO's authoritative handler (the strongly-consistent registry lives there
      //    — I4/I5). The DO returns { ok, revoked } or { ok, kid|device_id }.
      const stub = relayStub(env);
      let doPath: string;
      let doBody: Record<string, unknown>;
      if (isRevoke) {
        doPath = isCollector ? "https://do/do/admin/revoke-kid" : "https://do/do/admin/revoke-device";
        doBody = isCollector ? { kid: targetId } : { device_id: targetId };
      } else {
        doPath = isCollector ? "https://do/do/admin/enroll-collector" : "https://do/do/admin/enroll-device";
        doBody = isCollector ? { kid: targetId, secret } : { device_id: targetId, token: secret };
      }
      // 🚨 docs/devlog/16 MED (admin replay): forward the freshness headers so the DO can enforce
      //   the ADMIN REQUEST CONTRACT (X-ACO-Timestamp ±300s + single-use X-ACO-Nonce against the
      //   persisted nonce store) — a replayed OLD enroll can't undo a LATER revoke. The bearer above
      //   proves authorization; these prove RECENCY + uniqueness. Missing/stale/duplicate → the DO
      //   rejects (401/409) without mutating state.
      const fwdHeaders: Record<string, string> = { "content-type": "application/json" };
      const adminTs = request.headers.get("x-aco-timestamp");
      const adminNonce = request.headers.get("x-aco-nonce");
      if (adminTs !== null) fwdHeaders["x-aco-timestamp"] = adminTs;
      if (adminNonce !== null) fwdHeaders["x-aco-nonce"] = adminNonce;
      return stub.fetch(doPath, {
        method: "POST",
        body: JSON.stringify(doBody),
        headers: fwdHeaders,
      });
    }

    // Health (no secret, no state).
    if (request.method === "GET" && url.pathname === "/healthz") {
      return Response.json({ ok: true, service: "agentlamp-relay", v: 1 });
    }

    return Response.json({ error: "not_found", retry: false }, { status: 404 });
  },
};
