/**
 * relay_do.ts — the RelayDO Durable Object.
 *
 * 🚨 BUILD-SPEC I4: the DO owns ALL revocation-critical + strongly-consistent state:
 *   - nonce store (replay protection),
 *   - idempotency map (retried batch → prior result, not re-applied),
 *   - device/collector registry + revocation (a revoke takes effect IMMEDIATELY),
 *   - materialized frame state (apply validated events),
 *   - build_frame,
 *   - retention-purge + audit via a DO alarm.
 * KV CONFIG holds ONLY non-urgent config/cache (eventually consistent — never revocation).
 *
 * 🚨 BUILD-SPEC I1: the DO VALIDATES sanitized events (validate.ts) and NEVER re-runs the
 * transforms. A poison event is rejected per-event; the rest of the batch still applies.
 *
 * The DO is a singleton ("relay") addressed by the Worker. State is held in memory and mirrored
 * to DO storage (key-value) so it survives eviction; nonces/idempotency carry TTLs and are
 * swept by the alarm. Secrets (collector keys, device tokens) come from env, never committed.
 */
import {
  IDEMPOTENCY_TTL_S,
  MAX_BODY_BYTES,
  MAX_EVENTS_PER_REQUEST,
  NONCE_RE,
  NONCE_TTL_S,
  SHA256_HEX_RE,
  SUPPORTED_SCHEMA_VERSION,
  TIMESTAMP_WINDOW_S,
  KID_RE,
  payloadSha256Hex,
  verify,
} from "./sign";
import {
  applySanitizedEvent,
  buildFrame,
  collectorHeartbeat,
  newStateData,
  setQuota,
  SchemaVersionError,
  type FrameStateData,
} from "./frame";
import { SanitizationError, validateQuotaEvent, validateSanitizedEvent } from "./validate";
import { CA_BUNDLE_CONTENT_TYPE, DEFAULT_CA_BUNDLE, pemLooksValid } from "./ca";

interface VerifyResult {
  ok: boolean;
  reason: string;
  httpStatus: number;
  serverTime: number;
}

interface Env {
  AGENTLAMP_COLLECTOR_KEYS?: string;
  AGENTLAMP_DEVICE_TOKENS?: string;
  RETENTION_DAYS?: string;
  CONFIG?: KVNamespace;
  // Operator-set pinned CA bundle (var/secret). Falls back to KV CONFIG["ca_bundle"] then the
  // embedded DEFAULT_CA_BUNDLE (ca.ts). Served by GET /api/v1/device/:id/cacerts.
  CA_BUNDLE?: string;
  // Admin bearer — the in-Worker /admin gate (index.ts) checks this; the DO's /do/admin/* ops
  // are internal (reached only after the Worker's constant-time bearer gate passes). Declared
  // here so the Env shape is complete and accurate across both modules.
  AGENTLAMP_ADMIN_TOKEN?: string;
}

const RETENTION_DEFAULT_DAYS = 30;
const ALARM_INTERVAL_MS = 60 * 60 * 1000; // hourly purge/audit sweep
const TEXT = new TextEncoder();

function parsePairs(raw: string | undefined): Map<string, string> {
  const out = new Map<string, string>();
  for (const pair of (raw ?? "").split(",")) {
    const trimmed = pair.trim();
    const idx = trimmed.indexOf(":");
    if (idx <= 0) continue;
    const k = trimmed.slice(0, idx).trim();
    const v = trimmed.slice(idx + 1).trim();
    if (k && v) out.set(k, v);
  }
  return out;
}

async function sha256Hex(s: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", TEXT.encode(s));
  const bytes = new Uint8Array(digest);
  let out = "";
  for (let i = 0; i < bytes.length; i++) out += bytes[i]!.toString(16).padStart(2, "0");
  return out;
}

// 🚨 docs/devlog/16 MED: defensive shape validation for a restored "frame_state" — a partially
// written / schema-drifted / hostile stored value must NOT be trusted; bootstrap() falls back to a
// fresh newStateData() if this returns false. Checks the FrameStateData invariant shape (the two
// record maps + the scalar liveness/seq fields) without trusting the contents of each session/quota
// (those are re-validated on apply, never on read).
function isFrameStateData(v: unknown): v is FrameStateData {
  if (v === null || typeof v !== "object" || Array.isArray(v)) return false;
  const o = v as Record<string, unknown>;
  const sessions = o["sessions"];
  const quota = o["quota"];
  if (sessions === null || typeof sessions !== "object" || Array.isArray(sessions)) return false;
  if (quota === null || typeof quota !== "object" || Array.isArray(quota)) return false;
  if (typeof o["last_collector_heartbeat"] !== "number") return false;
  if (typeof o["seq"] !== "number") return false;
  // last_signature is string | null.
  if (o["last_signature"] !== null && typeof o["last_signature"] !== "string") return false;
  return true;
}

