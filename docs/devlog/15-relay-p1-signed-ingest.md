# 15 — Relay mode P1: signed collector ingest

> 2026-06-02. Boss chose to build the full cloud relay (TASK-007) — the "advanced chapter"
> that lets the screen show state away from the laptop's LAN, over WiFi→internet→cloud. This
> is the security envelope (Phase 1 of 5); the per-event sanitize+apply reuses the SAME
> default-deny sanitizer local mode uses. No deploy/firmware yet — pure, fully-tested server code.

## What shipped (server-only, local mode untouched)

- `server/agentlamp_server/ingest.py` — the security layer for
  `POST /api/v1/collectors/{id}/events`, exactly per `docs/api/collector_ingest_api.md`:
  - `canonical_string()` — the authoritative `v1\nPOST\n<path>\n<kid>\n<ts>\n<nonce>\n<sha256>`
    byte construction (6 newlines, no trailing); `sign()` = HMAC-SHA256.
  - `IngestVerifier` — charset (collector_id/kid/nonce/sha256) **before** signature; body-size
    + payload-SHA256 over the exact raw bytes; constant-time signature compare; ±300 s timestamp
    window (returns `server_time` so the collector resyncs, no loop); nonce replay store (≥720 s);
    `KeyStore` keyed by `kid` (rotation; revoked = absent → `collector_revoked`).
  - `IdempotencyStore` — a retried batch (fresh nonce, same `Idempotency-Key`) returns the prior
    result verbatim, never re-applied.
  - `load_keys_from_env` — `AGENTLAMP_COLLECTOR_KEYS=kid:secret,…`; **empty → reject all**
    (safe default; local mode ships no key, so its ingest endpoint accepts nothing).
- `app.py` — the `POST /api/v1/collectors/{id}/events` route: request-level failures →
  HTTP status (`bad_signature` 401, `stale_timestamp` 401, `reused_nonce` 409,
  `payload_hash_mismatch` 400, `collector_revoked` 403, `batch_too_large`/`body_too_large` 413,
  `schema_version_unsupported` 400); **per-event** failures → `results[]` with HTTP 200 (one
  poison event never stalls the batch). Each event maps to the provider-event envelope and goes
  through `FrameState.apply_event` → `sanitize_event` — the cloud's **independent** sanitize gate.
  `collector.heartbeat`/`quota.window` route to the matching state methods.

## Verified (security acceptance — security_model.md)

`server/tests/test_ingest.py` (13 tests): valid→accepted; bad signature→401; stale ts→401 (+
server_time); reused nonce→409; payload-hash mismatch→400; unknown/revoked kid→403; bad
collector_id charset→400; batch>50→413; body>100KB→413; idempotent retry→prior result;
**poison event rejected per-event while the clean one still applies**; empty keystore→403;
canonical-string exact shape. 160 server + 50 collector tests green; local mode unchanged.

## Next (P2–P5)

P2 collector signed-push (daemon signs+POSTs in relay mode); P3 public-deploy security (TOTP
admin, device token/pairing, rate limits, retention, audit); P4 device TLS (firmware HTTPS +
pinned ISRG Root X1 + NTP); P5 deploy + end-to-end over HTTPS from a foreign network.
