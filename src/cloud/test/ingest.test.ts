/**
 * ingest.test.ts — request-level security acceptance, mirrors server/tests/test_ingest.py.
 *
 * Runs inside workerd via @cloudflare/vitest-pool-workers: `SELF` fetches the real Worker
 * (src/index.ts) which forwards to the real RelayDO (src/relay_do.ts). Covers: valid signed
 * ingest, bad sig 401, stale ts 401 (+ server_time), reused nonce 409, payload hash mismatch
 * 400, revoked/unknown kid 403, batch too large 413, body too large 413, idempotent retry,
 * poison-per-event (one leak rejected in results[] while clean events apply — HTTP 200).
 *
 * The test signs requests with WebCrypto exactly as the firmware/collector would, using the
 * test key wired in vitest.config.ts (k1 / "test-collector-secret" — matches the hmac vectors).
 *
 * If the workerd runtime is unavailable (offline sandbox can't download it), this whole file
 * is skipped with a clear message; the load-bearing I2 parity assertions (sign/validate/frame)
 * still run in the Node "parity" project.
 */
import { SELF, env, createExecutionContext, waitOnExecutionContext, runInDurableObject } from "cloudflare:test";
import { beforeAll, describe, expect, it } from "vitest";
import worker from "../src/index";

const KID = "k1";
const SECRET = "test-collector-secret";
const CID = "collector-mac-main";
const BASE = "https://relay.example.invalid";

const enc = new TextEncoder();

function hex(buf: ArrayBuffer): string {
  const b = new Uint8Array(buf);
  let s = "";
  for (let i = 0; i < b.length; i++) s += b[i]!.toString(16).padStart(2, "0");
  return s;
}

async function sha256Hex(s: string): Promise<string> {
  return hex(await crypto.subtle.digest("SHA-256", enc.encode(s)));
}

async function hmacHex(secret: string, msg: string): Promise<string> {
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return hex(await crypto.subtle.sign("HMAC", key, enc.encode(msg)));
}

function body(events?: unknown[]): Record<string, unknown> {
  return {
    schema_version: 1,
    collector_id: CID,
    sent_at: 1_780_000_000,
    events: events ?? [
      {
        event_id: "evt_1",
        event_type: "session.upsert",
        provider: "claude",
        account_alias: "main",
        payload: { session_id: "hmac:abc123", project_alias: "project-a", status: "CODING", model: "claude", task_label: "implementing" },
      },
    ],
  };
}

interface PostOpts {
  kid?: string;
  secret?: string;
  ts?: number;
  nonce?: string;
  idem?: string;
  breakSig?: boolean;
  breakHash?: boolean;
  cid?: string;
}

async function post(bodyObj: unknown, opts: PostOpts = {}): Promise<Response> {
  const kid = opts.kid ?? KID;
  const secret = opts.secret ?? SECRET;
  const cid = opts.cid ?? CID;
  // A fresh, recent timestamp keeps the request within the ±300 s window against the DO's
  // real clock (the Python test froze the clock; here we sign against "now").
  const ts = opts.ts ?? Math.floor(Date.now() / 1000);
  const nonce = opts.nonce ?? "ab".repeat(16);

  const raw = JSON.stringify(bodyObj);
  let sha = await sha256Hex(raw);
  if (opts.breakHash) sha = "0".repeat(64);
  const path = `/api/v1/collectors/${cid}/events`;
  const canon = ["v1", "POST", path, kid, String(ts), nonce, sha].join("\n");
  let sig = await hmacHex(secret, canon);
  if (opts.breakSig) sig = "f".repeat(64);

  const headers: Record<string, string> = {
    "X-ACO-Key-Id": kid,
    "X-ACO-Timestamp": String(ts),
    "X-ACO-Nonce": nonce,
    "X-ACO-Payload-SHA256": sha,
    "X-ACO-Signature": "v1=" + sig,
    "Content-Type": "application/json",
  };
  if (opts.idem) headers["Idempotency-Key"] = opts.idem;
  return SELF.fetch(BASE + path, { method: "POST", body: raw, headers });
}

async function frameGet(deviceId: string, token: string): Promise<Response> {
  return SELF.fetch(`${BASE}/api/v1/device/${deviceId}/frame`, {
    method: "GET",
    headers: { Authorization: `Bearer ${token}`, "X-Frame-Schema-Version": "1" },
  });
}

// Unique nonce per call so independent tests never collide on the DO's shared nonce store.
let nonceCounter = 0;
function freshNonce(): string {
  nonceCounter += 1;
  return nonceCounter.toString(16).padStart(32, "0");
}

