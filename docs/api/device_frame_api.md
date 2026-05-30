# Device Frame API

The **same** contract is served by the local collector (local mode, LAN base URL) and by
the cloud relay (relay mode). Only the base URL and the transport differ; the firmware is
identical. Base URL is provisioned at pairing (`http://<lan-ip>:8787` or `https://<relay>`).

## Endpoint

```http
GET /api/v1/device/{device_id}/frame
Authorization: Bearer <device_token>
Accept: application/json
X-Frame-Schema-Version: 1
```

Tokens must not be placed in URLs. The path is versioned (`/api/v1/...`).

## Versioning / Schema Evolution

- The device sends `X-Frame-Schema-Version: <max supported>`; the server responds with
  `min(server_supported, requested)` and echoes `X-Frame-Schema-Version` + `"v"` in body.
- Adding a field is backward-compatible: the device MUST ignore unknown fields, not reject.
- Removing/renaming a field requires a new `v`; the server keeps serving the device's
  supported `v` during a deprecation window of **≥ 90 days** announced in `CHANGELOG`.

## Response Constraints

- JSON response body under 2 KB (hard cap; see Frame Generation Rules in `cloud_contract.md`).
- Poll interval: 3-5 seconds.
- Include schema version, sequence, TTL, and server time.
- Device frame API is read-only.

## Frame Schema v1

```json
{
  "v": 1,
  "device_id": "orb-01",
  "scene": "alert",
  "headline": "ACTION REQUIRED",
  "primary": {
    "provider": "Claude",
    "account": "work",
    "status": "WAITING",
    "project": "project-a",
    "task": "waiting"
  },
  "fleet": [
    {"provider": "Codex", "count": 3, "status": "CODING"},
    {"provider": "Claude", "count": 1, "status": "WAITING"}
  ],
  "quota": [
    {
      "provider": "Codex",
      "account": "main",
      "w5": 0.72,
      "week": 0.41,
      "confidence": 2,
      "estimated": true
    }
  ],
  "accent": "yellow",
  "ttl": 5,
  "seq": 1852,
  "server_time": 1716900400
}
```

## Enums

`scene`:

- `boot`
- `pairing`
- `fleet`
- `focus`
- `quota`
- `alert`
- `offline`
- `stale`
- `diagnostics`
- `sleep`

`status`:

- `IDLE`
- `THINKING`
- `CODING`
- `READING`
- `TESTING`
- `WAITING`
- `DONE`
- `ERROR`
- `OFFLINE`
- `STALE`
- `UNKNOWN` (fallback for an unrecognized provider event; firmware renders it muted, like `IDLE`)

> This is the authoritative `status` set. Priority scores in `../cloud/cloud_contract.md`
> MUST be a subset of it (no `RUNNING`).

`provider` (in `primary`/`fleet`) is a **Title-case display label** (`"Codex"`, `"Claude"`)
derived from the lowercase wire enum (`codex`/`claude`/`manual`); the frame generator does the
mapping. Firmware treats it as an opaque display string. All other identity fields (`account`,
`project`) carry the lowercase sanitized alias verbatim.

`accent`:

- `blue`
- `cyan`
- `purple`
- `yellow`
- `green`
- `red`
- `white`
- `muted`

`confidence` (integer in the frame; maps from the normalized string):

| Normalized string | Frame integer |
|-------------------|---------------|
| `high` | 3 |
| `medium` | 2 |
| `low` | 1 |
| `unknown` | 0 |

## Array Caps (protect the 2 KB budget)

- `fleet`: max **6** entries (server truncates lowest priority; overflow implied by a
  `fleet_more` count if needed).
- `quota`: max **2** entries (top-2 risk).

A frame that would exceed 2 KB is trimmed server-side **before** sending; the device never
receives an oversized frame, but still rejects one defensively (see below).

## Device Behavior

- Reject unsupported schema version; **ignore unknown fields** within a supported version.
- If `seq` is unchanged, continue current animation.
- If `scene` changes, run transition.
- If request fails, use last valid cached frame.
- After 3 consecutive failures, show Offline.
- Compute staleness from **local elapsed time since fetch** (`millis()`), not the device
  RTC vs `server_time` (a skewed RTC must not misjudge staleness). If elapsed > `ttl`
  grace, show Stale.
- If body exceeds 2 KB, reject and keep cached frame.

## Error Responses

Body is always `{"error": "<reason>", "retry": <bool>}`; `retry` tells the firmware whether
to keep polling normally.

| Status | `error` | `retry` | Device action |
|--------|---------|---------|---------------|
| 401 | `bad_token` | false | Diagnostics scene "PAIRING REQUIRED"; stop normal polling |
| 403 | `device_revoked` | false | Same as 401 |
| 404 | `unknown_device` | false | Same as 401 |
| 429 | `rate_limited` | true | Back off to `max(poll*2, 60s)`; honor `Retry-After` |
| 503 | `frame_unavailable` | true | Use cache; if TTL expired, show Stale |

