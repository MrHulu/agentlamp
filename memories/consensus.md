# Consensus

## Last Updated

2026-05-30 - Renamed to AgentLamp; multi-AI council design review applied (P0/P1 hardening + local-first reframe).

## Current Phase

M0: requirement freeze and contract hardening (council-hardened).

## Current State

- Project renamed Agent Cockpit Orb → **AgentLamp**; to be open-sourced as a teaching example.
- Original imported spec archived at `docs/original/agentlamp_ai_spec.md` (carries a "do not implement directly" banner; canonical contracts override it).
- Full contract set + new files: `threat_model.md`, `BUILD.md`, `LICENSE` (MIT), `SECURITY.md`, `CONTRIBUTING.md`, `.env.example`.
- A 4-AI council (claude/codex/opencode/openclaude) reviewed the design 2026-05-30; raw at ai-center `.output/multi-ai/agentlamp-review/runs/20260530-115834/`.
- No code implementation yet. No git repo initialized yet.

## Key Decisions

- The project lives at `/Users/hulu/huluman/agentlamp`.
- **Local mode is the default**: the collector serves the frame over the LAN; the public cloud is an **optional relay** for remote viewing only. (Boss decision 2026-05-30, adopting council's local-first reframe.)
- **v1 is single-owner self-host** — no shared/multi-tenant hosting; device↔collector binding scopes data within one owner.
- Sanitization is a **mechanism, not a claim**: local alias map; unmapped → keyed-HMAC label (local pepper), never a path basename; all identity-bearing fields are enums (`task_label`/`error_label`/`model`/`account_alias`), never free text or plan tiers; low-entropy ids use keyed HMAC, not plain SHA256.
- Hooks are **fire-and-forget** (append to local queue, return <1 s, zero network I/O); daemon does sanitize/sign/serve.
- Ingest (relay) hardened: `kid`, nonce ≥720 s, per-event results + dead-letter, batch/body limits, charset-restricted ids, independent cloud gate, retention purge (30 d).
- Firmware: PSRAM treated as required (memory budget); pin long-lived root (not intermediate) + authenticated cacerts refresh; signed OTA + rollback; SoftAP WiFi provisioning.
- Browser simulator + local frame server come before hardware/relay iteration.
- Codex/Claude v1 via hooks first (tolerate unknown event names); quota starts manual/unknown.

## Next Action

Execute `TASK-002`: build the **local frame server** + browser simulator (cloud-free), then the collector sanitizer mechanism (TASK-003) with the Required Fixtures as the gate.

## Open Risks

| Risk | Status | Mitigation |
|------|--------|------------|
| Provider quota may be imprecise | Open | Confidence + estimated fields; start manual/mock |
| Sanitization mechanism not yet implemented | Open | Required Fixtures are the gate before any provider adapter ships |
| Behavioral metadata side-channel (relay mode) | Accepted + documented | Local-mode default; upload jitter; 30-day purge; `threat_model.md` |
| ESP32 fits the board (PSRAM) | Open | Confirm revision in BUILD.md; memory budget table; PSRAM required for framebuffer |
| Provider hook names drift | Open | Unknown-event tolerance + versioned, dated mapping per adapter |
| Small screen overload | Open | fleet ≤ 6, quota ≤ 2, one focus session per frame |
