# 02 — Server Chain Review (independent cross-model)

**Reviewer:** Claude (chain reviewer) + Codex (gpt-5.5, reasoning=high) cross-model.
**Date:** 2026-05-30
**Scope:** `server/agentlamp_server/{app,sanitize,state,preview}.py`, `server/tests/*`, contracts in `docs/api/device_frame_api.md`, `docs/security/{sanitization_policy,provider_sanitization_fixtures,pairing_and_auth}.md`, `docs/cloud/cloud_contract.md`.
**Codex ran:** yes (`/tmp/claude-501/multi-ai/chain-server-review.txt`, 85 passed confirmed in its venv run too).

## Verdict: REVISE

Tests are green (85 passed in `.venv`), the schema shape is correct, bearer auth works for the frame endpoint, and the sanitizer's *pattern* rejection is solid. But three P0 issues break the product's core trust claims (2 KB hard cap, alias default-deny, one-time pairing), and several P1 contract/robustness gaps remain. None block the build; all are fixable without a redesign.

---

## Codex raw findings (verbatim)

- `app.py:120/124` — **P0 security**: `POST /pair` returns `DEV_DEVICE_TOKEN` for `orb-01` even with no valid pairing code. Violates one-time-code contract (`pairing_and_auth.md:16/19`); makes bearer auth bypassable for the default device.
- `state.py:521/524` — **P0 contract**: 2 KB cap not guaranteed. `_enforce_byte_cap()` only trims `quota`/`fleet`; an accepted long string in `primary` still exceeds the cap. Confirmed: 3000-char `project_alias` → 3261-byte frame.
- `sanitize.py:613/620` — **P0 security/contract**: `sanitize_event()` accepts arbitrary `project_alias`/`account_alias` after only forbidden-pattern checks. Basename-like aliases (`client-acme-prod`) and very long opaque strings pass verbatim → not default-deny for aliases. Required-fixture tests exercise `project_alias()` directly (`test_sanitization_fixtures.py:26`) but never prove the *event pipeline* can't emit a basename-like alias.
- `state.py:420/422` — **P1 priority**: WAITING/ERROR only interrupt if top-scored. Modifiers can make another session win (`CODING + low quota` ties/beats WAITING) → `focus` instead of `alert`.
- `state.py:413/424` — **P1 priority**: quota danger ignored with no sessions (`not ordered` → `sleep` before quota check). Confirmed: quota 0.95, no sessions → `sleep`, contradicting `cloud_contract.md:101`.
- `state.py:489/506` — **P1 schema**: quota entries emit *either* `w5` *or* `week` (deletes the other). Schema example shows both.
- `state.py:517` / `test_frame.py:67` — **P1 schema exactness**: runtime may emit top-level `fleet_more`, not in the v1 schema example; tests explicitly allow the extra key so they don't enforce exactness.
- `app.py:80` — **P1 API contract**: malformed `X-Frame-Schema-Version` returns FastAPI default `422 {"detail":...}` instead of the documented `{"error","retry"}` envelope.
- `sanitize.py:625` — **P1 crash**: non-integer `schema_version` raises raw `ValueError`, not `SanitizationError`; a caller catching only sanitizer errors can 500.

## My independent verification

Every P0/P1 reproduced with a minimal script against the repo `.venv` (Python 3.14, fastapi present):

| Claim | Repro result |
|---|---|
| Byte-cap bypass | 3000-char `project_alias` → frame **3261 bytes** ≥ 2048. `_enforce_byte_cap` never touches `primary`. **CONFIRMED P0** |
| Alias not default-deny | `sanitize_event` with `project_alias="client-acme-prod"` (and `"a"*3000`) → emitted **verbatim**; `looks_like_prompt`=False, `contains_forbidden`=None. **CONFIRMED P0** |
| Pairing bypass | `POST /api/v1/device/orb-01/pair {"pairing_code":"totally-bogus-not-issued"}` → **200 `{"device_token":"dev-local-token"}`**. **CONFIRMED P0** (mitigated by local-only default token, but still violates the one-time-code contract) |
| Priority modifier suppresses alert | WAITING injected first, CODING (low-quota +30) injected last → tie at 100, recency tie-break picks CODING → scene **`focus`**, WAITING alert suppressed. Pinned CODING (+50=120) > WAITING(100) → **`focus`** too. **CONFIRMED P1** |
| Quota danger w/ no sessions | quota 0.95, zero sessions → scene **`sleep`**. **CONFIRMED P1** |
| Non-int schema_version | raw `ValueError: invalid literal for int()`. **CONFIRMED P1** |
| Malformed header | `X-Frame-Schema-Version: abc` → **422 `{"detail":[...]}`** (not the contract envelope). **CONFIRMED P1** |

