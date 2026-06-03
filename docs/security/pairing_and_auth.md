# Pairing and Auth

## Actors

- Admin user.
- Collector.
- ESP32 device.
- Cloud API.

## Device Pairing

> **What is implemented where.** The pairing-**code** exchange flow below is implemented in
> **local mode** (`server/agentlamp_server/app.py`: `POST /admin/device/{id}/code` issues the
> code, `POST /api/v1/device/{id}/pair` burns it for the token). The **relay** Worker does
> **not** implement `/pair` — on the relay a device token is installed directly as the
> `AGENTLAMP_DEVICE_TOKENS` secret (`wrangler secret put`, see `../cloud/deploy.md` §3) or at
> runtime via the admin route `POST /admin/devices/{device_id}/enroll` (`src/cloud/src/index.ts`),
> and the device pastes that token into its provisioning portal. Step 1's "dashboard" is
> aspirational (the relay has no HTML admin UI — see `../cloud/cloud_contract.md` → API Surface).

MVP flow — the **one-time pairing code is exchanged for the token** (the long-lived token
is never shown in a URL or QR, only the short code is):

1. Admin creates a device in dashboard.
2. Server returns `device_id` + a short-lived **one-time pairing code** (≤ 10 min TTL),
   bound to `device_id` (+ owner). The read-only device token is **not** revealed here.
3. The device (via provisioning portal) submits `device_id` + pairing code to
   `POST /api/v1/device/{device_id}/pair`; the server returns the read-only device token
   **once** and burns the code.
4. Device stores `device_id`, base URL, and token in NVS/Preferences.
5. Device calls the frame API with `Authorization: Bearer <device_token>`.
6. Admin can revoke or rotate the token (see Rotation).

Tokens never appear in URLs or QR codes; only the burn-on-use pairing code does.
Server stores only a **hash** of the device token, never the token itself.

### Local mode (no cloud)

In local mode the **local frame server** plays the role the cloud plays in relay mode for
pairing and device auth — there is no internet-exposed admin:

- The local frame server's admin route `POST /admin/device/{device_id}/code` (localhost/LAN
  only) issues the one-time pairing code for a `device_id`+`device_token`. (There is **no**
  `agentlamp device add` / `agentlamp device pair` subcommand — the `agentlamp` CLI is
  collector control only: enroll / revoke / status / doctor. Pairing is server routes, not a
  CLI verb.)
- The local frame server serves `POST /api/v1/device/{device_id}/pair` (token exchange) and
  verifies the `Authorization: Bearer <device_token>` on every frame request.
- Device tokens are stored as hashes in the collector's local store; the same revoke/rotate
  rules apply. MFA is not required (localhost/LAN binding), but the local admin/CLI must not
  bind to a public interface.

## Collector Registration

MVP flow:

1. Admin creates collector record.
2. Cloud returns `collector_id` and `collector_secret` once.
3. Collector stores secret in OS keyring.
4. Collector signs every ingest request.
5. Admin can revoke, rotate, or label collector keys.

## Token Types

| Token | Scope | Storage | Lifetime |
|-------|-------|---------|----------|
| Device token | Read device frame only | ESP32 NVS (server keeps hash only) | ≤ 90 days, rotatable |
| Collector secret | Write sanitized events for one collector | Local OS keyring (server keeps hash + `kid`) | rotatable via `kid` |
| Admin session | Dashboard access | Browser cookie, hardened flags | ≤ 1 h idle |

## Identifier Constraints

- `collector_id` and `device_id` MUST match `^[A-Za-z0-9_-]{1,64}$` (rejected otherwise at
  both ends — prevents HMAC canonical-string ambiguity; see `../api/collector_ingest_api.md`).

## Rotation

- Device token rotation keeps old + new valid for a short grace period; server stores hashes.
- Collector secret rotation uses `kid` in server records and the `X-ACO-Key-Id` header; more
  than one active key per collector is supported during rotation.
- Revocation takes effect immediately.

## Header Policy

- Device: `Authorization: Bearer <device_token>`.
- Collector: `X-ACO-*` HMAC headers including `X-ACO-Key-Id`.
- Admin: hardened cookie/session.

Tokens in URL query strings are rejected.

## Admin Session Security (relay mode — internet-exposed)

A public relay's admin surface is internet-exposed, so:

- **TOTP (or equivalent MFA) is required** for any non-localhost binding — not "preferred."
- Login rate-limiting + lockout (e.g. 10 attempts / 5 min → temporary lock).
- Session cookie flags: `Secure; HttpOnly; SameSite=Strict; Path=/admin; Max-Age=3600`.
- CSRF token on every state-changing admin action.
- Local-mode admin (bound to localhost/LAN only) may relax MFA but keeps the cookie flags.