// 🚨 docs/devlog/16 MED (admin replay): the admin request contract. An /admin/* request must carry
// X-ACO-Timestamp (decimal epoch seconds, ±300s freshness) + X-ACO-Nonce (lowercase hex, the SAME
// charset as the ingest nonce). The DO reuses the persisted nonce store so a replayed old enroll
// can't undo a later revoke after an eviction. Forwarded by the Worker as x-aco-* headers.
const ADMIN_TIMESTAMP_WINDOW_S = 300;

export class RelayDO {
  private state: DurableObjectState;
  private env: Env;

  // Active collector signing secrets keyed by kid = env-derived ∪ runtime-enrolled (I5). The
  // env base is set in the constructor; bootstrap() unions the persisted runtime-enrolled keys.
  private keys: Map<string, string>;
  // Runtime-enrolled collector secrets ONLY (I5) — persisted to DO storage so adding a computer
  // needs no `wrangler deploy`. Kept separate from env keys so persistence never resurrects a kid
  // that was later dropped from env.
  private enrolledKeys: Map<string, string> = new Map();
  // Device bearer token hashes keyed by device_id (stored hashed at rest) = env ∪ runtime-enrolled.
  private deviceTokenHashes: Map<string, string> = new Map();
  // Env-derived device token hashes ONLY (computed once in bootstrap; the immutable base for the
  // live union). Kept separate so rebuildLiveRegistries is deterministic + async-free.
  private envHashes: Map<string, string> = new Map();
  // Runtime-enrolled device token hashes ONLY (I5) — persisted; same separation rationale.
  private enrolledHashes: Map<string, string> = new Map();
  // Revocation sets — a revoked kid/device is rejected IMMEDIATELY (strong consistency, I4).
  private revokedKids: Set<string> = new Set();
  private revokedDevices: Set<string> = new Set();
  // Replay: nonce -> expiry (epoch s). Idempotency: key -> {exp, value}.
  private nonces: Map<string, number> = new Map();
  private idem: Map<string, { exp: number; value: unknown }> = new Map();
  // Materialized frame state + per-event audit ring (rejections; counts/hashes only).
  private frame: FrameStateData;
  private audit: Array<{ t: number; kind: string; reason: string; ph?: string }> = [];
  private bootstrapped = false;

  constructor(state: DurableObjectState, env: Env) {
    this.state = state;
    this.env = env;
    this.keys = parsePairs(env.AGENTLAMP_COLLECTOR_KEYS);
    this.frame = newStateData(Date.now() / 1000);
  }

