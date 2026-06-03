/**
 * validate.ts — the cloud VALIDATE-ONLY gate (relay mode). Mirrors
 * server/agentlamp_server/validate.py EXACTLY.
 *
 * 🚨 BUILD-SPEC I1: this NEVER re-runs the sanitizer TRANSFORMS. The Python collector is the
 * ONLY place raw->safe heuristic redaction happens. This gate only VALIDATES the
 * already-sanitized output and REJECTS (never coerces) anything non-canonical:
 *
 *   1. key allowlist + forbidden-key reject (envelope + payload),
 *   2. forbidden-pattern reject scan over EVERY leaf (the backstop),
 *   3. provider mandatory + enum,
 *   4. typed enum membership (validate-if-present; absence is not a leak),
 *   5. needs_attention is bool if present,
 *   6. alias-shape fields positively match the neutral display shape (relay strict).
 *
 * Verified against tests/fixtures/parity/sanitize_corpus.json (every accept/reject decision).
 * The forbidden-pattern + alias-shape regexes come from policy.json (generated from
 * sanitize.py) — not hand-retyped. We re-author the *scan control flow* in TS (the spec's I1
 * trade: validating enum-only output is language-equivalent; re-deriving transforms is not).
 */
import {
  ALIAS_MAX_LEN,
  CONFIDENCE_ENUM,
  ERROR_LABEL_ENUM,
  FORBIDDEN_KEYS,
  FORBIDDEN_PATTERN_ENTRIES,
  MODEL_ENUM,
  MODEL_ID_IGNORECASE,
  PLAN_TIERS,
  POLICY,
  PROVIDER_ENUM,
  SESSION_HAS_DIGIT_RE,
  SESSION_HMAC_RE,
  SESSION_ID_MAX_LEN,
  SESSION_TOKEN_RE,
  SESSION_WHITESPACE_RE,
  STATUS_DETAIL_ENUM,
  STATUS_ENUM,
  TASK_LABEL_ENUM,
  TOOL_CATEGORY_ENUM,
  VALIDATE_ENVELOPE_KEYS,
  VALIDATE_PAYLOAD_KEYS,
} from "./policy";

/** Rejection carries metadata only (reason + payload hash) — never the offending value. */
export class SanitizationError extends Error {
  reason: string;
  payloadHash: string;
  constructor(reason: string, payloadHash = "") {
    super(reason);
    this.name = "SanitizationError";
    this.reason = reason;
    this.payloadHash = payloadHash;
  }
}

// enum field -> allowed set (validate-if-present). status upper-cases, the rest lower.
const ENUM_FIELDS: Array<[string, Set<string>]> = [
  ["status", STATUS_ENUM],
  ["tool_category", TOOL_CATEGORY_ENUM],
  ["status_detail", STATUS_DETAIL_ENUM],
  ["task_label", TASK_LABEL_ENUM],
  ["model", MODEL_ENUM],
  ["error_label", ERROR_LABEL_ENUM],
  ["confidence", CONFIDENCE_ENUM],
];

const ALIAS_FIELDS = ["project_alias", "account_alias", "display_title"] as const;

// Quota windows the device renders (device_frame_api.md → Frame Schema v1). Mirrors
// validate.py._QUOTA_WINDOW_TYPES.
const QUOTA_WINDOW_TYPES = new Set(["5h", "week"]);

// --- forbidden-pattern scan (mirrors sanitize.py.contains_forbidden) ----------------------
// Built from policy.json sources. The first matching pattern's name is the reject reason,
// in the SAME order sanitize.py reports them (forbidden:<pat24> | plan_tier:<t> | model_id |
// code_density). The reason strings match the corpus (e.g. "forbidden:/Users/").
//
// 🚨 I1/I2: each entry is `{pattern, ignorecase}` (the generator now ships per-entry flags).
// We MUST honour `ignorecase` — sanitize.py compiles `\bBearer\s` / `\bCookie:` / `\bsha256[(:]`
// with `re.IGNORECASE`; dropping the flag here silently lets lowercase `bearer`/`cookie:` leaks
// through (the verifier-found CRITICAL). `name` mirrors `forbidden:{pat.pattern[:24]}` (the regex
// SOURCE, not a stringified object), so reason strings stay corpus-exact.
const FORBIDDEN_PATTERNS: Array<{ re: RegExp; name: string }> = FORBIDDEN_PATTERN_ENTRIES.map(
  (p) => ({
    re: new RegExp(p.pattern, p.ignorecase ? "i" : ""),
    name: `forbidden:${p.pattern.slice(0, 24)}`,
  }),
);
// The model-id regex honours its own ignorecase flag (mirrors re.IGNORECASE on _MODEL_ID_RE).
const MODEL_ID_RE = new RegExp(POLICY.model_id_regex, MODEL_ID_IGNORECASE ? "i" : "");
const CODE_DENSITY_RE = /[{};]/g;
const ALIAS_SHAPE_RE = new RegExp(POLICY.alias_shape_regex);

