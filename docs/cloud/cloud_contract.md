# Cloud Contract

> Relay mode only. The aggregation, priority, and frame-generation rules below are the
> **single source of truth** and are reused verbatim by the local-mode frame server
> (`architecture.md` → local mode). The local server omits the **collector-ingest HMAC**
> surface (events come straight from the in-process collector) and the **internet-exposed
> MFA admin**, but **still issues and verifies device bearer tokens** and serves the
> pairing endpoint via a minimal local pairing CLI/UI (see `../security/pairing_and_auth.md`
> → Local Mode).

## Responsibilities

- Collector registration and key state.
- Signed collector ingest.
- Event dedupe and replay protection.
- Sanitized event persistence.
- Session/quota/alert aggregation.
- Display priority calculation.
- Device frame generation.
- Admin dashboard.
- 172x320 browser simulator.
- Audit logs.

## API Surface

The **Status** column is ground-truth as of the relay build (`src/cloud/src/index.ts`): the
Cloudflare Worker implements ONLY `events`, `frame`, `cacerts`, the `/admin` enroll+revoke
routes, and `/healthz`. Rows marked *local-mode-only* are served by the Python frame server
(`server/agentlamp_server/app.py`), not the relay Worker. Rows marked *aspirational* are in
neither and are tracked future work — do not curl them against a deployed relay.

| Endpoint | Purpose | Status |
|----------|---------|--------|
| `POST /api/v1/collectors/{collector_id}/events` | Signed sanitized ingest (relay only) | relay-implemented (`index.ts` → RelayDO) |
| `GET /api/v1/device/{device_id}/frame` | Read-only frame pull | relay-implemented + local-mode |
| `GET /api/v1/device/{device_id}/cacerts` | Authenticated CA-bundle for device TLS pin refresh (see firmware contract) | relay-implemented |
| `GET /healthz` | Liveness (no secret, no state) | relay-implemented + local-mode |
| `POST /admin/collectors/{kid}/revoke` | Revoke a collector kid (strongly-consistent, I4) | relay-implemented (admin-bearer gated) |
| `POST /admin/collectors/{kid}/enroll` | Runtime-enroll a collector kid (I5; body `{secret}`) | relay-implemented (admin-bearer gated) |
| `POST /admin/devices/{device_id}/revoke` | Revoke a device token (strongly-consistent, I4) | relay-implemented (admin-bearer gated) |
| `POST /admin/devices/{device_id}/enroll` | Runtime-enroll a device token (I5; body `{token}`) | relay-implemented (admin-bearer gated) |
| `POST /api/v1/device/{device_id}/pair` | Exchange one-time pairing code for read-only token | **local-mode-only** (`app.py`; relay enrolls device tokens via `/admin/devices/{id}/enroll`) |
| `POST /admin/event` · `POST /admin/quota` · `POST /admin/heartbeat` · `POST /admin/reset` · `POST /admin/device/{id}/code` | Local manual event / quota / pairing-code injection | **local-mode-only** (`app.py`) |
| `GET /preview` | 172x320 browser simulator | **local-mode-only** (`app.py`) |
| `POST /api/v1/device/{device_id}/heartbeat` | Optional device health | **aspirational** (not in the Worker or the local server) |
| `/admin` dashboard + login (MFA/TOTP) | Web admin UI | **aspirational** (the relay exposes only the `/admin/*` revoke+enroll JSON routes above; no HTML dashboard/login is implemented — admin gating is the in-Worker bearer + optional Cloudflare Access) |

### `GET /api/v1/device/{device_id}/cacerts` — pinned CA bundle refresh

Cross-piece contract with the firmware (`firmware/src/relay.h::refreshCaBundle`,
`docs/api/device_frame_api.md`). The device pins a small ROOT CA bundle and refreshes it here so a
CA rotation never bricks a deployed orb (no reflash).

- **Auth**: bearer device token, **identical to `/frame`** (header-only, hashed at rest). The
  Durable Object verifies the token and applies revocation immediately (I4): a revoked/unknown
  device gets `403 device_revoked` / `404 unknown_device` and CANNOT pull a fresh trust anchor.
  `401 bad_token` on a wrong token. Same precedence table as the frame route.