  private async bootstrap(): Promise<void> {
    if (this.bootstrapped) return;
    // Hash configured (env) device tokens once into the immutable env base (stored hashed at rest,
    // I4 registry). rebuildLiveRegistries unions this with the runtime-enrolled hashes.
    const tokens = parsePairs(this.env.AGENTLAMP_DEVICE_TOKENS);
    this.envHashes = new Map();
    for (const [deviceId, token] of tokens) {
      this.envHashes.set(deviceId, await sha256Hex(token));
    }
    // I5 runtime enroll: restore the persisted runtime-enrolled registries (a computer/device
    // added via POST /admin/.../enroll survives a DO restart with NO `wrangler deploy`).
    const storedKeys = (await this.state.storage.get<Record<string, string>>("collector_keys")) ?? {};
    this.enrolledKeys = new Map(Object.entries(storedKeys));
    const storedHashes = (await this.state.storage.get<Record<string, string>>("device_token_hashes")) ?? {};
    this.enrolledHashes = new Map(Object.entries(storedHashes));

    // Restore revocation lists from storage (survive eviction; strong consistency).
    const rk = (await this.state.storage.get<string[]>("revoked_kids")) ?? [];
    const rd = (await this.state.storage.get<string[]>("revoked_devices")) ?? [];
    this.revokedKids = new Set(rk);
    this.revokedDevices = new Set(rd);

    // 🚨 docs/devlog/16 HIGH (replay window survives eviction): the nonce + idempotency stores were
    // in-memory ONLY, so a DO eviction reset the replay window — a captured signed batch could
    // replay within ±300s and a retried batch re-apply. Restore both from DO storage (strongly
    // consistent, I4) so the replay window + idempotency record outlive an eviction. Expired entries
    // are swept here on load AND in alarm().
    const now = Date.now() / 1000;
    const storedNonces = (await this.state.storage.get<Record<string, number>>("nonces")) ?? {};
    this.nonces = new Map();
    for (const [n, exp] of Object.entries(storedNonces)) {
      if (typeof exp === "number" && exp > now) this.nonces.set(n, exp);
    }
    const storedIdem = (await this.state.storage.get<Record<string, { exp: number; value: unknown }>>("idem")) ?? {};
    this.idem = new Map();
    for (const [k, rec] of Object.entries(storedIdem)) {
      if (rec && typeof rec.exp === "number" && rec.exp > now) this.idem.set(k, rec);
    }

    // 🚨 docs/devlog/16 MED (frame survives eviction): persistFrame() writes "frame_state" but the
    // DO never restored it — after an eviction the device saw an EMPTY frame until events refilled
    // it. Restore the materialized frame with DEFENSIVE shape validation; fall back to a fresh
    // newStateData() only if the stored value is missing or structurally corrupt.
    const storedFrame = await this.state.storage.get<unknown>("frame_state");
    this.frame = isFrameStateData(storedFrame) ? (storedFrame as FrameStateData) : newStateData(now);

    // Build the live unions (env ∪ runtime-enrolled, minus revoked). A revoke is destructive — a
    // revoked kid/device is truly dead until re-enrolled — so revoked ids are excluded here too.
    this.rebuildLiveRegistries();

    // Schedule the retention/audit alarm if none pending.
    if ((await this.state.storage.getAlarm()) === null) {
      await this.state.storage.setAlarm(Date.now() + ALARM_INTERVAL_MS);
    }
    this.bootstrapped = true;
  }

  // -- live registry unions (I4 + I5) ------------------------------------------------------
  // The live keys/hashes consulted by verifyIngest / authDevice = env-derived ∪ runtime-enrolled,
  // minus the revoked set. Rebuilt on bootstrap and after any enroll/revoke so the in-memory live
  // maps always reflect (env ∪ enrolled) \ revoked.
  private rebuildLiveRegistries(): void {
    this.keys = parsePairs(this.env.AGENTLAMP_COLLECTOR_KEYS);
    for (const [kid, secret] of this.enrolledKeys) this.keys.set(kid, secret);
    for (const kid of this.revokedKids) this.keys.delete(kid);

    const liveHashes = new Map<string, string>(this.envHashes);
    for (const [deviceId, hash] of this.enrolledHashes) liveHashes.set(deviceId, hash);
    for (const deviceId of this.revokedDevices) liveHashes.delete(deviceId);
    this.deviceTokenHashes = liveHashes;
  }

  // -- persisted runtime registries (I5) ---------------------------------------------------
  // Mirror the runtime-enrolled-ONLY maps to DO storage so an enroll survives eviction without a
  // redeploy. Stored as plain objects (DO storage values must be structured-clone-able). Env keys
  // are NEVER persisted (they re-derive from env on every bootstrap), so dropping a kid from env
  // can't be resurrected by storage.
  private async persistEnrolledKeys(): Promise<void> {
    await this.state.storage.put("collector_keys", Object.fromEntries(this.enrolledKeys));
  }

  private async persistEnrolledHashes(): Promise<void> {
    await this.state.storage.put("device_token_hashes", Object.fromEntries(this.enrolledHashes));
  }