describe("relay ingest security (mirrors test_ingest.py)", () => {
  beforeAll(() => {
    // sanity: SELF must be bound (skips cleanly if the harness didn't load).
    expect(SELF).toBeDefined();
  });

  it("valid signed ingest accepted", async () => {
    const r = await post(body(), { nonce: freshNonce() });
    expect(r.status).toBe(200);
    const j = (await r.json()) as Record<string, unknown>;
    expect(j["ok"]).toBe(true);
    expect(j["accepted"]).toBe(1);
    expect(j["rejected"]).toBe(0);
    expect((j["results"] as Array<Record<string, unknown>>)[0]!["status"]).toBe("accepted");
  });

  it("bad signature → 401", async () => {
    const r = await post(body(), { breakSig: true, nonce: freshNonce() });
    expect(r.status).toBe(401);
    expect(((await r.json()) as Record<string, unknown>)["reason"]).toBe("bad_signature");
  });

  it("stale timestamp → 401 with server_time (collector resyncs, no loop)", async () => {
    const r = await post(body(), { ts: Math.floor(Date.now() / 1000) - 400, nonce: freshNonce() });
    expect(r.status).toBe(401);
    const j = (await r.json()) as Record<string, unknown>;
    expect(j["reason"]).toBe("stale_timestamp");
    expect(typeof j["server_time"]).toBe("number");
    expect(j["server_time"]).toBeGreaterThan(0);
  });

  it("reused nonce → 409", async () => {
    const n = freshNonce();
    expect((await post(body(), { nonce: n })).status).toBe(200);
    const r2 = await post(body(), { nonce: n });
    expect(r2.status).toBe(409);
    expect(((await r2.json()) as Record<string, unknown>)["reason"]).toBe("reused_nonce");
  });

  it("payload hash mismatch → 400", async () => {
    const r = await post(body(), { breakHash: true, nonce: freshNonce() });
    expect(r.status).toBe(400);
    expect(((await r.json()) as Record<string, unknown>)["reason"]).toBe("payload_hash_mismatch");
  });

  it("unknown/revoked kid → 403", async () => {
    const r = await post(body(), { kid: "ghost", secret: "whatever", nonce: freshNonce() });
    expect(r.status).toBe(403);
    expect(((await r.json()) as Record<string, unknown>)["reason"]).toBe("collector_revoked");
  });

  it("bad collector id charset → 400 (before signature)", async () => {
    // The Worker rejects a malformed :id at the edge before any DO/HMAC work.
    const r = await SELF.fetch(`${BASE}/api/v1/collectors/bad%20id!/events`, {
      method: "POST",
      body: "{}",
      headers: { "Content-Type": "application/json" },
    });
    expect(r.status).toBe(400);
    expect(((await r.json()) as Record<string, unknown>)["reason"]).toBe("bad_collector_id");
  });

  it("batch too large → 413", async () => {
    const ev = body().events as unknown[];
    const many = Array.from({ length: 51 }, (_, i) => ({ ...(ev[0] as Record<string, unknown>), event_id: `e${i}` }));
    const r = await post(body(many), { nonce: freshNonce() });
    expect(r.status).toBe(413);
    expect(((await r.json()) as Record<string, unknown>)["reason"]).toBe("batch_too_large");
  });

  it("body too large → 413", async () => {
    const big = body() as Record<string, unknown>;
    big["pad"] = "x".repeat(100 * 1024 + 10);
    const r = await post(big, { nonce: freshNonce() });
    expect(r.status).toBe(413);
    expect(((await r.json()) as Record<string, unknown>)["reason"]).toBe("body_too_large");
  });

  it("idempotent retry returns prior result (fresh nonce, same key)", async () => {
    const r1 = await post(body(), { nonce: freshNonce(), idem: "batch-001" });
    expect(r1.status).toBe(200);
    const j1 = (await r1.json()) as Record<string, unknown>;
    expect(j1["duplicate"]).toBeUndefined();
    const r2 = await post(body(), { nonce: freshNonce(), idem: "batch-001" });
    expect(r2.status).toBe(200);
    const j2 = (await r2.json()) as Record<string, unknown>;
    expect(j2["duplicate"]).toBe(true);
    expect(j2["ingest_id"]).toBe(j1["ingest_id"]);
  });

  it("poison event rejected per-event, not per-request (cloud validate-only gate, I1)", async () => {
    const events = [
      {
        event_id: "ok1",
        event_type: "session.upsert",
        provider: "claude",
        account_alias: "main",
        payload: { session_id: "hmac:a", project_alias: "project-a", status: "CODING", task_label: "implementing" },
      },
      {
        event_id: "leak",
        event_type: "session.upsert",
        provider: "claude",
        account_alias: "main",
        payload: { session_id: "hmac:b", project_alias: "/Users/hulu/secret/path", status: "CODING", task_label: "implementing" },
      },
    ];
    const r = await post(body(events), { nonce: freshNonce() });
    expect(r.status).toBe(200);
    const j = (await r.json()) as Record<string, unknown>;
    const res = Object.fromEntries((j["results"] as Array<Record<string, unknown>>).map((x) => [x["event_id"], x]));
    expect((res["ok1"] as Record<string, unknown>)["status"]).toBe("accepted");
    expect((res["leak"] as Record<string, unknown>)["status"]).toBe("rejected");
    expect(j["accepted"]).toBe(1);
    expect(j["rejected"]).toBe(1);
  });

  // -------------------------------------------------------------------------------------------
  // CRITICAL FIX (docs/devlog/16 I1): the quota.window branch previously called setQuota DIRECTLY
  // with the attacker-controlled account_alias/provider, bypassing the validate gate — a signed
  // batch could put "/Users/.../secret" into frame.quota[].account served to the device. The
  // quota branch now passes the SAME default-deny gate as session.* (validateQuotaEvent).
  it("quota.window with a path account_alias is rejected per-event (was a validate-gate bypass)", async () => {
    const events = [
      {
        event_id: "q_leak",
        event_type: "quota.window",
        provider: "claude",
        account_alias: "/Users/hulu/secret-project",
        payload: { window_type: "5h", used_ratio: 0.95 },
      },
      {
        event_id: "q_ok",
        event_type: "quota.window",
        provider: "claude",
        account_alias: "main",
        payload: { window_type: "5h", used_ratio: 0.95 },
      },
      {
        event_id: "q_nan",
        event_type: "quota.window",
        provider: "claude",
        account_alias: "work",
        payload: { window_type: "5h", used_ratio: "x" }, // Number("x")→NaN → reject (parity w/ Python float())
      },
    ];
    const r = await post(body(events), { nonce: freshNonce() });
    expect(r.status).toBe(200);
    const j = (await r.json()) as Record<string, unknown>;
    const res = Object.fromEntries((j["results"] as Array<Record<string, unknown>>).map((x) => [x["event_id"], x]));
    expect((res["q_leak"] as Record<string, unknown>)["status"]).toBe("rejected");
    expect((res["q_ok"] as Record<string, unknown>)["status"]).toBe("accepted");
    expect((res["q_nan"] as Record<string, unknown>)["status"]).toBe("rejected");
    expect(j["accepted"]).toBe(1);
    expect(j["rejected"]).toBe(2);

    // And the device frame NEVER carries the leaked account — it shows the validated "main" only.
    const fr = await frameGet("orb-01", "dev-local-token");
    expect(fr.status).toBe(200);
    const frame = (await fr.json()) as Record<string, unknown>;
    const quotaAccounts = ((frame["quota"] as Array<Record<string, unknown>>) ?? []).map((q) => q["account"]);
    expect(quotaAccounts).not.toContain("/Users/hulu/secret-project");
  });
});

