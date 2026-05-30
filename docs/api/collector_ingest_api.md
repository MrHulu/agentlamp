# Collector Ingest API

> Relay mode only. In local mode the collector serves the frame over the LAN and this
> ingest hop does not exist (see `architecture.md` → Deployment Modes).

## Endpoint

```http
POST /api/v1/collectors/{collector_id}/events
Content-Type: application/json
X-ACO-Key-Id: <kid>
X-ACO-Timestamp: <unix_seconds>
X-ACO-Nonce: <random_128bit_hex>
X-ACO-Payload-SHA256: <hex_sha256_of_raw_body>
X-ACO-Signature: v1=<hex_hmac_sha256>
Idempotency-Key: <stable_event_or_batch_key>
```

The path is versioned (`/api/v1/...`). `collector_id` MUST match
`^[A-Za-z0-9_-]{1,64}$`; the cloud rejects any other value **before** signature
verification (prevents canonical-string ambiguity/injection). `X-ACO-Key-Id` (`kid`)
selects which active collector secret signed the request, enabling rotation.

## Canonical Signature

This is the **authoritative** byte construction — both sides build exactly this string,
fields separated by a single `\n` (0x0A), no trailing newline:

```text
CanonicalString =
  "v1" + "\n" +
  HTTP_METHOD + "\n" +              # "POST"
  REQUEST_PATH + "\n" +            # "/api/v1/collectors/{collector_id}/events"
  KID + "\n" +
  TIMESTAMP + "\n" +              # decimal unix seconds, no padding
  NONCE + "\n" +                  # lowercase hex
  PAYLOAD_SHA256                  # lowercase hex of the exact raw request body
```

```text
signature = hex(HMAC-SHA256(collector_secret_for_kid, utf8(CanonicalString)))
```

Cloud compares signatures with constant-time comparison. Because `collector_id`, `kid`,
`nonce`, and `timestamp` are all restricted to safe character sets, no field can contain
a `\n` and the parse is unambiguous.

## Replay Protection

- Reject timestamps outside ±300 seconds of server time.
- Store each nonce for at least **720 seconds** (must exceed the full ±300 s window plus
  buffer, so a nonce can never be evicted while its timestamp is still acceptable).
- Reject any repeated nonce within that window.
- Store idempotency key for at least 7 days; a duplicate returns the prior result without
  re-applying the event.
- After a `stale_timestamp` rejection the collector MUST resync its clock from the
  response `server_time` (and NTP) before retrying — it must not loop.

## Limits

| Limit | Value | On exceed |
|-------|-------|-----------|
| Max events per request | 50 | `413 batch_too_large` — collector splits |
| Max raw body size | 100 KB | `413 body_too_large` |
| Per-collector ingest rate | 60 req/min | `429` + `Retry-After` |

## Request Body

```json
{
  "schema_version": 1,
  "collector_id": "collector-mac-main",
  "sent_at": 1716900400,
  "batch_id": "20260529T120000Z-0001",
  "events": [
    {
      "event_id": "evt_01HX...",
      "event_type": "session.upsert",
      "provider": "codex",
      "provider_event_name": "PreToolUse",
      "account_alias": "main",
      "source_seq": 42,
      "event_time": 1716900398,
      "dedupe_key": "codex:session:hmac7f3a:42",
      "payload": {
        "session_id": "hmac:7f3a9c…",
        "project_alias": "project-a",
        "status": "CODING",
        "model": "codex",
        "task_label": "implementing",
        "updated_at": 1716900398
      },
      "sanitization": {
        "policy_version": 1,
        "redactions": [],
        "confidence": "medium"
      }
    }
  ]
}
```

## Event Types

| Type | Purpose |
|------|---------|
| `collector.heartbeat` | Collector liveness |
| `session.upsert` | Create/update session summary |
| `session.status` | Status transition |
| `session.close` | Done/idle/timeout |
| `quota.window` | Quota summary |
| `alert.raise` | Waiting/error/quota alert |
| `alert.clear` | Clear alert |

## Provider Event Names

`provider_event_name` is optional but recommended for adapters. It stores the provider lifecycle event label after validation, for example:

- Codex: `UserPromptSubmit`, `PreToolUse`, `PermissionRequest`, `PostToolUse`, `Stop`.
- Claude: `UserPromptSubmit`, `PreToolUse`, `PermissionRequest`, `Notification`, `PostToolUseFailure`, `SessionEnd`, `Stop`.

The event name is allowed; raw provider event payload is not.

**Provider hook names are not a stable API.** They can be renamed or added by Codex/Claude
at any time. Adapters MUST NOT hard-fail on an unrecognized `provider_event_name`: map it
to the closest normalized status, set `status: UNKNOWN` if none fits, preserve the raw
event **name** (not payload) for local diagnostics, and never silently no-op. Each adapter
doc carries a "verified against provider hook docs as of <date>; treat as unstable" note
and versions its mapping.

## Response (per-event results — no poison-pill stall)

The response reports **each event individually** so one bad event never blocks the rest
or freezes the offline queue. Aggregate counts are convenience only.

```json
{
  "ok": true,
  "server_time": 1716900401,
  "ingest_id": "ing_01HX...",
  "accepted": 1,
  "duplicates": 0,
  "rejected": 1,
  "results": [
    {"event_id": "evt_01HX...", "status": "accepted"},
    {"event_id": "evt_01HY...", "status": "rejected", "reason": "sanitization_failed"}
  ]
}
```

Collector behavior on a per-event `rejected`:

- **Quarantine** the rejecting event to a local `dead_letter/` store (rejection reason +
  payload hash only — never the raw value), drop it from the replay queue, **continue**
  replaying the rest, and surface a collector-health alert.
- Never retry a `sanitization_failed` event automatically — it will never pass.

## Rejection Reasons → HTTP status

Request-level failures use HTTP status; per-event failures appear in `results[].reason`.

| Reason | HTTP | Level |
|--------|------|-------|
| `bad_signature` | 401 | request |
| `collector_revoked` | 403 | request |
| `stale_timestamp` | 401 | request |
| `reused_nonce` | 409 | request |
| `payload_hash_mismatch` | 400 | request |
| `schema_version_unsupported` | 400 | request |
| `batch_too_large` / `body_too_large` | 413 | request |
| rate limited | 429 (+ `Retry-After`) | request |
| `sanitization_failed` | per-event in `results` (HTTP still 200) | event |
| `unknown_field` | per-event in `results` (HTTP still 200) | event |

## Cloud-Side Invariant

Cloud runs a second, independent sanitization gate (identical rules to the collector's,
including recursive unknown-field rejection) even when the collector claims the event is
sanitized. See `../security/sanitization_policy.md` → Cloud Requirements.