/** NFKC-fold + drop zero-width / control / format chars (Unicode Cc, Cf). Mirrors
 * sanitize.py._strip_invisibles — used for SCANNING only. */
function stripInvisibles(value: string): string {
  const folded = value.normalize("NFKC");
  let out = "";
  for (const ch of folded) {
    // \p{Cc} = control, \p{Cf} = format. Drop both (matches Python categories Cc/Cf).
    if (/\p{Cc}|\p{Cf}/u.test(ch)) continue;
    out += ch;
  }
  return out;
}

function forbiddenIn(value: string): string | null {
  for (const { re, name } of FORBIDDEN_PATTERNS) {
    if (re.test(value)) return name;
  }
  const low = value.toLowerCase();
  for (const tier of PLAN_TIERS) {
    // \b<tier>\b standalone-word (matches sanitize.py's `re.search(rf"\b{tier}\b", low)`).
    if (new RegExp(`\\b${tier}\\b`).test(low)) return `plan_tier:${tier}`;
  }
  if (MODEL_ID_RE.test(value)) return "model_id";
  const braces = value.match(CODE_DENSITY_RE);
  if ((braces ? braces.length : 0) >= 2 || value.includes("\n")) return "code_density";
  return null;
}

/** Return the first forbidden pattern matched, else null. Scans BOTH raw + invisibles-stripped
 * (a leak hidden behind a zero-width / control char can't slip past adjacency patterns). */
export function containsForbidden(value: string): string | null {
  const hit = forbiddenIn(value);
  if (hit !== null) return hit;
  const stripped = stripInvisibles(value);
  if (stripped !== value) return forbiddenIn(stripped);
  return null;
}

/** True iff value positively matches the neutral display-alias shape (max-len + regex). */
export function looksLikeNeutralAlias(value: string): boolean {
  if (!value || value.length > ALIAS_MAX_LEN) return false;
  return ALIAS_SHAPE_RE.test(value);
}

/** True iff value is a canonical opaque session id — the collector's `hmac:<hex>` label, OR a
 * high-entropy url-safe token (≥16 chars, contains a digit, no whitespace). Mirrors
 * sanitize.py.looks_like_session_id byte-for-byte (regexes sourced from policy.json, I2). Rejects
 * free text used as a session KEY (`please fix auth now` / `fix-the-login-bug`). Default-deny: a
 * non-opaque id means a buggy/hostile collector; the event is dropped. */
export function looksLikeSessionId(value: string): boolean {
  if (!value || value.length > SESSION_ID_MAX_LEN) return false;
  if (SESSION_WHITESPACE_RE.test(value)) return false;
  if (SESSION_HMAC_RE.test(value)) return true;
  return SESSION_TOKEN_RE.test(value) && SESSION_HAS_DIGIT_RE.test(value);
}

// Stable-ish payload hash for rejection audit (counts, never the value). Mirrors the SHAPE of
// sanitize.py.payload_hash (sha256(repr(obj))[:16]); the exact bytes are not asserted (Python
// repr ≠ JS), so this is audit-only metadata. Synchronous FNV-1a over a JSON view.
export function payloadHash(obj: unknown): string {
  let json: string;
  try {
    json = JSON.stringify(obj);
  } catch {
    json = String(obj);
  }
  let h1 = 0x811c9dc5;
  let h2 = 0x01000193;
  for (let i = 0; i < json.length; i++) {
    const c = json.charCodeAt(i);
    h1 = (h1 ^ c) >>> 0;
    h1 = (h1 * 0x01000193) >>> 0;
    h2 = (h2 ^ ((c << 3) | (c >> 5))) >>> 0;
    h2 = (h2 * 0x85ebca6b) >>> 0;
  }
  return (h1.toString(16).padStart(8, "0") + h2.toString(16).padStart(8, "0")).slice(0, 16);
}

function scanLeaves(obj: unknown, ph: string): void {
  if (typeof obj === "string") {
    const hit = containsForbidden(obj);
    if (hit !== null) throw new SanitizationError(hit, ph);
    return;
  }
  if (Array.isArray(obj)) {
    for (const v of obj) scanLeaves(v, ph);
    return;
  }
  if (obj !== null && typeof obj === "object") {
    for (const v of Object.values(obj as Record<string, unknown>)) scanLeaves(v, ph);
  }
}

