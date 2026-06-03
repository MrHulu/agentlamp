# 16 — Relay on Cloudflare: build spec (Worker + Durable Object + KV)

> 2026-06-02. The owner reopened the relay design with a hard new requirement: the screen
> must work from ANY network, the owner has MANY computers and SWITCHES computer + WiFi
> frequently, switching must be SIMPLE + FAST, and the cloud is **Cloudflare-only**. This spec
> is the binding contract for the build, derived from `/deep-research` prior-art (Tidbyt cloud-pull
> + Cloudflare Durable Objects + ESPHome provisioning) and a Claude-vs-Codex debate that returned
> **GO-WITH-CHANGES**. Every downstream build/review agent follows THIS document.

---

## 🚨 NON-NEGOTIABLE INVARIANTS (read first — violating any one is a NO-GO)

| # | Invariant | Why |
|---|-----------|-----|
| **I1** | **The cloud NEVER re-runs the sanitizer transforms.** The Python collector is the ONLY place raw→safe heuristic redaction happens. The Worker/DO **only VALIDATES** the already-sanitized output: key-allowlist + forbidden-key reject + enum membership + neutral-alias shape + forbidden-pattern *reject* scan. **NO-GO** if the cloud ever accepts raw `cwd`/`prompt`/`model`/path and tries to sanitize it in TS. | JS `RegExp`/Unicode ≠ Python `re`+`unicodedata`; re-deriving 800 lines of NFKC/zero-width heuristics in TS = silent under-redaction. Validation of enum-only output is language-equivalent. |
| **I2** | **Cross-language parity = release blocker.** The fixtures in `tests/fixtures/parity/` are GENERATED from the proven Python reference. Both the Python tests AND the TS vitest assert against the identical corpora. Mismatch fails the build. **No deploy until both green.** | Single source of truth = the Python impl + the generator. Prose docs are not a contract; executable fixtures are. |
| **I3** | **NO single-machine / single-network hardcodes in relay paths.** Relay URL, device token, CA roots, collector `kid`/secret, Cloudflare account/zone — all from NVS provisioning (device) or env/secrets (cloud/collector). `firmware/src/config.h`'s `192.168.1.148` + `"yangzhenzhous-macbook-air"` MUST NOT compile into a relay build. | Cross-device / cross-network / no-hardcode requirement. The whole point is "not tied to one Mac." |
| **I4** | **The Durable Object owns ALL revocation-critical + strongly-consistent state** — nonce set, idempotency map, device/collector registry, revocation, materialized frame state, purge/audit alarms. **KV holds only non-urgent config/cache.** A revoke must take effect immediately; KV is eventually consistent. | A revoked `kid`/device must stop being accepted at once. |
| **I5** | **Enrollment installs the whole stack, not just a `kid`.** One-line enroll on a new computer must: install hooks + init pepper/aliases (keyring) + store the collector secret + enable relay push. An un-enrolled computer → cloud shows offline/stale, never "magically follows." | "Switch computer fast" must be real, not a half-setup that silently shows nothing. |

---

## Topology (the proven shape)

```
each computer →  collector (Python daemon, EXISTS) → sanitize (Python, the only transform)
                     → HMAC-sign → POST https://<relay-host>/api/v1/collectors/{kid}/events
Cloudflare    →  Worker (verify HMAC + edge rate-limit + route + uniform auth errors)
                   └─RPC─→ Durable Object "relay" (singleton state machine):
                            nonce/idempotency · device+collector registry+revocation ·
                            VALIDATE sanitized event (I1) · apply → materialized state ·
                            frame generation · retention-purge + audit via DO alarms
                 KV "CONFIG": non-urgent config/cache only (NOT revocation-critical)
ESP32 device  →  polls ONE FIXED https URL  GET /api/v1/device/{device_id}/frame
                   Authorization: Bearer <device_token>  (header only, hashed at rest)
                   pinned ROOT CA bundle + NTP-before-TLS + /cacerts refresh
                   WiFi: multi-network NVS store + captive-portal fallback
```

Switch computer = run the collector there (one-line enroll, its own `kid`); revoke = delete the
kid from the DO registry. Switch WiFi = device auto-joins any stored network, else 1-field portal.
**The device's backend URL never changes**, so neither switch touches the device config.

## Auth model (resolves the debate's one residual split)
- **Device `/frame` + collector `/events`**: high-entropy bearer / HMAC + WAF + rate-limit (edge & DO)
  + uniform `401/403/404`. **No** Cloudflare Access on these (an extra rotating ESP32 secret is a
  portability/SOLID regression; the device contract mandates header-only Bearer). A WAF rule can be
  added later with zero firmware change.
- **`/admin` + enroll**: Cloudflare Access / MFA / TOTP required (no ESP32 in this path).

## Component build list (dependency order)

### F. Foundation (built first, by the orchestrator — it is the contract)
- `server/agentlamp_server/validate.py` — the **validate-only cloud gate** (Python reference).
  Pure stdlib; DRY-imports the enums + `contains_forbidden` + `looks_like_neutral_alias` +
  `_KNOWN_*_KEYS` + `_FORBIDDEN_KEYS` from `sanitize.py` (zero duplication on the Python side).
  Strict: reject (never coerce) — the collector already emitted canonical values.
