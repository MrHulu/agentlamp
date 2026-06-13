/**
 * policy.ts — embeds the GENERATED policy corpus (tests/fixtures/parity/policy.json).
 *
 * 🚨 BUILD-SPEC I2: the enums / allowed-key sets / forbidden-key set / regex sources are
 * imported from the generated data — NEVER hand-retyped. policy.json is generated from the
 * live Python `sanitize.py`/`validate.py` (tests/fixtures/parity/generate.py); a Python drift
 * fails server/tests/test_parity.py there AND the TS parity tests here until regenerated.
 *
 * `resolveJsonModule` lets us `import` the JSON directly; bundling inlines it into the Worker
 * (no fs at runtime in workerd). The TS parity tests separately re-load the SAME file via `fs`
 * to prove this embedded copy matches on disk.
 */
import policyJson from "../../../tests/fixtures/parity/policy.json" with { type: "json" };

export interface Policy {
  policy_version: number;
  provider_enum: string[];
  model_enum: string[];
  status_enum: string[];
  status_detail_enum: string[];
  tool_category_enum: string[];
  task_label_enum: string[];
  error_label_enum: string[];
  confidence_enum: string[];
  validate_envelope_keys: string[];
  validate_payload_keys: string[];
  forbidden_keys: string[];
  plan_tiers: string[];
  alias_max_len: number;
  title_max_len: number;
  alias_shape_regex: string;
  display_label_regex: string;
  /** Owner display-label cap (sanitize.DISPLAY_LABEL_MAX_LEN) — DATA-sourced, not retyped. */
  display_label_max_len: number;
  model_id_regex: string;
  /** provider_session_id shape gate (2026-06-03 hardening). DATA-sourced (not hand-retyped) so
   * looksLikeSessionId mirrors validate.py step 7 byte-for-byte — same I2 contract as the alias
   * shape regex. A non-canonical session id (free text used as a session KEY) rejects. */
  session_id_max_len: number;
  session_hmac_regex: string;
  session_token_regex: string;
  session_has_digit_regex: string;
  session_whitespace_regex: string;
  /** 🚨 I1/I2: per-entry case-sensitivity flag — the generator now ships
   * `{pattern, ignorecase}` objects (e.g. `\bBearer\s` / `\bCookie:` are case-INsensitive in
   * sanitize.py via `re.IGNORECASE`). validate.ts MUST honour each flag — dropping it silently
   * under-redacts lowercase `bearer`/`cookie:` leaks. */
  forbidden_patterns: Array<{ pattern: string; ignorecase: boolean }>;
  /** Case-sensitivity for the model-id regex (sanitize.py compiles `_MODEL_ID_RE` with
   * `re.IGNORECASE`). The generator emits this so TS does not hardcode the flag. */
  model_id_ignorecase: boolean;
}

export const POLICY: Policy = policyJson as Policy;

// Convenience Sets (membership checks). status compares upper-case; the rest lower-case
// (mirrors validate.py: `cmp = val.upper() if f == "status" else val.lower()`).
export const PROVIDER_ENUM = new Set(POLICY.provider_enum);
export const MODEL_ENUM = new Set(POLICY.model_enum);
export const STATUS_ENUM = new Set(POLICY.status_enum);
export const STATUS_DETAIL_ENUM = new Set(POLICY.status_detail_enum);
export const TOOL_CATEGORY_ENUM = new Set(POLICY.tool_category_enum);
export const TASK_LABEL_ENUM = new Set(POLICY.task_label_enum);
export const ERROR_LABEL_ENUM = new Set(POLICY.error_label_enum);
export const CONFIDENCE_ENUM = new Set(POLICY.confidence_enum);

export const FORBIDDEN_KEYS = new Set(POLICY.forbidden_keys);
export const VALIDATE_ENVELOPE_KEYS = new Set(POLICY.validate_envelope_keys);
export const VALIDATE_PAYLOAD_KEYS = new Set(POLICY.validate_payload_keys);
export const PLAN_TIERS = POLICY.plan_tiers;

// Forbidden-pattern entries carry a per-entry `ignorecase` flag (mirrors each
// `re.compile(..., re.IGNORECASE?)` in sanitize.py._FORBIDDEN_PATTERNS). validate.ts rebuilds
// each `new RegExp(pattern, ignorecase ? "i" : "")` so the case-sensitivity is preserved.
export const FORBIDDEN_PATTERN_ENTRIES = POLICY.forbidden_patterns;
// Mirrors `re.IGNORECASE` on sanitize.py._MODEL_ID_RE.
export const MODEL_ID_IGNORECASE = POLICY.model_id_ignorecase;

export const ALIAS_MAX_LEN = POLICY.alias_max_len;
export const TITLE_MAX_LEN = POLICY.title_max_len;

// provider_session_id shape regexes (mirror sanitize.py._SESSION_*_RE). Sourced from policy.json
// so the canonical opaque-id shape is never hand-retyped (I2). Consumed by looksLikeSessionId.
export const SESSION_ID_MAX_LEN = POLICY.session_id_max_len;
export const SESSION_HMAC_RE = new RegExp(POLICY.session_hmac_regex);
export const SESSION_TOKEN_RE = new RegExp(POLICY.session_token_regex);
export const SESSION_HAS_DIGIT_RE = new RegExp(POLICY.session_has_digit_regex);
export const SESSION_WHITESPACE_RE = new RegExp(POLICY.session_whitespace_regex);

// confidence string -> frame integer (device_frame_api.md). Mirrors S.CONFIDENCE_INT.
export const CONFIDENCE_INT: Record<string, number> = {
  high: 3,
  medium: 2,
  low: 1,
  unknown: 0,
};