function rejectKeys(obj: Record<string, unknown>, known: Set<string>, where: string, ph: string): void {
  // Default-deny: any forbidden raw-leak key, or any key outside `known`, rejects.
  for (const key of Object.keys(obj)) {
    if (FORBIDDEN_KEYS.has(key)) throw new SanitizationError(`forbidden_key:${where}.${key}`, ph);
    if (!known.has(key)) throw new SanitizationError(`unknown_field:${where}.${key}`, ph);
  }
}

/**
 * Validate an already-sanitized event envelope. Returns it unchanged, or throws
 * SanitizationError (metadata only) on the FIRST violation. Pure validation — no transform.
 * Mirrors validate.py.validate_sanitized_event step-for-step (so the first-violation ORDER
 * matches the corpus reason strings).
 */
export function validateSanitizedEvent(event: unknown): Record<string, unknown> {
  const ph = payloadHash(event);
  if (event === null || typeof event !== "object" || Array.isArray(event)) {
    throw new SanitizationError("event_not_object", ph);
  }
  const ev = event as Record<string, unknown>;

  // 1. key allowlist + forbidden-key reject.
  rejectKeys(ev, VALIDATE_ENVELOPE_KEYS, "event", ph);
  let payload = ev["payload"];
  if (payload === undefined || payload === null) payload = {};
  if (typeof payload !== "object" || Array.isArray(payload)) {
    throw new SanitizationError("payload_not_object", ph);
  }
  const p = payload as Record<string, unknown>;
  rejectKeys(p, VALIDATE_PAYLOAD_KEYS, "payload", ph);

  // 2. forbidden-pattern reject scan over EVERY leaf (here `model` is already an enum and
  //    `display_title` is already an hmac/neutral label, so nothing is exempt — stricter than
  //    the collector's input scan, which exempts the two pre-transform fields).
  const envView: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(ev)) if (k !== "payload") envView[k] = v;
  scanLeaves(envView, ph);
  scanLeaves(p, ph);

  // 3. provider mandatory + enum.
  const provider = String(ev["provider"] ?? "").trim().toLowerCase();
  if (!PROVIDER_ENUM.has(provider)) throw new SanitizationError("provider_not_in_enum", ph);

  // 4. typed enum membership (validate-if-present, strict — no coercion).
  for (const [f, allowed] of ENUM_FIELDS) {
    const val = p[f];
    if (val !== undefined && val !== null) {
      const s = String(val).trim();
      const cmp = f === "status" ? s.toUpperCase() : s.toLowerCase();
      if (!allowed.has(cmp)) throw new SanitizationError(`enum:${f}`, ph);
    }
  }

  // 5. needs_attention is bool if present.
  if ("needs_attention" in p && typeof p["needs_attention"] !== "boolean") {
    throw new SanitizationError("needs_attention_not_bool", ph);
  }

  // 6. alias-shape fields: positively neutral (relay strict).
  for (const f of ALIAS_FIELDS) {
    const val = p[f];
    if (val !== undefined && val !== null && !looksLikeNeutralAlias(String(val))) {
      throw new SanitizationError(`alias_shape:${f}`, ph);
    }
  }

  // 7. provider_session_id shape gate (2026-06-03 hardening; mirrors validate.py step 7). It is
  //    leaf-scanned (step 2) but a forbidden-pattern-clean free-text string ("please fix auth now")
  //    would otherwise survive as the stored SESSION KEY. Require the canonical opaque shape
  //    (`hmac:<hex>` or a high-entropy token) — reject, never coerce, a non-canonical id.
  const psid = ev["provider_session_id"];
  if (psid !== undefined && psid !== null && !looksLikeSessionId(String(psid))) {
    throw new SanitizationError("session_id_shape", ph);
  }

  return ev;
}

/** Python `a or b or ...` chain: return the FIRST truthy arg (Python truthiness), else the last.
 * Falsy = null/undefined, "", 0/-0, NaN, false, empty array/object — mirroring CPython so the TS
 * quota fallback semantics match `str(x or y or "")` exactly (I2). The last arg is the default. */
function pyOr(...vals: unknown[]): unknown {
  for (let i = 0; i < vals.length - 1; i++) {
    if (pyTruthy(vals[i])) return vals[i];
  }
  return vals[vals.length - 1];
}

function pyTruthy(v: unknown): boolean {
  if (v === null || v === undefined || v === false) return false;
  if (typeof v === "string") return v.length > 0;
  if (typeof v === "number") return v !== 0 && !Number.isNaN(v);
  if (typeof v === "boolean") return v;
  if (Array.isArray(v)) return v.length > 0;
  if (typeof v === "object") return Object.keys(v as object).length > 0;
  return true;
}

export interface ValidatedQuota {
  provider: string;
  account_alias: string;
  window_type: string;
  used_ratio: number;
  confidence: string;
  is_estimated: boolean;
}