- **Response (200)**: `Content-Type: application/x-pem-file`; body is a PEM bundle with one or
  more `BEGIN/END CERTIFICATE` blocks. The firmware structurally validates it and stores it in
  NVS where it **wins** over the compiled `ca_bundle.h` fallback. Invalid/non-200 → device keeps
  its current anchor (fail-closed).
- **Bundle source precedence (relay)**: `CA_BUNDLE` env var/secret → KV `CONFIG["ca_bundle"]`
  (non-urgent cache, eventually consistent — I4) → an embedded default in `src/cloud/src/ca.ts`
  (ISRG Root X1 + DigiCert Global Root G2 + Baltimore CyberTrust Root, the same roots compiled
  into the firmware fallback). A configured-but-malformed bundle (no BEGIN/END markers) is refused
  and falls over to the default. **Rotation** = `wrangler secret put CA_BUNDLE` or a KV write; no
  firmware reflash, no per-device/per-network hardcode (I3).
- **Rate limit**: shares the device-frame bucket (20/min); the firmware calls it only on repeated
  TLS handshake failures, not on every poll.

## Rate Limits

| Caller | Limit | On exceed |
|--------|-------|-----------|
| Device frame poll | 20 req/min | `429` + `Retry-After` |
| Collector ingest | 60 req/min | `429` + `Retry-After` |
| Admin login | 10 / 5 min, then lockout | `429` |

## Retention & Purge

- Sanitized `collector_events` purged after **30 days** (default; configurable); only
  materialized state kept long-term.
- Purge job is scheduled + auditable. Admin has an explicit delete/export control.
- Backups must be encrypted at rest and contain no raw rejected payloads.

## Alert Identity / Dedup

Alerts are keyed by `(provider, account_alias, alert_type)`. `alert.raise` returns an
`alert_id`; `alert.clear` references the same tuple. A cloud-raised alert (e.g. quota > 90%)
is cleared by the matching tuple, so a raise and its clear can never mismatch.

## Internal Services

```text
auth_service
collector_ingest_api
event_store
provider_normalizer
session_aggregator
quota_aggregator
alert_engine
display_priority_engine
device_frame_api
admin_dashboard
audit_log
```

## Priority Rules

Base score:

- `WAITING`: +100
- `ERROR`: +90
- `CODING`: +70
- `THINKING`: +65
- `TESTING`: +60
- `READING`: +55
- `DONE`: +20
- `IDLE`: +0

> Status values here MUST be a subset of the `status` enum in `../api/device_frame_api.md`.
> (`UNKNOWN` is an internal fallback — it scores `+0` like `IDLE` and is never rendered as a
> distinct scene.)

Modifiers:

- Low quota: +30.
- User pinned: +50.
- Stale over 10 minutes: -20.

## Frame Generation Rules

- Cloud emits the final ordering.
- Device receives only top-level display-ready values.
- Quota page includes top 2 quota entries.
- Alert scene interrupts normal rotation for waiting/error/quota danger/offline.
- Frame body must remain under 2KB.
- Codex and Claude sessions share one priority queue; provider name is display metadata, not a separate scene.

## Admin MVP

> **Implementation status:** of this list, only **revoke/enroll of collector and device
> tokens** is implemented on the relay — as the admin-bearer-gated JSON routes
> `POST /admin/{collectors,devices}/{id}/{revoke,enroll}` (`src/cloud/src/index.ts`), NOT as
> an HTML dashboard. Login / a web dashboard / test-frame trigger / simulator view / a
> rejection-audit UI are **aspirational** — see the API Surface table. (The 172x320 simulator
> exists, but only in the local-mode Python server's `/preview`.)

- Login.
- View providers, accounts, sessions, quotas, alerts, collectors, devices.
- Trigger test frames.
- View simulator.
- Revoke/rotate collector and device tokens.
- Audit ingest rejections.