describe("device frame auth (uniform errors, bearer header-only)", () => {
  it("known device + correct token → 200 frame", async () => {
    const r = await frameGet("orb-01", "dev-local-token");
    expect(r.status).toBe(200);
    const j = (await r.json()) as Record<string, unknown>;
    expect(j["v"]).toBe(1);
    expect(j["device_id"]).toBe("orb-01");
    expect(typeof j["scene"]).toBe("string");
  });

  it("known device + wrong token → 401 bad_token", async () => {
    const r = await frameGet("orb-01", "nope");
    expect(r.status).toBe(401);
    const j = (await r.json()) as Record<string, unknown>;
    expect(j["error"]).toBe("bad_token");
    expect(j["retry"]).toBe(false);
  });

  it("unknown device → 404 unknown_device", async () => {
    const r = await frameGet("ghost-device", "whatever");
    expect(r.status).toBe(404);
    expect(((await r.json()) as Record<string, unknown>)["error"]).toBe("unknown_device");
  });
});

// ---------------------------------------------------------------------------------------------
// I4 IMMEDIATE revocation (verifier-found MEDIUM defect #2).
//
// A revoke must take effect AT ONCE (the DO owns the revocation set; KV is eventually
// consistent — I4). These tests prove revocation of a PREVIOUSLY-VALID credential, NOT merely an
// unknown one: k2 / orb-02 are configured in the miniflare bindings, accepted FIRST, then revoked
// via the DO admin route, then the SAME correctly-signed / correctly-tokened request is rejected.
// (k2/orb-02 are dedicated to these tests so revoking them can't poison the k1/orb-01 happy path.)
//
// We drive the DO admin op (/do/admin/revoke-*) on the singleton "relay" stub directly — the same
// internal op the Worker forwards to — because the public Worker intentionally exposes no /admin
// route (that path is Cloudflare-Access-gated in production, no ESP32 involved).
function relayStub() {
  // `env` from cloudflare:test is typed as the (possibly-empty) generated Cloudflare.Env; the
  // RELAY DO binding is provided via miniflare.bindings + wrangler.toml at runtime. Narrow it
  // locally to the one binding we need rather than augmenting the generated namespace.
  const e = env as unknown as { RELAY: DurableObjectNamespace };
  const id = e.RELAY.idFromName("relay");
  return e.RELAY.get(id);
}

const REVOKE_KID = "k2";
const REVOKE_SECRET = "test-collector-secret-2";

describe("I4 immediate revocation (revoke a PREVIOUSLY-VALID credential, defect #2)", () => {
  it("revoked kid → 403 collector_revoked on a SUBSEQUENT correctly-signed request", async () => {
    // 1. k2 is valid first: a correctly-signed ingest is accepted (proves it's a known good kid,
    //    so a later 403 is REVOCATION, not unknown-kid).
    const ok = await post(body(), { kid: REVOKE_KID, secret: REVOKE_SECRET, nonce: freshNonce() });
    expect(ok.status).toBe(200);
    expect(((await ok.json()) as Record<string, unknown>)["ok"]).toBe(true);

    // 2. Revoke k2 via the DO admin route (immediate, strongly-consistent — I4). The DO now enforces
    //    the admin replay contract (docs/devlog/16 MED), so a DO-direct call carries fresh X-ACO-*.
    const revoke = await relayStub().fetch("https://do/do/admin/revoke-kid", {
      method: "POST",
      body: JSON.stringify({ kid: REVOKE_KID }),
      headers: { "content-type": "application/json", ...adminReplayHeaders() },
    });
    expect(revoke.status).toBe(200);
    expect(((await revoke.json()) as Record<string, unknown>)["revoked"]).toBe(REVOKE_KID);

    // 3. A FRESH correctly-signed request from the now-revoked kid is rejected 403 AT ONCE.
    const after = await post(body(), { kid: REVOKE_KID, secret: REVOKE_SECRET, nonce: freshNonce() });
    expect(after.status).toBe(403);
    expect(((await after.json()) as Record<string, unknown>)["reason"]).toBe("collector_revoked");

    // k1 (the happy-path kid) is unaffected — revocation is per-kid, immediate, isolated.
    const k1ok = await post(body(), { nonce: freshNonce() });
    expect(k1ok.status).toBe(200);
  });

  it("revoked device token → 403 device_revoked on a SUBSEQUENT correctly-tokened request", async () => {
    // 1. orb-02 with its correct token works first (proves a valid, known device).
    const ok = await frameGet("orb-02", "dev-local-token-2");
    expect(ok.status).toBe(200);
    expect(((await ok.json()) as Record<string, unknown>)["device_id"]).toBe("orb-02");

    // 2. Revoke orb-02 via the DO admin route (DO enforces the admin replay contract → fresh X-ACO-*).
    const revoke = await relayStub().fetch("https://do/do/admin/revoke-device", {
      method: "POST",
      body: JSON.stringify({ device_id: "orb-02" }),
      headers: { "content-type": "application/json", ...adminReplayHeaders() },
    });
    expect(revoke.status).toBe(200);
    expect(((await revoke.json()) as Record<string, unknown>)["revoked"]).toBe("orb-02");

    // 3. The SAME correct token is now rejected 403 (revoked wins over a valid token — I4).
    //    403, NOT 401 (bad token) and NOT 404 (unknown) — proves immediate revocation precedence.
    const after = await frameGet("orb-02", "dev-local-token-2");
    expect(after.status).toBe(403);
    expect(((await after.json()) as Record<string, unknown>)["error"]).toBe("device_revoked");

    // The /cacerts route shares the same revocation gate: a revoked device can't pull a fresh CA.
    const caAfter = await SELF.fetch(`${BASE}/api/v1/device/orb-02/cacerts`, {
      method: "GET",
      headers: { Authorization: "Bearer dev-local-token-2", Accept: "application/x-pem-file" },
    });
    expect(caAfter.status).toBe(403);
    expect(((await caAfter.json()) as Record<string, unknown>)["error"]).toBe("device_revoked");

    // orb-01 (happy path) unaffected.
    const orb01 = await frameGet("orb-01", "dev-local-token");
    expect(orb01.status).toBe(200);
  });
});