/**
 * Validate a `quota.window` ingest event BEFORE it reaches `setQuota` (the CRITICAL second gate,
 * docs/devlog/16 I1). Mirrors validate.py.validate_quota_event step-for-step. `setQuota` writes
 * `account_alias` + `provider` straight into the materialized frame (`frame.quota[].account`)
 * served to the device, so the quota branch MUST pass the SAME default-deny gate as `session.*`.
 *
 * 🚨 NaN divergence FIX: a non-finite `used_ratio` must REJECT (Number.isFinite gate), matching
 * Python's `float()` raising → rejected. Previously TS did `Number(p.used_ratio ?? 0)` straight
 * into setQuota, so `"x"`→NaN and `NaN`→NaN flowed through where Python rejected — a parity hole.
 *
 * Rejects (never coerces / stores a raw value); returns the resolved canonical quota on success.
 */
export function validateQuotaEvent(ev: unknown): ValidatedQuota {
  if (ev === null || typeof ev !== "object" || Array.isArray(ev)) {
    throw new SanitizationError("event_not_object", "");
  }
  const e = ev as Record<string, unknown>;
  let payload = e["payload"];
  if (payload === undefined || payload === null) payload = {};
  if (typeof payload !== "object" || Array.isArray(payload)) {
    throw new SanitizationError("payload_not_object", payloadHash(ev));
  }
  const p = payload as Record<string, unknown>;
  const ph = payloadHash(ev);

  // provider mandatory + enum (envelope or payload). 🚨 I2 parity: Python uses
  // `str(ev.get("provider") or p.get("provider") or "")` — a TRUTHY `or`, so an empty-string
  // envelope provider falls through to the payload one (and `0`/`False`/`""`/None all fall
  // through). TS `??` only falls through on null/undefined, diverging on `""`. Use pyOr to mirror
  // Python's `or` chain exactly (the corpus locks identical decisions on every quota case).
  const provider = String(pyOr(e["provider"], p["provider"], "")).trim().toLowerCase();
  if (!PROVIDER_ENUM.has(provider)) throw new SanitizationError("provider_not_in_enum", ph);

  // account_alias: forbidden-pattern clean AND positively neutral. Never coerce. Same truthy-`or`
  // fallback semantics as Python (`str(ev.get(...) or p.get(...) or "")`).
  const accountAlias = String(pyOr(e["account_alias"], p["account_alias"], ""));
  const hit = containsForbidden(accountAlias);
  if (hit !== null) throw new SanitizationError(hit, ph);
  if (!looksLikeNeutralAlias(accountAlias)) throw new SanitizationError("alias_shape:account_alias", ph);

  // window_type enum. Python: `str(p.get("window_type") or "")` (truthy `or`).
  const windowType = String(pyOr(p["window_type"], "") ?? "");
  if (!QUOTA_WINDOW_TYPES.has(windowType)) throw new SanitizationError("enum:window_type", ph);

  // used_ratio: a FINITE number in [0, 1]. 🚨 I2 parity: Python rejects a `bool` BEFORE `float()`
  // (`if isinstance(raw_ratio, bool): reject`) — `float(True)==1.0` would otherwise silently coerce
  // True→1.0. So a boolean used_ratio is `quota_used_ratio_not_float`, NOT a valid 1.0/0.0. NaN /
  // inf / non-numeric / out-of-range REJECT (mirrors Python `float()` raising → rejected; never
  // clamp). `Number(undefined)`/`Number("")` → NaN → rejected, exactly like float(None)/float("").
  const usedRaw = p["used_ratio"];
  if (typeof usedRaw === "boolean") throw new SanitizationError("quota_used_ratio_not_float", ph);
  const usedNum =
    typeof usedRaw === "number"
      ? usedRaw
      : typeof usedRaw === "string" && usedRaw.trim() !== ""
        ? Number(usedRaw)
        : Number(usedRaw);
  if (!Number.isFinite(usedNum)) throw new SanitizationError("quota_used_ratio_not_float", ph);
  if (usedNum < 0 || usedNum > 1) throw new SanitizationError("quota_used_ratio_out_of_range", ph);

  // confidence (optional): enum-if-present.
  let confidence = p["confidence"];
  if (confidence !== undefined && confidence !== null) {
    confidence = String(confidence).trim().toLowerCase();
    if (!CONFIDENCE_ENUM.has(confidence as string)) throw new SanitizationError("enum:confidence", ph);
  } else {
    confidence = "unknown";
  }

  return {
    provider,
    account_alias: accountAlias,
    window_type: windowType,
    used_ratio: usedNum,
    confidence: confidence as string,
    is_estimated: p["is_estimated"] === undefined ? true : Boolean(p["is_estimated"]),
  };
}
