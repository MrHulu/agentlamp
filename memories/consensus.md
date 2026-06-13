# Consensus

## Last Updated

2026-06-11 - iPhone widget template updated to the latest single-file phone script; second-computer deployment path documented.

## Current Phase

Relay + multi-reader hardening. Core cloud/collector/reader code exists; current focus is phone deployment, multi-computer collector rollout, and ops documentation.

## Current State

- Repo is public as `github.com/MrHulu/agentlamp`; local working tree contains substantial uncommitted cloud/collector/widget changes. Do not commit/push without Boss approval.
- Relay mode is deployed on Cloudflare Worker + Durable Object + KV. This Mac is enrolled and pushes sanitized state in relay mode; owner-readable labels are enabled locally for the private relay.
- Cloud frame supports `brand`, per-provider quota blocks, plan tiers (`max_5x`/`max_20x`/`pro`/etc.), and reset epochs. Widget displays quota as **remaining** while cloud frame keeps the canonical used ratio.
- iPhone widget reader lives under `readers/iphone-widget/`. `agentlamp-widget.js` is now the single-file Scriptable template matching the latest phone script: white HULU card, Chinese labels, Claude/Codex quota remaining %, plan chips, reset times, and pairing-required behavior on 401/403/404.
- `frame-view.js` remains the shared pure logic for alert/tests; `agentlamp-alert.js` still imports it. Reader test coverage is 15 zero-dep Node tests.
- Multi-computer fan-in needs no cloud algorithm change: every collector writes into the singleton RelayDO. A second computer needs its own `collector_id`/kid/secret plus a distinct `AGENTLAMP_ACCOUNT` so the phone can distinguish machines.

## Key Decisions

- The project lives at `/Users/hulu/huluman/agentlamp`.
- **Local mode is the default** for the public teaching project, but Boss's private setup currently uses relay mode for phone viewing.
- **v1 is single-owner self-host** — no shared/multi-tenant hosting; device↔collector binding scopes data within one owner.
- Sanitization is a **mechanism, not a claim**: local alias map; unmapped → keyed-HMAC label (local pepper), never a path basename; all identity-bearing fields are enums (`task_label`/`error_label`/`model`/`account_alias`), never free text or plan tiers; low-entropy ids use keyed HMAC, not plain SHA256.
- Hooks are **fire-and-forget** (append to local queue, return <1 s, zero network I/O); daemon does sanitize/sign/serve.
- Ingest (relay) hardened: `kid`, nonce ≥720 s, per-event results + dead-letter, batch/body limits, charset-restricted ids, independent cloud gate, retention purge (30 d).
- Firmware: PSRAM treated as required (memory budget); pin long-lived root (not intermediate) + authenticated cacerts refresh; signed OTA + rollback; SoftAP WiFi provisioning.
- Codex/Claude v1 via hooks first (tolerate unknown event names); quota source is now real Claude OAuth usage + Codex rollout/CodexBar-adjacent snapshots where available.
- Phone widget is intentionally single-file for deployment ergonomics. Optional alert automation can still use `frame-view.js`.

## Next Action

Update the on-phone `AgentLamp` Scriptable script from `readers/iphone-widget/agentlamp-widget.js`, then enroll the other Mac using `readers/iphone-widget/DEPLOY.md` → "Add another computer (Mac)". Remaining acceptance for TASK-020/TASK-019: real phone screenshot + two-machine relay verification.

## Open Risks

| Risk | Status | Mitigation |
|------|--------|------------|
| Provider quota may be imprecise | Partly mitigated | Claude uses live OAuth usage; Codex remains snapshot-based when no live endpoint exists |
| Sanitization mechanism not yet implemented | Closed for current relay path | Default-deny sanitizer + cloud validate-only gate implemented and covered by parity tests |
| Behavioral metadata side-channel (relay mode) | Accepted + documented | Local-mode default; upload jitter; 30-day purge; `threat_model.md` |
| ESP32 fits the board (PSRAM) | Open | Confirm revision in BUILD.md; memory budget table; PSRAM required for framebuffer |
| Provider hook names drift | Open | Unknown-event tolerance + versioned, dated mapping per adapter |
| Small screen overload | Open | large widget shows quota + up to 5 sessions; medium prioritizes quota |