// ---------------------------------------------------------------------------------------------
// Cross-piece /cacerts route (verifier-found HIGH defect #4) — firmware/src/relay.h calls
// GET /api/v1/device/:id/cacerts (Bearer + Accept: application/x-pem-file) on TLS failure to
// refresh the pinned CA bundle without a reflash. The Worker now serves it, bearer-authed like
// /frame, returning a structurally valid PEM bundle (pemLooksValid).
describe("device /cacerts pinned-CA refresh (cross-piece, defect #4)", () => {
  async function caGet(deviceId: string, token: string): Promise<Response> {
    return SELF.fetch(`${BASE}/api/v1/device/${deviceId}/cacerts`, {
      method: "GET",
      headers: { Authorization: `Bearer ${token}`, Accept: "application/x-pem-file" },
    });
  }

  it("known device + correct token → 200 PEM bundle (firmware pemLooksValid passes)", async () => {
    const r = await caGet("orb-01", "dev-local-token");
    expect(r.status).toBe(200);
    expect(r.headers.get("content-type")).toContain("application/x-pem-file");
    // Read as bytes then decode (the runtime doesn't classify x-pem-file as text; the firmware
    // reads raw bytes via HTTPClient.getString() too).
    const pem = new TextDecoder().decode(await r.arrayBuffer());
    // Mirrors firmware pemLooksValid: must contain BEGIN + END CERTIFICATE markers.
    expect(pem).toContain("-----BEGIN CERTIFICATE-----");
    expect(pem).toContain("-----END CERTIFICATE-----");
    // The documented default ships the 3 pinned roots (≥1 cert; firmware bundle = 3).
    expect((pem.match(/-----BEGIN CERTIFICATE-----/g) ?? []).length).toBeGreaterThanOrEqual(1);
  });

  it("known device + wrong token → 401 bad_token (bearer-authed like /frame)", async () => {
    const r = await caGet("orb-01", "nope");
    expect(r.status).toBe(401);
    expect(((await r.json()) as Record<string, unknown>)["error"]).toBe("bad_token");
  });

  it("unknown device → 404 unknown_device", async () => {
    const r = await caGet("ghost-device", "whatever");
    expect(r.status).toBe(404);
    expect(((await r.json()) as Record<string, unknown>)["error"]).toBe("unknown_device");
  });

  it("bad device id charset → 404 (edge gate, no DO/secret work)", async () => {
    const r = await SELF.fetch(`${BASE}/api/v1/device/bad%20id!/cacerts`, {
      method: "GET",
      headers: { Authorization: "Bearer whatever" },
    });
    expect(r.status).toBe(404);
    expect(((await r.json()) as Record<string, unknown>)["error"]).toBe("unknown_device");
  });
});

// ---------------------------------------------------------------------------------------------
// PUBLIC /admin revoke routes (verifier-found SUBSTANTIVE defect A) — the DO implemented
// revocation but the Worker exposed NO public route forwarding to it, so revocation could not be
// triggered in production. The Worker now exposes:
//     POST /admin/collectors/:kid/revoke
//     POST /admin/devices/:id/revoke
// gated by a CONSTANT-TIME bearer check against AGENTLAMP_ADMIN_TOKEN, FAIL-CLOSED (403) when the
// token is unset. (Cloudflare Access can ALSO gate /admin at the edge — defense in depth.)
//
// k3 / orb-03 are dedicated to these tests (revoke a PREVIOUSLY-VALID credential VIA THE PUBLIC
// ROUTE, then prove a subsequent correctly-signed/correctly-tokened request is rejected), so they
// never poison the k1/orb-01 happy path or the k2/orb-02 DO-direct tests.
const ADMIN_TOKEN = "test-admin-token"; // matches vitest.workers.config.ts bindings
const ADMIN_KID = "k3";
const ADMIN_SECRET = "test-collector-secret-3";

// docs/devlog/16 MED (admin replay): every /admin/* op now requires a fresh X-ACO-Timestamp
// (±300s) + a single-use X-ACO-Nonce (the persisted nonce store, reused). These helpers attach a
// fresh pair by default so the existing bearer/charset/fail-closed assertions are unchanged; a
// caller can override `replay` to pin a stale/duplicate/missing pair for the replay-rejection tests.
interface AdminReplay {
  timestamp?: string | null; // null = omit the header
  nonce?: string | null; // null = omit the header
}
function adminReplayHeaders(replay?: AdminReplay): Record<string, string> {
  const h: Record<string, string> = {};
  const ts = replay && "timestamp" in replay ? replay.timestamp : String(Math.trunc(Date.now() / 1000));
  const nonce = replay && "nonce" in replay ? replay.nonce : freshNonce();
  if (ts !== null && ts !== undefined) h["X-ACO-Timestamp"] = ts;
  if (nonce !== null && nonce !== undefined) h["X-ACO-Nonce"] = nonce;
  return h;
}

async function adminPost(path: string, token: string | null, replay?: AdminReplay): Promise<Response> {
  const headers: Record<string, string> = { "content-type": "application/json", ...adminReplayHeaders(replay) };
  if (token !== null) headers["Authorization"] = `Bearer ${token}`;
  return SELF.fetch(`${BASE}${path}`, { method: "POST", headers });
}