- `tests/fixtures/parity/generate.py` — imports `agentlamp_server.{sanitize,state,ingest,validate}`
  and emits, all from the Python reference:
  - `policy.json` — portable data: every enum, allowed-key set, forbidden-key set, `ALIAS_MAX_LEN`,
    `TITLE_MAX_LEN`, the alias-shape + display-label + title regex source. Both languages load it.
  - `hmac_vectors.json` — `canonical_string` + `sign` vectors (the frozen byte-spec, codegen seed).
  - `sanitize_corpus.json` — validate accept/reject cases (relay mode), each `{name,event,expect,reason?}`.
  - `frame_vectors.json` — `{name, events[], expect_frame}` golden from `state.build_frame`.
- `server/tests/test_parity.py` — proves the corpora are faithful to the live Python impl.

### C. Cloud (TypeScript, `src/cloud/`) — the crux
- scaffold: `package.json`, `tsconfig.json`, `wrangler.toml` (DO binding `RELAY`, KV `CONFIG`,
  vars for host; secrets via `wrangler secret`), `vitest.config.ts`.
- `src/policy.ts` — loads/embeds `policy.json` (NO hand-retyped enums — import the generated data).
- `src/sign.ts` — `canonicalString()` + HMAC verify (must match `hmac_vectors.json` byte-for-byte).
- `src/validate.ts` — the validate-only gate (I1), mirrors `validate.py`; verified by `sanitize_corpus`.
- `src/frame.ts` — priority/scene/fleet/quota/byte-cap (ports `state.py` DISPLAY logic — not security
  heuristics); verified by `frame_vectors.json`.
- `src/relay_do.ts` — the Durable Object (I4).
- `src/index.ts` — Worker entry: route + HMAC verify + edge rate-limit + uniform errors.
- `test/{sign,validate,frame,frame_round,quota,ingest}.test.ts` — vitest, loading the SAME
  generated corpora (I2): `sign.test.ts` ↔ `hmac_vectors.json`, `validate.test.ts` ↔
  `sanitize_corpus.json`, `frame.test.ts` / `frame_round.test.ts` ↔ `frame_vectors.json`,
  `quota.test.ts` ↔ `quota_corpus.json`, and `ingest.test.ts` (end-to-end DO ingest). Run under
  `wrangler dev` local / `vitest` — **no cloud auth needed**.

### K. Collector signed-push + enroll (Python, extends `src/collector/`)
- relay-mode signed push (`netpost.py` already a seed); dead-letter on reject; resync on `server_time`.
  The daemon also emits a periodic **signed `collector.heartbeat`** in relay mode
  (`daemon._relay_heartbeat` → `relaypost.push_heartbeat`) so an idle-but-present owner does
  not decay to offline; the cloud short-circuits that event before the validate gate (no payload).
- `agentlamp enroll` — installs the WHOLE stack (I5) and enables relay push. With no
  `--kid`/`--secret` it **mints** a fresh `kid` + 256-bit secret (`_mint_kid`/`_mint_secret`),
  stores the secret in the keyring, and (step 6) registers the pair with the DO's live registry
  via `/admin/collectors/{kid}/enroll` (admin-token gated) — that runtime registration is what
  makes the one-liner real (no `wrangler` redeploy). You may still pass `--kid`/`--secret` to
  reuse a pre-provisioned `AGENTLAMP_COLLECTOR_KEYS` pair. Bring-up is `enroll` → source
  `relay.env` → start the daemon.

### D. Device firmware (`firmware/`, C++)
- HTTPS: `WiFiClientSecure` + pinned ROOT CA **bundle** (2-3 roots) + **NTP-before-TLS** + `/cacerts` refresh.
- Kill the single-machine hardcodes for relay builds (I3); relay URL/token/CA from NVS only.
- Multi-network WiFi store + captive-portal fallback. (Build/compile autonomously; physical flash + eyeball is owner-gated.)

### X. Docs (critical — bad docs make agents dumb)
- `docs/architecture/architecture.md` → relay = Cloudflare Worker+DO+KV; mark I1 (validate-only) at top.
- `docs/security/sanitization_policy.md` → Cloud Requirements: clarify "independent gate" = validate-only
  of the sanitized output (with the debate rationale), NOT re-running transforms.
- `docs/runbook/switch-fast.md` (NEW) — "switch computer / switch WiFi in under a minute", step-by-step.
- `docs/cloud/deploy.md` (NEW) — exact `wrangler login` + `wrangler deploy` + KV/DO/secret setup
  (owner-gated: needs one-time `wrangler login`).

## Gated steps (cannot be done autonomously — flag, don't fake)
1. **`wrangler deploy`** needs a one-time interactive `wrangler login` (OAuth) — owner runs it once.
   Everything is built + locally tested (`wrangler dev` + vitest); the deploy is scripted + documented.
2. **Physical device flash + eyeball** needs the hardware on a network.

## Definition of done
- F + C + K + D + X built; Python suite (≥210) AND TS vitest green against the SAME corpora (I2);
  no relay-path hardcodes (I3); reviewed by my sub-agents (spec + skill-optimizer) AND Codex sub-agents;
  useful findings fixed (docs especially); every non-gated feature manually verified; the 2 gated steps
  clearly flagged with exact runbooks.