  // -- HMAC + replay + idempotency verify (request-level) ----------------------------------
  private verifyIngest(args: {
    collectorId: string;
    method: string;
    path: string;
    rawLen: number;
    kid: string;
    timestamp: string;
    nonce: string;
    payloadSha256: string;
  }, bodyHashOk: boolean, sigOk: boolean, now: number): VerifyResult {
    const nowI = Math.trunc(now);
    // 1. charset BEFORE signature (canonical-string ambiguity/injection).
    if (!KID_RE.test(args.kid || "")) return { ok: false, reason: "bad_signature", httpStatus: 401, serverTime: nowI };
    if (!NONCE_RE.test(args.nonce || "")) return { ok: false, reason: "bad_signature", httpStatus: 401, serverTime: nowI };
    if (!SHA256_HEX_RE.test(args.payloadSha256 || "")) return { ok: false, reason: "payload_hash_mismatch", httpStatus: 400, serverTime: nowI };
    // 2. body size + payload hash (over exact raw bytes; computed by caller).
    if (args.rawLen > MAX_BODY_BYTES) return { ok: false, reason: "body_too_large", httpStatus: 413, serverTime: nowI };
    if (!bodyHashOk) return { ok: false, reason: "payload_hash_mismatch", httpStatus: 400, serverTime: nowI };
    // 3. signature against the active secret for this kid (revoked/unknown → 403, no oracle).
    if (!this.keys.has(args.kid) || this.revokedKids.has(args.kid)) {
      return { ok: false, reason: "collector_revoked", httpStatus: 403, serverTime: nowI };
    }
    if (!sigOk) return { ok: false, reason: "bad_signature", httpStatus: 401, serverTime: nowI };
    // 4. timestamp window. 🚨 docs/devlog/16 LOW (parity): Number.parseInt("12345x", 10) === 12345
    //    (it stops at the first non-digit), but Python int("12345x") RAISES → rejected. A signed
    //    timestamp with a trailing-garbage suffix must NOT slip through the freshness window with a
    //    silently-truncated value. Require a STRICT decimal integer string before parsing (the
    //    canonical string signs the raw timestamp bytes, so the value is attacker-fixed once signed,
    //    but charset strictness keeps TS↔Python decisions identical).
    if (!/^\d+$/.test(args.timestamp)) return { ok: false, reason: "stale_timestamp", httpStatus: 401, serverTime: nowI };
    const ts = Number.parseInt(args.timestamp, 10);
    if (!Number.isFinite(ts)) return { ok: false, reason: "stale_timestamp", httpStatus: 401, serverTime: nowI };
    if (Math.abs(nowI - ts) > TIMESTAMP_WINDOW_S) return { ok: false, reason: "stale_timestamp", httpStatus: 401, serverTime: nowI };
    // 5. nonce replay.
    this.sweepNonces(now);
    const exp = this.nonces.get(args.nonce);
    if (exp !== undefined && exp > now) return { ok: false, reason: "reused_nonce", httpStatus: 409, serverTime: nowI };
    this.nonces.set(args.nonce, now + NONCE_TTL_S);
    return { ok: true, reason: "", httpStatus: 200, serverTime: nowI };
  }

  private sweepNonces(now: number): void {
    if (this.nonces.size > 4096) {
      for (const [k, e] of this.nonces) if (e <= now) this.nonces.delete(k);
    }
  }

  private idemGet(key: string, now: number): unknown | null {
    const rec = this.idem.get(key);
    if (rec === undefined) return null;
    if (rec.exp <= now) {
      this.idem.delete(key);
      return null;
    }
    return rec.value;
  }

  private idemPut(key: string, value: unknown, now: number): void {
    if (this.idem.size > 8192) {
      for (const [k, rec] of this.idem) if (rec.exp <= now) this.idem.delete(k);
    }
    this.idem.set(key, { exp: now + IDEMPOTENCY_TTL_S, value });
  }

  // -- per-event apply (validate-only gate, I1) --------------------------------------------
  private ingestEventToEnvelope(ev: Record<string, unknown>): Record<string, unknown> {
    const payload: Record<string, unknown> = { ...((ev["payload"] as Record<string, unknown>) ?? {}) };
    if (ev["account_alias"] !== undefined && payload["account_alias"] === undefined) {
      payload["account_alias"] = ev["account_alias"];
    }
    // session_id lives at envelope level (provider_session_id), not payload.
    const psid = payload["session_id"];
    delete payload["session_id"];
    return {
      schema_version: ev["schema_version"] ?? 1,
      provider: ev["provider"] ?? "manual",
      provider_event_name: ev["provider_event_name"] ?? null,
      provider_session_id: psid ?? null,
      event_time: ev["event_time"] ?? null,
      payload,
      sanitization: { policy_version: 1 },
    };
  }