describe("public /admin revoke routes (defect A)", () => {
  it("(a) admin-revoke a previously-VALID kid → 200, then that kid → 403 collector_revoked", async () => {
    // k3 is valid FIRST (a correctly-signed ingest is accepted — so a later 403 is REVOCATION).
    const ok = await post(body(), { kid: ADMIN_KID, secret: ADMIN_SECRET, nonce: freshNonce() });
    expect(ok.status).toBe(200);
    expect(((await ok.json()) as Record<string, unknown>)["ok"]).toBe(true);

    // Revoke via the PUBLIC admin route with the correct admin token → 200 (DO returns revoked).
    const rev = await adminPost(`/admin/collectors/${ADMIN_KID}/revoke`, ADMIN_TOKEN);
    expect(rev.status).toBe(200);
    expect(((await rev.json()) as Record<string, unknown>)["revoked"]).toBe(ADMIN_KID);

    // A FRESH correctly-signed request from the now-revoked kid is rejected 403 AT ONCE (I4).
    const after = await post(body(), { kid: ADMIN_KID, secret: ADMIN_SECRET, nonce: freshNonce() });
    expect(after.status).toBe(403);
    expect(((await after.json()) as Record<string, unknown>)["reason"]).toBe("collector_revoked");

    // k1 (happy path) is unaffected.
    expect((await post(body(), { nonce: freshNonce() })).status).toBe(200);
  });

  it("(b) admin-revoke a previously-VALID device token → 200, then that device → 403 device_revoked", async () => {
    // orb-03 with its correct token works first (a valid, known device).
    const ok = await frameGet("orb-03", "dev-local-token-3");
    expect(ok.status).toBe(200);
    expect(((await ok.json()) as Record<string, unknown>)["device_id"]).toBe("orb-03");

    // Revoke via the PUBLIC admin route.
    const rev = await adminPost(`/admin/devices/orb-03/revoke`, ADMIN_TOKEN);
    expect(rev.status).toBe(200);
    expect(((await rev.json()) as Record<string, unknown>)["revoked"]).toBe("orb-03");

    // The SAME correct token is now rejected 403 (revoked wins over a valid token — I4).
    const after = await frameGet("orb-03", "dev-local-token-3");
    expect(after.status).toBe(403);
    expect(((await after.json()) as Record<string, unknown>)["error"]).toBe("device_revoked");

    // orb-01 (happy path) unaffected.
    expect((await frameGet("orb-01", "dev-local-token")).status).toBe(200);
  });

  it("(c) admin route with MISSING admin token → 401", async () => {
    const r = await adminPost(`/admin/collectors/k1/revoke`, null);
    expect(r.status).toBe(401);
    expect(((await r.json()) as Record<string, unknown>)["error"]).toBe("admin_unauthorized");
  });

  it("(c) admin route with WRONG admin token → 401 (constant-time compare, no oracle)", async () => {
    const r = await adminPost(`/admin/collectors/k1/revoke`, "wrong-token");
    expect(r.status).toBe(401);
    expect(((await r.json()) as Record<string, unknown>)["error"]).toBe("admin_unauthorized");
  });

  it("(c) admin route with bad kid charset → 400 (edge gate, before the DO)", async () => {
    const r = await adminPost(`/admin/collectors/bad%20kid!/revoke`, ADMIN_TOKEN);
    expect(r.status).toBe(400);
    expect(((await r.json()) as Record<string, unknown>)["error"]).toBe("bad_id");
  });

  it("(d) UNSET AGENTLAMP_ADMIN_TOKEN → route is FAIL-CLOSED (403), never open", async () => {
    // Call the Worker's fetch() DIRECTLY with the admin token binding STRIPPED (the rest of env
    // intact, incl. the RELAY DO binding). Even with the CORRECT token presented, an unset server
    // secret must NEVER authorize the route — it fails closed with 403, not open.
    const { AGENTLAMP_ADMIN_TOKEN: _drop, ...envNoAdmin } = env as Record<string, unknown>;
    const ctx = createExecutionContext();
    const req = new Request(`${BASE}/admin/collectors/k1/revoke`, {
      method: "POST",
      headers: { Authorization: `Bearer ${ADMIN_TOKEN}`, "content-type": "application/json" },
    });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const r = await (worker as any).fetch(req, envNoAdmin, ctx);
    await waitOnExecutionContext(ctx);
    expect(r.status).toBe(403);
    expect(((await r.json()) as Record<string, unknown>)["error"]).toBe("admin_disabled");

    // And the k1 happy path is still intact (we never actually revoked it).
    expect((await post(body(), { nonce: freshNonce() })).status).toBe(200);
  });
});

// ---------------------------------------------------------------------------------------------
// P0 RUNTIME ENROLL (docs/devlog/16 FIX 2, I5) — the headline "switch computer in one line".
//
// Before this fix the DO loaded collector keys ONLY from env.AGENTLAMP_COLLECTOR_KEYS at
// construction; there was NO runtime enroll, so adding a computer needed `wrangler secret put` +
// `wrangler deploy` (owner-gated) — the documented one-liner was fiction. opEnrollDevice existed
// but was unreachable AND didn't persist. The Worker now exposes:
//     POST /admin/collectors/:kid/enroll  (body: { secret })
//     POST /admin/devices/:id/enroll      (body: { token })
// gated by the SAME constant-time bearer + fail-closed-403 + charset + rate-limit as revoke. The
// DO persists kid->secret / device->token-hash to DO storage and unions them with the env-derived
// keys/hashes on bootstrap, so an enroll survives a DO restart with NO redeploy.
//
// e1/orb-e1 = collector/device enrolled fresh (NOT in the env bindings). e2 = revoke-then-reenroll.
// e3/orb-e3 = persist-across-restart. These ids are unique so they never poison the happy path.
const ENROLL_KID = "e1";
const ENROLL_SECRET = "enrolled-secret-e1";
const ENROLL_DEVICE = "orb-e1";
const ENROLL_DEVICE_TOKEN = "enrolled-device-token-e1";

async function adminEnroll(path: string, jsonBody: Record<string, unknown>, token: string | null, replay?: AdminReplay): Promise<Response> {
  const headers: Record<string, string> = { "content-type": "application/json", ...adminReplayHeaders(replay) };
  if (token !== null) headers["Authorization"] = `Bearer ${token}`;
  return SELF.fetch(`${BASE}${path}`, { method: "POST", body: JSON.stringify(jsonBody), headers });
}

function relayStubE(): DurableObjectStub {
  const e = env as unknown as { RELAY: DurableObjectNamespace };
  return e.RELAY.get(e.RELAY.idFromName("relay"));
}

