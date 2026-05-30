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

| Endpoint | Purpose |
|----------|---------|
| `POST /api/v1/collectors/{collector_id}/events` | Signed sanitized ingest (relay only) |
| `POST /api/v1/device/{device_id}/pair` | Exchange one-time pairing code for read-only token (both modes) |
| `GET /api/v1/device/{device_id}/frame` | Read-only frame pull |
| `GET /api/v1/device/{device_id}/cacerts` | Authenticated CA-bundle for device TLS pin refresh (relay; see firmware contract) |
| `POST /api/v1/device/{device_id}/heartbeat` | Optional device health |
| `/admin` | Dashboard |
| `/preview` | 172x320 simulator |

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

- Login.
- View providers, accounts, sessions, quotas, alerts, collectors, devices.
- Trigger test frames.
- View simulator.
- Revoke/rotate collector and device tokens.
- Audit ingest rejections.