  private applyIngestEvent(ev: unknown, now: number): { event_id: string; status: string; reason?: string } {
    if (ev === null || typeof ev !== "object" || Array.isArray(ev)) {
      return { event_id: "", status: "rejected", reason: "event_not_object" };
    }
    const e = ev as Record<string, unknown>;
    const eid = String(e["event_id"] ?? "");
    const etype = String(e["event_type"] ?? "");
    try {
      if (etype === "collector.heartbeat") {
        collectorHeartbeat(this.frame, now);
        return { event_id: eid, status: "accepted" };
      }
      if (etype === "quota.window") {
        // CRITICAL (docs/devlog/16 I1): setQuota writes account_alias + provider straight into
        // the materialized frame (frame.quota[].account) served to the device, so the quota
        // branch MUST pass the SAME independent VALIDATE gate as session.* — previously it called
        // setQuota DIRECTLY with attacker-controlled values (and Number(...)→NaN flowed through
        // where Python rejected). validateQuotaEvent rejects (never coerces) any non-canonical
        // value, including a non-finite used_ratio (the NaN divergence FIX).
        const q = validateQuotaEvent(e);
        setQuota(this.frame, q, now);
        return { event_id: eid, status: "accepted" };
      }
      // session.* / alert.* / unknown → the INDEPENDENT validate-only gate (I1), then apply.
      const envelope = this.ingestEventToEnvelope(e);
      const clean = validateSanitizedEvent(envelope);
      applySanitizedEvent(this.frame, clean, now);
      return { event_id: eid, status: "accepted" };
    } catch (err) {
      if (err instanceof SanitizationError) {
        this.recordAudit(now, "ingest_reject", err.reason, err.payloadHash);
        return { event_id: eid, status: "rejected", reason: err.reason };
      }
      const name = err instanceof Error ? err.constructor.name : "Error";
      return { event_id: eid, status: "rejected", reason: `bad_event:${name}` };
    }
  }

  private recordAudit(now: number, kind: string, reason: string, ph?: string): void {
    this.audit.push({ t: Math.trunc(now), kind, reason, ...(ph ? { ph } : {}) });
    if (this.audit.length > 500) this.audit.splice(0, this.audit.length - 500);
  }

  // -- admin replay protection (docs/devlog/16 MED) ----------------------------------------
  // 🚨 The /admin/* surface was bearer-only with NO freshness, so a replayed OLD enroll could undo
  // a LATER revoke (the bearer alone proves authorization, not recency). Every admin op now requires
  // a fresh X-ACO-Timestamp (±300s) + a single-use X-ACO-Nonce, REUSING the persisted nonce store —
  // so the freshness window survives an eviction too. Returns a denial Response, or null (proceed)
  // after recording + persisting the nonce. ADMIN REQUEST CONTRACT:
  //   X-ACO-Timestamp: <decimal epoch seconds, ±300s of server time>
  //   X-ACO-Nonce:     <lowercase hex, [0-9a-f]{16,128}, single-use>
  private async checkAdminReplay(request: Request, now: number): Promise<Response | null> {
    const nowI = Math.trunc(now);
    const timestamp = request.headers.get("x-aco-timestamp") ?? "";
    const nonce = (request.headers.get("x-aco-nonce") ?? "").toLowerCase();
    // Strict decimal timestamp (same parity discipline as ingest, item 5).
    if (!/^\d+$/.test(timestamp)) {
      return Response.json({ error: "admin_stale", retry: false, server_time: nowI }, { status: 401 });
    }
    const ts = Number.parseInt(timestamp, 10);
    if (!Number.isFinite(ts) || Math.abs(nowI - ts) > ADMIN_TIMESTAMP_WINDOW_S) {
      return Response.json({ error: "admin_stale", retry: false, server_time: nowI }, { status: 401 });
    }
    // Nonce charset (reuse the ingest nonce shape) + single-use against the PERSISTED nonce store.
    if (!NONCE_RE.test(nonce)) {
      return Response.json({ error: "admin_bad_nonce", retry: false, server_time: nowI }, { status: 401 });
    }
    this.sweepNonces(now);
    const exp = this.nonces.get(nonce);
    if (exp !== undefined && exp > now) {
      return Response.json({ error: "admin_replay", retry: false, server_time: nowI }, { status: 409 });
    }
    this.nonces.set(nonce, now + NONCE_TTL_S);
    await this.persistNonces();
    return null;
  }

  // -- HTTP surface (Worker forwards here via fetch) ---------------------------------------
  async fetch(request: Request): Promise<Response> {
    await this.bootstrap();
    const url = new URL(request.url);
    const op = url.pathname; // internal op path set by the Worker

    if (op === "/do/ingest" && request.method === "POST") return this.opIngest(request);
    if (op === "/do/frame" && request.method === "GET") return this.opFrame(request);
    if (op === "/do/cacerts" && request.method === "GET") return this.opCacerts(request);
    if (op === "/do/admin/revoke-kid" && request.method === "POST") return this.opRevokeKid(request);
    if (op === "/do/admin/revoke-device" && request.method === "POST") return this.opRevokeDevice(request);
    if (op === "/do/admin/enroll-collector" && request.method === "POST") return this.opEnrollCollector(request);
    if (op === "/do/admin/enroll-device" && request.method === "POST") return this.opEnrollDevice(request);
    if (op === "/do/admin/audit" && request.method === "GET") {
      return Response.json({ audit: this.audit.slice(-100) });
    }
    return Response.json({ error: "not_found", retry: false }, { status: 404 });
  }