describe("P0 runtime enroll (FIX 2, I5: switch computer in one line)", () => {
  it("(a) enroll a NEW kid via the route → a signed request from it is accepted", async () => {
    // The kid is NOT in env, so a signed request is rejected FIRST (proves enroll is what flips it).
    const before = await post(body(), { kid: ENROLL_KID, secret: ENROLL_SECRET, nonce: freshNonce() });
    expect(before.status).toBe(403);
    expect(((await before.json()) as Record<string, unknown>)["reason"]).toBe("collector_revoked");

    // Enroll via the public route (one authed POST — no wrangler deploy).
    const enr = await adminEnroll(`/admin/collectors/${ENROLL_KID}/enroll`, { secret: ENROLL_SECRET }, ADMIN_TOKEN);
    expect(enr.status).toBe(200);
    expect(((await enr.json()) as Record<string, unknown>)["kid"]).toBe(ENROLL_KID);

    // A SUBSEQUENT correctly-signed request from the now-enrolled kid is accepted AT ONCE.
    const after = await post(body(), { kid: ENROLL_KID, secret: ENROLL_SECRET, nonce: freshNonce() });
    expect(after.status).toBe(200);
    expect(((await after.json()) as Record<string, unknown>)["ok"]).toBe(true);

    // A WRONG secret for the enrolled kid still fails (the secret is honoured, not just the kid).
    const wrong = await post(body(), { kid: ENROLL_KID, secret: "wrong-secret", nonce: freshNonce() });
    expect(wrong.status).toBe(401);
    expect(((await wrong.json()) as Record<string, unknown>)["reason"]).toBe("bad_signature");
  });

  it("(b) enroll a new device token → /frame with it works", async () => {
    // Unknown device first → 404 (proves enroll is what makes it known).
    const before = await frameGet(ENROLL_DEVICE, ENROLL_DEVICE_TOKEN);
    expect(before.status).toBe(404);
    expect(((await before.json()) as Record<string, unknown>)["error"]).toBe("unknown_device");

    const enr = await adminEnroll(`/admin/devices/${ENROLL_DEVICE}/enroll`, { token: ENROLL_DEVICE_TOKEN }, ADMIN_TOKEN);
    expect(enr.status).toBe(200);
    expect(((await enr.json()) as Record<string, unknown>)["device_id"]).toBe(ENROLL_DEVICE);

    // /frame with the enrolled token now works.
    const fr = await frameGet(ENROLL_DEVICE, ENROLL_DEVICE_TOKEN);
    expect(fr.status).toBe(200);
    expect(((await fr.json()) as Record<string, unknown>)["device_id"]).toBe(ENROLL_DEVICE);

    // The wrong token for the enrolled device still 401s (hash is honoured, not just the id).
    const wrong = await frameGet(ENROLL_DEVICE, "nope");
    expect(wrong.status).toBe(401);
    expect(((await wrong.json()) as Record<string, unknown>)["error"]).toBe("bad_token");
  });

  it("(c) revoke then RE-enroll the SAME kid → accepted again (revoke is not permanent)", async () => {
    const kid = "e2";
    const secret = "enrolled-secret-e2";
    // Enroll → accepted.
    expect((await adminEnroll(`/admin/collectors/${kid}/enroll`, { secret }, ADMIN_TOKEN)).status).toBe(200);
    expect((await post(body(), { kid, secret, nonce: freshNonce() })).status).toBe(200);

    // Revoke → the same correctly-signed request is now 403 (destructive: the key is removed).
    expect((await adminPost(`/admin/collectors/${kid}/revoke`, ADMIN_TOKEN)).status).toBe(200);
    const revoked = await post(body(), { kid, secret, nonce: freshNonce() });
    expect(revoked.status).toBe(403);
    expect(((await revoked.json()) as Record<string, unknown>)["reason"]).toBe("collector_revoked");

    // RE-enroll the SAME kid → accepted again (revokedKids.delete on enroll).
    expect((await adminEnroll(`/admin/collectors/${kid}/enroll`, { secret }, ADMIN_TOKEN)).status).toBe(200);
    const reenrolled = await post(body(), { kid, secret, nonce: freshNonce() });
    expect(reenrolled.status).toBe(200);
    expect(((await reenrolled.json()) as Record<string, unknown>)["ok"]).toBe(true);
  });

  it("(d) enroll PERSISTS across a simulated DO restart (re-bootstrap from storage) → still accepted", async () => {
    const kid = "e3";
    const secret = "enrolled-secret-e3";
    const deviceId = "orb-e3";
    const deviceToken = "enrolled-device-token-e3";
    expect((await adminEnroll(`/admin/collectors/${kid}/enroll`, { secret }, ADMIN_TOKEN)).status).toBe(200);
    expect((await adminEnroll(`/admin/devices/${deviceId}/enroll`, { token: deviceToken }, ADMIN_TOKEN)).status).toBe(200);

    // Simulate a DO restart: clear the in-memory live state + bootstrapped flag, then re-bootstrap.
    // The persisted "collector_keys"/"device_token_hashes" in DO storage must be restored and
    // unioned with env, so the enrolled credentials survive eviction with NO redeploy.
    await runInDurableObject(relayStubE(), async (instance) => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const di = instance as any;
      di.bootstrapped = false;
      di.keys = new Map();
      di.enrolledKeys = new Map();
      di.deviceTokenHashes = new Map();
      di.enrolledHashes = new Map();
      di.envHashes = new Map();
      await di.bootstrap();
      // The restored unions must contain the enrolled credentials (and still the env k1/orb-01).
      expect(di.keys.has(kid)).toBe(true);
      expect(di.keys.has("k1")).toBe(true);
      const expectedHash = await (async () => {
        const buf = await crypto.subtle.digest("SHA-256", enc.encode(deviceToken));
        return hex(buf);
      })();
      expect(di.deviceTokenHashes.get(deviceId)).toBe(expectedHash);
    });

    // After the restart a signed request from the enrolled kid is STILL accepted, and /frame with
    // the enrolled device token STILL works — proving persistence, not just in-memory state.
    const after = await post(body(), { kid, secret, nonce: freshNonce() });
    expect(after.status).toBe(200);
    expect(((await after.json()) as Record<string, unknown>)["ok"]).toBe(true);
    const fr = await frameGet(deviceId, deviceToken);
    expect(fr.status).toBe(200);
    expect(((await fr.json()) as Record<string, unknown>)["device_id"]).toBe(deviceId);
  });

  it("(e) enroll with MISSING admin token → 401; UNSET server admin token → 403 (fail-closed)", async () => {
    // Missing bearer → 401 (same gate as revoke).
    const noTok = await adminEnroll(`/admin/collectors/e9/enroll`, { secret: "x" }, null);
    expect(noTok.status).toBe(401);
    expect(((await noTok.json()) as Record<string, unknown>)["error"]).toBe("admin_unauthorized");

    // Wrong bearer → 401 (constant-time compare, no oracle).
    const wrongTok = await adminEnroll(`/admin/collectors/e9/enroll`, { secret: "x" }, "wrong-token");
    expect(wrongTok.status).toBe(401);

    // UNSET server admin token → FAIL-CLOSED 403 even with the correct token presented.
    const { AGENTLAMP_ADMIN_TOKEN: _drop, ...envNoAdmin } = env as Record<string, unknown>;
    const ctx = createExecutionContext();
    const req = new Request(`${BASE}/admin/collectors/e9/enroll`, {
      method: "POST",
      body: JSON.stringify({ secret: "x" }),
      headers: { Authorization: `Bearer ${ADMIN_TOKEN}`, "content-type": "application/json" },
    });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const r = await (worker as any).fetch(req, envNoAdmin, ctx);
    await waitOnExecutionContext(ctx);
    expect(r.status).toBe(403);
    expect(((await r.json()) as Record<string, unknown>)["error"]).toBe("admin_disabled");
  });

  it("(f) enroll with an empty/missing credential body → 400 (never enroll an empty credential)", async () => {
    const empty = await adminEnroll(`/admin/collectors/e9/enroll`, {}, ADMIN_TOKEN);
    expect(empty.status).toBe(400);
    expect(((await empty.json()) as Record<string, unknown>)["error"]).toBe("bad_request");

    const blank = await adminEnroll(`/admin/devices/orb-e9/enroll`, { token: "   " }, ADMIN_TOKEN);
    expect(blank.status).toBe(400);
  });

  it("(g) bad kid charset on enroll → 400 (edge gate, before the DO)", async () => {
    const r = await adminEnroll(`/admin/collectors/bad%20kid!/enroll`, { secret: "x" }, ADMIN_TOKEN);
    expect(r.status).toBe(400);
    expect(((await r.json()) as Record<string, unknown>)["error"]).toBe("bad_id");
  });
});

