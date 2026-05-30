# Threat Model

This is the structured threat model AgentLamp is designed against. It is also a teaching
artifact: the point of the project is to show how to bridge hardware to sensitive agent
state **without** leaking it.

## Assets

| Asset | Where it lives | Sensitivity |
|-------|----------------|-------------|
| Provider credentials (cookies, refresh tokens, keys) | Local machine only | Critical — must never leave |
| Raw prompts / transcripts / source / paths | Local machine only | Critical — must never leave |
| Sanitized agent state (status, aliases, quota ratios) | Collector; relay (relay mode) | Low, but behavioral metadata (below) |
| Collector secret | OS keyring (local) | High — signs ingest |
| Device token | ESP32 NVS; hash on server | Medium — read-only frame access |
| Admin session | Browser cookie | High — controls tokens/devices (relay) |

## Trust boundaries

```
[provider creds | raw content]  --(never crosses)-->  X
        |
   local machine  ──sanitize──►  collector process
        |                              |
        |                    local mode: LAN frame  ─────────►  ESP32 (LAN)
        |                              |
        |                    relay mode: signed HTTPS  ──────►  public relay  ──►  ESP32 (anywhere)
```

The sanitizer is the **one** boundary the whole product depends on. Local mode keeps the
device inside the local-machine/LAN boundary; relay mode adds a public boundary.

## Attacker profiles

| Attacker | Capability | Primary mitigations |
|----------|-----------|---------------------|
| **Honest-but-curious relay operator** | Reads the relay DB and request stream | Sanitization (enum-only, keyed-hash, no plan tier/model id); metadata side-channel acknowledged + jittered + purged; **local mode avoids them entirely** |
| **Relay DB compromise** | Dumps all stored rows + backups | Sanitized data only; encryption at rest; no raw rejected payloads; token hashes not tokens; 30-day retention limits the window |
| **Network MITM** | Sits between collector/device and relay | TLS; HMAC-signed ingest with replay protection; device cert-pinning to long-lived root |
| **Stolen device** | Has the physical ESP32 | Token is read-only + revocable; flash extraction out-of-scope (documented) |
| **Malicious/buggy collector** | Emits crafted events | Independent cloud-side sanitization gate; recursive unknown-field deny; per-event quarantine |
| **Compromised admin (relay)** | Has dashboard access | MFA required; CSRF; lockout; audit log of all changes |

## Top attack trees (abbreviated)

1. **Leak client identity to the relay.** Paths: free-text field → *closed* (enums only);
   path basename as alias → *closed* (alias map + HMAC, basename forbidden); low-entropy
   hash reversal → *closed* (keyed HMAC); model id / plan tier → *closed* (enum / generic
   alias). Residual: coarse behavioral metadata → *accepted + documented*, avoided in local mode.
2. **Forge or replay ingest.** Closed by HMAC + nonce(≥720 s) + timestamp window + idempotency
   + charset-restricted ids (no canonical-string injection).
3. **Take over a device's frame.** Closed by header-only hashed token, no token in URL/QR,
   device↔collector binding, revocation.
4. **Brick or own the device.** Cert pin to long-lived root + 2-root bundle + authenticated
   cacerts refresh; signed OTA + rollback; serial recovery.

## Explicitly out of scope (v1)

- Physical flash extraction from a stolen ESP32.
- Multi-tenant shared relay hosting.
- A relay operator's inference of coarse behavioral metadata in relay mode (use local mode).
- Supply-chain integrity of third-party libraries (track via dependency pinning, future work).

## The honest summary

If you run **local mode**, no third party sees anything; the threat surface is your own LAN.
If you run **relay mode**, the relay sees sanitized behavioral metadata — enough to infer
your work schedule and how many projects/accounts you run, never their names or contents.
Choose relay mode only when remote viewing is worth that trade.