  private async opIngest(request: Request): Promise<Response> {
    const now = Date.now() / 1000;
    const collectorId = request.headers.get("x-aco-collector-id") ?? "";
    const kid = request.headers.get("x-aco-key-id") ?? "";
    const timestamp = request.headers.get("x-aco-timestamp") ?? "";
    const nonce = request.headers.get("x-aco-nonce") ?? "";
    const payloadSha256 = (request.headers.get("x-aco-payload-sha256") ?? "").toLowerCase();
    let signature = request.headers.get("x-aco-signature") ?? "";
    if (signature.toLowerCase().startsWith("v1=")) signature = signature.slice(3);
    const idem = (request.headers.get("idempotency-key") ?? "").trim();
    const path = request.headers.get("x-aco-path") ?? `/api/v1/collectors/${collectorId}/events`;

    const raw = new Uint8Array(await request.arrayBuffer());

    // Compute payload hash + signature for the verifier (crypto is async, so do it before).
    let bodyHashOk = false;
    if (SHA256_HEX_RE.test(payloadSha256) && raw.length <= MAX_BODY_BYTES) {
      const actual = await payloadSha256Hex(raw);
      bodyHashOk = actual === payloadSha256;
    }
    let sigOk = false;
    const secret = this.keys.get(kid);
    if (secret !== undefined && !this.revokedKids.has(kid)) {
      sigOk = await verify(
        TEXT.encode(secret),
        { method: "POST", path, kid, timestamp, nonce, payloadSha256 },
        signature,
      );
    }

    const v = this.verifyIngest(
      { collectorId, method: "POST", path, rawLen: raw.length, kid, timestamp, nonce, payloadSha256 },
      bodyHashOk,
      sigOk,
      now,
    );
    if (!v.ok) {
      return Response.json({ ok: false, reason: v.reason, server_time: v.serverTime }, { status: v.httpStatus });
    }
    // The nonce was just recorded by verifyIngest (in-memory). Persist the replay window NOW —
    // before any apply — so an eviction immediately after this request still rejects a replay of
    // this exact signed batch (docs/devlog/16 HIGH). A fresh-nonce retry is a different nonce, so
    // the replay gate stays correct while idempotency (below) handles the legit retry.
    await this.persistNonces();

    // Idempotency: a retried batch (fresh nonce, same key) returns the prior result.
    if (idem) {
      const prior = this.idemGet(idem, now);
      if (prior !== null) {
        return Response.json({ ...(prior as Record<string, unknown>), duplicate: true });
      }
    }

    let body: Record<string, unknown>;
    try {
      body = JSON.parse(new TextDecoder().decode(raw) || "{}") as Record<string, unknown>;
    } catch {
      return Response.json({ ok: false, reason: "bad_json", server_time: v.serverTime }, { status: 400 });
    }
    if (Number.parseInt(String(body["schema_version"] ?? 1), 10) !== SUPPORTED_SCHEMA_VERSION) {
      return Response.json({ ok: false, reason: "schema_version_unsupported", server_time: v.serverTime }, { status: 400 });
    }
    const events = body["events"];
    if (!Array.isArray(events)) {
      return Response.json({ ok: false, reason: "bad_events", server_time: v.serverTime }, { status: 400 });
    }
    if (events.length > MAX_EVENTS_PER_REQUEST) {
      return Response.json({ ok: false, reason: "batch_too_large", server_time: v.serverTime }, { status: 413 });
    }

    const results = events.map((ev) => this.applyIngestEvent(ev, now));
    const accepted = results.filter((r) => r.status === "accepted").length;
    const rejected = results.filter((r) => r.status === "rejected").length;
    const resp: Record<string, unknown> = {
      ok: true,
      server_time: v.serverTime,
      ingest_id: "ing_" + crypto.randomUUID().replace(/-/g, "").slice(0, 16),
      accepted,
      duplicates: 0,
      rejected,
      results,
    };
    if (idem) {
      this.idemPut(idem, resp, now);
      await this.persistIdem(); // survive eviction: a retried batch re-applies otherwise (HIGH).
    }
    await this.persistFrame();
    return Response.json(resp);
  }