// ---------------------------------------------------------------------------------------------
// docs/devlog/16 HIGH — replay window + idempotency PERSIST across a DO eviction.
//
// Before this fix the nonce set + idempotency map were in-memory ONLY: a DO eviction reset the
// replay window, so a captured signed batch could replay within ±300s and a retried batch could
// re-apply. These tests simulate an eviction (clear in-memory state + re-bootstrap from storage)
// and prove the persisted stores survive it.
// ---------------------------------------------------------------------------------------------

// Simulate a DO eviction: drop the in-memory caches + bootstrapped flag, then re-bootstrap so the
// instance reloads nonces / idempotency / frame_state from DO storage (exactly what a cold DO does).
async function simulateEviction(): Promise<void> {
  await runInDurableObject(relayStubE(), async (instance) => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const di = instance as any;
    di.bootstrapped = false;
    di.nonces = new Map();
    di.idem = new Map();
    di.keys = new Map();
    di.enrolledKeys = new Map();
    di.deviceTokenHashes = new Map();
    di.enrolledHashes = new Map();
    di.envHashes = new Map();
    // Drop the materialized frame to a sentinel so we can prove bootstrap restored it (not kept).
    di.frame = { sessions: { __evicted__: true }, quota: {}, last_collector_heartbeat: 0, seq: -1, last_signature: null };
    await di.bootstrap();
  });
}

describe("HIGH: persistence survives DO eviction", () => {
  it("nonce replay is REJECTED after a bootstrap (replay window survived the eviction)", async () => {
    const n = freshNonce();
    // 1. A signed request with nonce N is accepted (and the nonce is persisted to DO storage).
    const first = await post(body(), { nonce: n });
    expect(first.status).toBe(200);

    // 2. Simulate a DO eviction: in-memory nonces wiped, then re-bootstrap from storage.
    await simulateEviction();

    // 3. The SAME nonce N is replayed AFTER the bootstrap → 409 reused_nonce. Without persistence
    //    this would be 200 (the in-memory set was empty post-eviction) — the HIGH bug.
    const replay = await post(body(), { nonce: n });
    expect(replay.status).toBe(409);
    expect(((await replay.json()) as Record<string, unknown>)["reason"]).toBe("reused_nonce");

    // A genuinely fresh nonce still works after the bootstrap (the gate isn't just stuck-closed).
    expect((await post(body(), { nonce: freshNonce() })).status).toBe(200);
  });

  it("idempotency record survives eviction → a retried batch returns the prior result, not re-applied", async () => {
    const idemKey = "idem-evict-" + freshNonce();
    // 1. First request with an idempotency key → 200, recorded + persisted.
    const first = await post(body(), { nonce: freshNonce(), idem: idemKey });
    expect(first.status).toBe(200);
    const firstJson = (await first.json()) as Record<string, unknown>;
    expect(firstJson["ok"]).toBe(true);
    const firstIngestId = firstJson["ingest_id"];

    // 2. Evict + re-bootstrap (idempotency map reloads from storage).
    await simulateEviction();

    // 3. A retried batch (same idempotency key, FRESH nonce so the replay gate passes) returns the
    //    PRIOR result tagged duplicate — proving the record survived, so it is not re-applied.
    const retry = await post(body(), { nonce: freshNonce(), idem: idemKey });
    expect(retry.status).toBe(200);
    const retryJson = (await retry.json()) as Record<string, unknown>;
    expect(retryJson["duplicate"]).toBe(true);
    expect(retryJson["ingest_id"]).toBe(firstIngestId); // same ingest id = the stored prior result
  });

  it("materialized frame_state survives eviction (defensive-restore, MED) → device still sees the session", async () => {
    // 1. Apply a session for a dedicated device's account; the frame is persisted.
    const dev = "orb-01"; // a known env device with a valid token
    expect((await post(body(), { nonce: freshNonce() })).status).toBe(200);
    const before = await frameGet(dev, "dev-local-token");
    expect(before.status).toBe(200);

    // 2. Evict (the sentinel frame proves restore happened) + re-bootstrap.
    await simulateEviction();

    // 3. The restored frame is a valid FrameStateData (not the sentinel, not empty) — /frame works
    //    and the materialized session is still present.
    await runInDurableObject(relayStubE(), async (instance) => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const di = instance as any;
      expect(di.frame.seq).not.toBe(-1); // sentinel had seq -1
      expect(di.frame.sessions["__evicted__"]).toBeUndefined(); // sentinel session is gone
      expect(Object.keys(di.frame.sessions).length).toBeGreaterThan(0); // real session restored
    });
    const after = await frameGet(dev, "dev-local-token");
    expect(after.status).toBe(200);
  });

  it("a CORRUPT stored frame_state falls back to a fresh state (defensive shape validation)", async () => {
    // Write a structurally-corrupt frame_state directly to storage, then bootstrap. The defensive
    // isFrameStateData() guard must reject it and fall back to newStateData() — never crash or trust
    // a malformed/hostile value.
    await runInDurableObject(relayStubE(), async (instance) => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const di = instance as any;
      await di.state.storage.put("frame_state", { sessions: "not-an-object", quota: 42 });
      di.bootstrapped = false;
      di.frame = { sessions: { __evicted__: true }, quota: {}, last_collector_heartbeat: 0, seq: -1, last_signature: null };
      await di.bootstrap();
      // Fell back to a fresh state: empty maps, a real seq (0), the sentinel gone.
      expect(di.frame.sessions["__evicted__"]).toBeUndefined();
      expect(Object.keys(di.frame.sessions).length).toBe(0);
      expect(di.frame.seq).toBe(0);
    });
  });
});

