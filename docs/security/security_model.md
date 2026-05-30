# Security Model

> The full attacker-profile / trust-boundary / attack-tree treatment lives in
> `threat_model.md`. This file is the controls summary.

## Trust Zones

| Zone | Trust Level | Contents |
|------|-------------|----------|
| Local machine | High | Provider sessions, cookies, local files, hooks, raw logs |
| Collector process | Medium-high | Reads sensitive local state, sanitizes, serves frame (local) / signs+pushes (relay) |
| Public cloud relay *(relay mode only)* | Medium | Stores sanitized summaries + audit metadata only |
| LAN *(local mode)* | Medium | Frame travels the local network only; no third party |
| ESP32 device | Low-medium | Stores read-only device token + cached frames |
| Browser admin | Medium | Authenticated dashboard + simulator |

> **Local mode removes the public-cloud zone entirely** — the frame never leaves the LAN.
> Most controls below exist *because of* relay mode; in local mode the attack surface is the
> LAN only.

## Sensitive Data That Must Not Reach Cloud

- OpenAI cookies.
- Claude cookies.
- Refresh tokens.
- Browser sessions.
- Raw provider credential files.
- Full prompts.
- Full transcripts.
- Source code contents.
- Full local paths.
- Private repository remotes unless explicitly aliased.

## Required Controls

| Control | Required Behavior |
|---------|-------------------|
| Collector authentication | HMAC-SHA256 with `kid`, timestamp, nonce, payload hash; constant-time compare |
| Device authentication | Read-only bearer token (server stores hash only), never in URL, ≤ 90 d rotatable |
| Admin authentication | **TOTP required** for any non-localhost binding; login lockout; CSRF; 1 h session |
| Identifier hygiene | `collector_id`/`device_id` restricted to `[A-Za-z0-9_-]{1,64}` |
| Audit log | Key creation, key revoke, ingest rejection, admin changes, purge runs |
| Rate limit | Device 20/min, collector 60/min, admin login 10/5min (see `cloud_contract.md`) |
| Sanitization | Collector allowlist + **independent** cloud rejection gate (recursive unknown-field deny) |
| Hashing | Keyed HMAC for low-entropy identifiers (local pepper); plain SHA256 only for high-entropy ids |
| Encryption at rest | Relay DB + backups encrypted; no raw rejected payloads persisted |
| Retention | Sanitized events purged after 30 d (default); materialized state kept |

## Threats

| Threat | Mitigation |
|--------|------------|
| Replay attack | Timestamp window + nonce cache (≥720 s) + idempotency |
| Canonical-string injection | `collector_id`/`kid`/`nonce` restricted charsets; unambiguous parse |
| Token leakage from URL/proxy logs | Tokens only in headers; server stores hash; short-lived + rotatable |
| Collector bug leaks local path/prompt | Default-deny sanitizer + independent cloud gate + enum-only fields |
| Low-entropy hash reversal | Keyed HMAC with local pepper, not plain SHA256 |
| Identity leak via metadata | Local-mode default; upload jitter; retention purge — see metadata side-channel below |
| Public cloud (relay) compromise | Store sanitized summaries only; blast radius documented in `threat_model.md`; rotate/revoke |
| Cross-collector data bleed | Device↔collector binding scopes frames to bound collectors |
| Malformed frame crashes ESP32 | Schema validation + cached fallback + no token/frame over serial |
| Device stolen | Revoke device token (read-only); flash extraction is out-of-scope (see threat model) |
| Poison event stalls telemetry | Per-event ingest results + dead-letter quarantine |

## Metadata Side-Channel (sanitized ≠ invisible)

Even fully sanitized, **relay mode** uploads behavioral metadata (timing, project/account
counts, session cadence, quota burn). The complete inventory is in `sanitization_policy.md`
(Cloud-Visible Data Inventory) and the attacker model in `threat_model.md`. Local mode
exposes none of this to any third party — the strongest mitigation is "don't deploy relay
unless you need remote viewing."

## Security Acceptance

The project is not shippable until tests prove:

- HMAC mismatch / old timestamp / reused nonce are rejected.
- Duplicate event is not applied twice; one poison event does not stall the queue.
- Sanitizer fixtures pass (see `sanitization_policy.md` → Required Fixtures): unmapped cwd
  never emits basename; low-entropy branch uses HMAC not plain SHA256; plan-tier/model-id/
  path-in-error/free-text-task collapse to safe values; unknown field rejects the event.
- Device token in query string is rejected; token stored as hash server-side.
- `collector_id`/`device_id` outside the allowed charset are rejected.