  // Shared device-bearer auth (same precedence for /frame + /cacerts, so revocation is uniform):
  // revoked (403) → unknown (404) → bad token (401). Returns null iff the token is valid.
  private async authDevice(deviceId: string, token: string): Promise<Response | null> {
    const stored = this.deviceTokenHashes.get(deviceId);
    if (this.revokedDevices.has(deviceId)) {
      return Response.json({ error: "device_revoked", retry: false }, { status: 403 });
    }
    if (stored === undefined) {
      return Response.json({ error: "unknown_device", retry: false }, { status: 404 });
    }
    const presented = token ? await sha256Hex(token) : "";
    if (presented !== stored) {
      return Response.json({ error: "bad_token", retry: false }, { status: 401 });
    }
    return null;
  }

  private async opFrame(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const deviceId = url.searchParams.get("device_id") ?? "";
    const token = request.headers.get("x-device-token") ?? "";
    const denied = await this.authDevice(deviceId, token);
    if (denied !== null) return denied;
    const requested = Number.parseInt(url.searchParams.get("schema_version") ?? "1", 10);
    const now = Date.now() / 1000;
    try {
      const frame = buildFrame(this.frame, deviceId, now, Number.isFinite(requested) ? requested : 1);
      return Response.json(frame, { headers: { "X-Frame-Schema-Version": String(frame["v"]) } });
    } catch (err) {
      if (err instanceof SchemaVersionError) {
        return Response.json({ error: "frame_unavailable", retry: true }, { status: 503 });
      }
      return Response.json({ error: "frame_unavailable", retry: true }, { status: 503 });
    }
  }

  // GET /do/cacerts — return the current pinned ROOT CA bundle as application/x-pem-file.
  // Bearer-authed identically to /frame (revocation applies, I4: a revoked/unknown device can NOT
  // pull a fresh trust anchor). Source precedence: env CA_BUNDLE → KV CONFIG["ca_bundle"] →
  // embedded DEFAULT_CA_BUNDLE (ca.ts). A configured-but-malformed bundle (no BEGIN/END markers)
  // is REFUSED and falls over to the default, so a misconfig can never brick the device with
  // garbage (firmware pemLooksValid would reject it anyway and lose the refresh).
  private async opCacerts(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const deviceId = url.searchParams.get("device_id") ?? "";
    const token = request.headers.get("x-device-token") ?? "";
    const denied = await this.authDevice(deviceId, token);
    if (denied !== null) return denied;

    let bundle = this.env.CA_BUNDLE;
    if (!pemLooksValid(bundle) && this.env.CONFIG !== undefined) {
      const fromKv = await this.env.CONFIG.get("ca_bundle");
      if (pemLooksValid(fromKv)) bundle = fromKv ?? undefined;
    }
    if (!pemLooksValid(bundle)) bundle = DEFAULT_CA_BUNDLE;

    return new Response(bundle, {
      status: 200,
      headers: { "content-type": CA_BUNDLE_CONTENT_TYPE, "cache-control": "no-store" },
    });
  }

  private async opRevokeKid(request: Request): Promise<Response> {
    const now = Date.now() / 1000;
    const replay = await this.checkAdminReplay(request, now);
    if (replay !== null) return replay;
    const { kid } = (await request.json()) as { kid?: string };
    if (!kid) return Response.json({ error: "bad_request" }, { status: 400 });
    // Destructive revoke (I4): add to the revoked set AND remove the live + runtime-enrolled key,
    // so the kid is TRULY dead (not merely shadowed by the set) until it is re-enrolled.
    this.revokedKids.add(kid);
    this.enrolledKeys.delete(kid);
    this.keys.delete(kid);
    await this.state.storage.put("revoked_kids", [...this.revokedKids]);
    await this.persistEnrolledKeys();
    this.recordAudit(Date.now() / 1000, "revoke_kid", kid);
    return Response.json({ ok: true, revoked: kid });
  }

  private async opRevokeDevice(request: Request): Promise<Response> {
    const now = Date.now() / 1000;
    const replay = await this.checkAdminReplay(request, now);
    if (replay !== null) return replay;
    const { device_id } = (await request.json()) as { device_id?: string };
    if (!device_id) return Response.json({ error: "bad_request" }, { status: 400 });
    // Destructive revoke (I4): add to the revoked set AND remove the live + runtime-enrolled hash.
    this.revokedDevices.add(device_id);
    this.enrolledHashes.delete(device_id);
    this.deviceTokenHashes.delete(device_id);
    await this.state.storage.put("revoked_devices", [...this.revokedDevices]);
    await this.persistEnrolledHashes();
    this.recordAudit(Date.now() / 1000, "revoke_device", device_id);
    return Response.json({ ok: true, revoked: device_id });
  }