// ---------------------------------------------------------------------------------------------
// docs/devlog/16 MED — admin rate-limit BEFORE auth + admin replay rejection.
// ---------------------------------------------------------------------------------------------
describe("MED: admin rate-limit before auth", () => {
  it("wrong-token attempts ARE throttled (limiter runs BEFORE the token comparison)", async () => {
    // Hammer the admin surface with WRONG tokens from one source IP. Pre-fix the limiter ran only
    // AFTER a successful auth, so every wrong-token attempt returned 401 and was NEVER throttled.
    // Post-fix the per-IP bucket is consumed FIRST, so a wrong-token storm eventually hits 429.
    const ip = "203.0.113.77"; // a dedicated source so other tests' admin calls don't share the bucket
    let saw429 = false;
    let saw401 = false;
    // ADMIN_RATE_PER_MIN = 30 per IP; send > 30 to cross it within the same minute window.
    for (let i = 0; i < 40; i++) {
      const r = await SELF.fetch(`${BASE}/admin/collectors/k1/revoke`, {
        method: "POST",
        headers: {
          Authorization: "Bearer wrong-token",
          "content-type": "application/json",
          "CF-Connecting-IP": ip,
          ...adminReplayHeaders(),
        },
      });
      if (r.status === 401) saw401 = true;
      if (r.status === 429) {
        saw429 = true;
        break;
      }
    }
    expect(saw401).toBe(true); // the first attempts are 401 (wrong token), proving these are unauthed
    expect(saw429).toBe(true); // but the limiter eventually throttles the wrong-token storm (the fix)
  });
});

describe("MED: admin replay rejected", () => {
  it("a replayed admin nonce is rejected (and persists across eviction so a replay can't undo a later revoke)", async () => {
    const kid = "ar1";
    const secret = "admin-replay-secret-ar1";
    const ts = String(Math.trunc(Date.now() / 1000));
    const nonce = freshNonce();

    // 1. First enroll with (ts, nonce) → 200.
    const first = await adminEnroll(`/admin/collectors/${kid}/enroll`, { secret }, ADMIN_TOKEN, { timestamp: ts, nonce });
    expect(first.status).toBe(200);

    // 2. REPLAY the identical admin request (same nonce) → 409 admin_replay (single-use nonce).
    const replay = await adminEnroll(`/admin/collectors/${kid}/enroll`, { secret }, ADMIN_TOKEN, { timestamp: ts, nonce });
    expect(replay.status).toBe(409);
    expect(((await replay.json()) as Record<string, unknown>)["error"]).toBe("admin_replay");

    // 3. The admin nonce store survives an eviction → the replay is STILL rejected after a bootstrap
    //    (so a captured old enroll can't be replayed to undo a later revoke once the DO restarts).
    await simulateEviction();
    const afterEvict = await adminEnroll(`/admin/collectors/${kid}/enroll`, { secret }, ADMIN_TOKEN, { timestamp: ts, nonce });
    expect(afterEvict.status).toBe(409);
    expect(((await afterEvict.json()) as Record<string, unknown>)["error"]).toBe("admin_replay");
  });

  it("a STALE admin timestamp is rejected (±300s freshness)", async () => {
    const stale = String(Math.trunc(Date.now() / 1000) - 1000); // 1000s in the past, well outside ±300s
    const r = await adminPost(`/admin/collectors/k1/revoke`, ADMIN_TOKEN, { timestamp: stale, nonce: freshNonce() });
    expect(r.status).toBe(401);
    expect(((await r.json()) as Record<string, unknown>)["error"]).toBe("admin_stale");
  });

  it("a MISSING admin nonce/timestamp is rejected (the contract is mandatory)", async () => {
    const noNonce = await adminPost(`/admin/collectors/k1/revoke`, ADMIN_TOKEN, { nonce: null });
    expect(noNonce.status).toBe(401);
    const noTs = await adminPost(`/admin/collectors/k1/revoke`, ADMIN_TOKEN, { timestamp: null });
    expect(noTs.status).toBe(401);
    expect(((await noTs.json()) as Record<string, unknown>)["error"]).toBe("admin_stale");
  });
});
