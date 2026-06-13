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

## CA Bundle Refresh Endpoint

The device pins a small ROOT CA **bundle** (compiled fallback in `firmware/src/ca/*.pem.inc`).
So a CA rotation does not require a reflash, the firmware refreshes the bundle over the
already-trusted TLS connection (`firmware/src/relay.h::refreshCaBundle`):

```http
GET /api/v1/device/{device_id}/cacerts
Authorization: Bearer <device_token>
Accept: application/x-pem-file
```

- **Auth**: identical to `/frame` — Bearer device token, header-only, hashed at rest. Revocation
  applies immediately (a revoked/unknown device cannot pull a fresh trust anchor): same
  `401 bad_token` / `403 device_revoked` / `404 unknown_device` precedence as the frame route.
- **Success (200)**: body is a PEM bundle (`Content-Type: application/x-pem-file`) containing one
  or more `-----BEGIN CERTIFICATE----- … -----END CERTIFICATE-----` blocks. The firmware
  structurally validates it (must contain BEGIN+END markers); a valid bundle is stored in NVS and
  **wins** over the compiled fallback. A non-200 or structurally invalid body is ignored
  (fail-closed — the device keeps its current trust anchor, never downgrades to unverified HTTP).
- **Source / rotation (relay)**: the cloud serves the bundle from `CA_BUNDLE` (var/secret) →
  KV `CONFIG["ca_bundle"]` → an embedded default (ISRG Root X1 + DigiCert Global Root G2 +
  Baltimore CyberTrust Root — the same roots compiled into the firmware fallback). Rotate with
  `wrangler secret put CA_BUNDLE` or a KV write; no firmware reflash. See `cloud_contract.md`.
- **When called**: opportunistically, on repeated TLS handshake failures — NOT on every poll. It
  shares the device rate-limit bucket with `/frame`.

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
    {"provider": "ai-center", "count": 3, "status": "CODING"},
    {"provider": "channel-bridge", "count": 1, "status": "WAITING"}
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

- A `fleet` row groups by **project**: `provider` carries the project display label
  (a clean string — NO baked `xN` suffix); `count` is the number of **active** agents
  in that project (working right now — not idle/done/stale/offline); `status` is the
  highest-priority status among that project's active agents. A project with zero active
  agents is omitted. The device renders the count as a separate badge.
- `fleet_more` (optional, integer): the number of **additional active agents** in
  projects dropped beyond the 5-row `fleet` cap (and any rows dropped by the 2 KB
  byte-cap trim). **Present only when > 0**; absent otherwise. The device may ignore it
  (backward-compatible per the unknown-field rule) or render a "+N more" hint.
- A `quota` entry carries **both** `w5` and `week` for one `(provider, account)`
  when both windows are known; a window with no data is **omitted** (never `null`).
- `seq` + `server_time` are **per-response volatile**: `seq` is a monotonic counter that bumps
  only when the rendered content / scene changes; `server_time` is the relay's wall clock at send.
  Both are emitted on **every** live frame (the local server **and** the cloud relay, in parity),
  but are **stripped from the static parity goldens** in `tests/fixtures/parity/frame_vectors.json`
  before comparison — their absence in the fixtures is by design (volatile values can't be pinned),
  **not** a contract gap. A reader may use `seq` for cheap change-detection; staleness must come
  from local elapsed time, never `server_time` (a skewed device RTC must not misjudge it — see
  Device Behavior).

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

`provider` in `primary` is a **Title-case display label** (`"Codex"`, `"Claude"`) derived
from the lowercase wire enum (`codex`/`claude`/`manual`); the frame generator does the mapping.
In a `fleet` row the same field name instead carries the **project display label** (rows group
by project, since an owner running many agents cares about "which project, how many busy"); it
is a clean sanitized alias with no count suffix. Firmware treats both as opaque display strings.
Other identity fields (`account`, `project`) carry the lowercase sanitized alias verbatim.

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

- `fleet`: max **5** entries — the device renders 5 rows, so the wire cap equals the
  render cap (server truncates lowest priority; overflow implied by a
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