  // POST /do/admin/enroll-collector — runtime-enroll a collector kid (I5: add a computer in one
  // line, NO `wrangler deploy`). Persists kid->secret to DO storage, clears the kid from the
  // revoked set, and rebuilds the live key union so a SUBSEQUENT signed request from it is
  // accepted at once. Re-enroll after a revoke succeeds (revokedKids.delete).
  private async opEnrollCollector(request: Request): Promise<Response> {
    const now = Date.now() / 1000;
    const replay = await this.checkAdminReplay(request, now);
    if (replay !== null) return replay;
    const { kid, secret } = (await request.json()) as { kid?: string; secret?: string };
    if (!kid || !secret) return Response.json({ error: "bad_request" }, { status: 400 });
    this.enrolledKeys.set(kid, secret);
    this.revokedKids.delete(kid);
    this.rebuildLiveRegistries();
    await this.persistEnrolledKeys();
    await this.state.storage.put("revoked_kids", [...this.revokedKids]);
    this.recordAudit(Date.now() / 1000, "enroll_collector", kid);
    return Response.json({ ok: true, kid });
  }

  // POST /do/admin/enroll-device — runtime-enroll a device token (I5). PERSISTS the token HASH to
  // DO storage (never the raw token at rest), clears the device from the revoked set, and rebuilds
  // the live hash union so a SUBSEQUENT /frame with it works. Re-enroll after revoke succeeds.
  private async opEnrollDevice(request: Request): Promise<Response> {
    const now = Date.now() / 1000;
    const replay = await this.checkAdminReplay(request, now);
    if (replay !== null) return replay;
    const { device_id, token } = (await request.json()) as { device_id?: string; token?: string };
    if (!device_id || !token) return Response.json({ error: "bad_request" }, { status: 400 });
    this.enrolledHashes.set(device_id, await sha256Hex(token));
    this.revokedDevices.delete(device_id);
    this.rebuildLiveRegistries();
    await this.persistEnrolledHashes();
    await this.state.storage.put("revoked_devices", [...this.revokedDevices]);
    this.recordAudit(Date.now() / 1000, "enroll_device", device_id);
    return Response.json({ ok: true, device_id });
  }

  private async persistFrame(): Promise<void> {
    // Mirror materialized state to DO storage so it survives eviction (strongly consistent).
    await this.state.storage.put("frame_state", this.frame);
  }

  // 🚨 docs/devlog/16 HIGH: persist the replay window + idempotency records so an eviction can't
  // reset them (a captured signed batch must NOT replay within ±300s after a restart; a retried
  // batch must still return the prior result, not re-apply). Stored as plain objects (DO storage
  // values must be structured-clone-able). The maps are small (nonce TTL 720s, idem 7d, both
  // size-capped + alarm-swept), so a full rewrite is cheap.
  private async persistNonces(): Promise<void> {
    await this.state.storage.put("nonces", Object.fromEntries(this.nonces));
  }

  private async persistIdem(): Promise<void> {
    await this.state.storage.put("idem", Object.fromEntries(this.idem));
  }

  // -- retention purge + audit (DO alarm) --------------------------------------------------
  async alarm(): Promise<void> {
    await this.bootstrap();
    const now = Date.now() / 1000;
    const retentionDays = Number.parseInt(this.env.RETENTION_DAYS ?? String(RETENTION_DEFAULT_DAYS), 10) || RETENTION_DEFAULT_DAYS;
    const cutoff = now - retentionDays * 24 * 3600;

    // Purge sanitized sessions/quota older than retention (only materialized state kept).
    let purged = 0;
    for (const [k, s] of Object.entries(this.frame.sessions)) {
      if (s.updated_at < cutoff) {
        delete this.frame.sessions[k];
        purged++;
      }
    }
    for (const [k, q] of Object.entries(this.frame.quota)) {
      if (q.updated_at < cutoff) {
        delete this.frame.quota[k];
        purged++;
      }
    }
    // Sweep expired nonces + idempotency keys, then re-persist the pruned stores so the on-disk
    // copy doesn't grow unbounded across evictions (docs/devlog/16 HIGH: persist + alarm-sweep).
    for (const [k, e] of this.nonces) if (e <= now) this.nonces.delete(k);
    for (const [k, rec] of this.idem) if (rec.exp <= now) this.idem.delete(k);

    this.recordAudit(now, "retention_purge", `purged=${purged} cutoff=${Math.trunc(cutoff)}`);
    await this.persistFrame();
    await this.persistNonces();
    await this.persistIdem();
    // Reschedule the next sweep.
    await this.state.storage.setAlarm(Date.now() + ALARM_INTERVAL_MS);
  }
}