I found no Codex false positives. One nuance I add: the alias and byte-cap P0s are *reachable through the real `/admin/event` ingest path* and the sanitizer is explicitly the documented trust boundary ("the product's entire trust claim"), so these are genuine policy violations, not just theoretical. The pairing P0 is real but its blast radius is limited to local mode with a known default token — still a contract violation that must be gated for relay-mode reuse (the firmware/contract is shared).

What is correct and should NOT be touched: bearer-on-frame auth + token-never-in-URL (tests `test_token_never_in_url`, `test_frame_requires_bearer` pass), HMAC-keyed-not-plain-sha256 labels (`hmac_label` vs `plain_sha256`, asserted), enum coercion for status/model/task/error, recursive unknown-field + forbidden-key rejection, schema-version `min()` negotiation, TTL liveness STALE 120s/OFFLINE 600s, seq-on-content-change. The pepper is per-process `secrets.token_bytes(32)` with optional `AGENTLAMP_PEPPER_HEX` — fine for local mode.

---

## P0 (must fix before shipping / before relay-mode reuse)

1. **`sanitize.py` — aliases not default-deny.** In `sanitize_event` (~L613-622), `project_alias`/`account_alias` are emitted after only `assert_clean` + a weak `looks_like_prompt` check. Add a positive shape gate: enforce a max length (e.g. ≤ 32 chars) **and** an allowlist regex (e.g. `^[a-z0-9][a-z0-9-]{0,31}$`); anything else → reject (default-deny) or collapse to a keyed-HMAC `project-<hmac6>` / `account-<hmac4>` rather than echoing. Then add a fixture asserting the *event pipeline* (not just `project_alias()`) never emits a basename-like or over-length alias.

2. **`state.py` — 2 KB cap not guaranteed.** `_enforce_byte_cap` (~L509-527) trims only `quota`/`fleet`. Truncate/clamp `primary.project`/`primary.task`/`primary.account` (and any string field) so the serialized body is provably < 2048 regardless of accepted input. Add a test that injects a long alias and asserts `< FRAME_BYTE_CAP`. (Fixing P0 #1 mostly closes this, but keep a hard byte-cap backstop — defense in depth.)

3. **`app.py` — pairing token leak.** `pair_device` (~L121-124) mints `DEV_DEVICE_TOKEN` for `orb-01` on *any* (or absent) code. Require a valid issued one-time code for every pair, including the dev device; if a smooth local first-run is wanted, mint a code at startup and log it locally, never hand the token out for a bogus code. Update `test_pair_returns_device_token` accordingly.

## P1 (fix before mandatory / relay reuse)

4. **`state.py` `_select_scene` (~L417-426) — alert can be suppressed by modifiers.** Decide WAITING/ERROR/quota-danger interrupt by *scanning all sessions/quota for the interrupt condition*, not only `ordered[0]`. `cloud_contract.md:101` says alert interrupts rotation for waiting/error/quota-danger/offline unconditionally.

5. **`state.py` `_select_scene` (~L413) — quota danger ignored with no sessions.** Move the quota-danger check above the `if not ordered: return sleep` branch (or include it in the no-session path) so a 0.95 quota fires `alert` even with zero live sessions.

6. **`app.py:80` + `sanitize.py:625` — error-envelope + crash hardening.** Accept `X-Frame-Schema-Version` as `str` and coerce defensively (return the contract `{"error","retry"}` envelope on garbage, not FastAPI's `{"detail"}`); wrap `int(event.get("schema_version",1))` in sanitize so non-int raises `SanitizationError`, not raw `ValueError`.

7. **Schema exactness (P1, lower confidence).** Either (a) document `fleet_more` and the single-window `w5`/`week` quota shape as the authoritative v1 schema in `device_frame_api.md`, or (b) make the generator match the doc example (emit both `w5` and `week`, drop `fleet_more`). Pick one and make `test_frame_schema_v1_shape` enforce the exact key set (remove the `- {"fleet_more"}` escape hatch). Since the doc says "device MUST ignore unknown fields," `fleet_more` is contract-compatible — but the doc and tests should agree explicitly.

## Notes / non-blocking

- Tests run only in the repo `.venv` (`../.venv/bin/python -m pytest`); ambient `pytest` lacks fastapi. Document this in `server/README` / CI so the suite isn't silently skipped.
- `_pending_fleet_more` is an instance attribute set inside `build_frame` under `self._lock`, so the earlier-looking races are not actually reachable (single RLock around the whole build). No action.
- `preview.py` correctly escapes interpolated frame values with `esc()`; device_id/token are substituted server-side into a local-only page. Low risk in local mode; if exposed, validate `device_id` before substitution.
