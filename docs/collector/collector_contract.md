# Collector Contract

## Responsibilities

- Read local provider/session/quota signals.
- Normalize into provider-independent events.
- Sanitize fields (default-deny; see `../security/sanitization_policy.md`).
- Aggregate sessions/quota/alerts and compute display priority (rules from `cloud_contract.md`).
- **Local mode:** serve the compact frame over the LAN (the local frame server).
- **Relay mode:** sign event batches and push to the cloud; retry with exponential backoff + jitter.
- Persist a **bounded** offline cache locally (relay mode).
- Never upload raw credentials or raw local work content.

## Hook Ingestion (fire-and-forget — never block the agent)

Provider hooks run synchronously and time out the agent if slow. The hook entry point
(`hook_sink`) MUST do only: append the raw event to a local queue (socket/file) and return
in well under 1 second. **All** sanitization, signing, aggregation, and upload happen in the
background collector daemon that drains the queue. A hook must perform zero network I/O.

## Adapter Order

1. Manual adapter.
2. Codex hooks adapter.
3. Claude Code hooks adapter.
4. Cockpit Tools / CodexBar adapter.
5. Future providers.

## Local Storage

Use platform-specific config/cache paths:

- Config: base URL, collector id, enabled adapters.
- Keyring: collector secret.
- Cache: offline event batches.
- Logs: local redaction and send status only.

## Event Lifecycle

```text
raw local signal
  -> adapter normalizes
  -> sanitizer validates and redacts
  -> event envelope
  -> payload hash
  -> HMAC signature
  -> HTTPS push
  -> ack or offline cache
```

## Offline Cache (relay mode)

- Store sanitized signed payloads or sanitized unsigned events, not raw input.
- Preserve event ids and source sequence; replay in original order.
- **Bounded:** max size (default 50 MB); when full, drop oldest and log the drop count
  (never silently truncate).
- On a **per-event** `sanitization_failed`/`unknown_field` result: quarantine that event to
  a local `dead_letter/` store (reason + payload hash only), drop it from the queue, and
  **continue** replaying the rest. A single poison event must never stall the queue.
- Retry uses exponential backoff **with jitter**, and the daemon adds small random jitter
  to upload timing to blunt the work-schedule side-channel (see `../security/threat_model.md`).

## Provider Adapter Rules

- Do not parse full transcripts unless a future task explicitly allows a local-only derived summary.
- Do not upload raw hook payloads.
- Do not upload `cwd`, `transcript_path`, raw file paths, raw shell commands, raw tool inputs, raw tool outputs, prompts, or model output.
- Treat quota values as estimated unless source is explicit and stable.
- Add confidence for every quota window.
- Codex and Claude support starts with hooks. OTel or CLI status sources are optional later inputs and must pass the same sanitizer.

## Codex / Claude MVP

| Provider | Primary Source | Required Alerts | Quota Source |
|----------|----------------|-----------------|--------------|
| Codex | Lifecycle hooks | `PermissionRequest` -> `WAITING` | Manual/unknown in v1 |
| Claude | Lifecycle hooks + `Notification` | permission/input wait -> `WAITING` | Manual/unknown in v1 |

Provider-specific contracts live in `docs/providers/`.

## Tests Required Before Provider Adapters

- Path redaction.
- Token/cookie rejection.
- Prompt-length rejection.
- Signature canonicalization.
- Offline replay idempotency.
- Cloud rejection response handling.
