# TASKS

> Build order is **local-mode-first** (see `docs/architecture/architecture.md` → Deployment
> Modes). The cloud relay is the last, optional step. P0 hardening from the 2026-05-30 multi-AI
> design review is baked into the acceptance gates below.

## In Progress

- [ ] TASK-001: Freeze M0 docs and contracts (LOCAL-FIRST + council hardening)
  - Deliver: product spec, architecture (local/relay modes), API contracts, security model,
    sanitization **mechanism** (alias map + keyed HMAC), threat model, BUILD/quickstart, LICENSE.
  - Verify: link check; no duplicate/conflicting auth or frame schema sections; examples use
    generic aliases + enums only (no basename / plan tier / real model id / plain SHA256).

## Pending

- [ ] TASK-002: Build the **local frame server** + browser simulator (NO cloud)
  - Serve `GET /api/v1/device/{device_id}/frame` from in-process collector state over the LAN.
  - Return schema v1 frames under 2 KB; fleet ≤ 6, quota ≤ 2; version header negotiation.
  - 172×320 simulator renders Fleet/Focus/Quota/Alert/Offline/Stale; screenshot checks.

- [ ] TASK-003: Build collector core + sanitizer **mechanism**
  - Local alias map (`aliases.toml`); unmapped → keyed-HMAC label (local pepper in keyring),
    never basename. Enum-only fields (`task_label`/`error_label`/`model`/`account_alias`).
  - **Gate:** the Required Fixtures in `docs/security/sanitization_policy.md` pass —
    `unmapped_cwd`, `low_entropy_branch`, `plan_tier_account`, `real_model_id`,
    `error_with_path`, `free_text_task`, `unknown_field`, `stable_label`.
  - Aggregation + display priority (rules from `cloud_contract.md`); session TTL/heartbeat.

- [ ] TASK-004: Build firmware mock frame renderer
  - PlatformIO; fetch JSON frame (HTTP on LAN), render local scenes, no HTML.
  - SoftAP WiFi provisioning; staleness from local elapsed time; cache + Offline/Stale.
  - **Gate:** fits the memory budget (PSRAM confirmed); malformed JSON never crashes; no
    token/full-frame over serial.

- [ ] TASK-005: Wire Codex + Claude hook adapters (fire-and-forget)
  - TASK-005A: provider normalization models + sanitizer fixtures (depends on TASK-003 gate).
  - TASK-005B: Codex `hook_sink` — append-to-local-queue + return <1 s, zero network I/O.
  - TASK-005C: Claude `hook_sink` — same; tolerate unknown hook event names (no hard-fail).
  - TASK-005D: render concurrent Codex+Claude sessions in simulator.
  - TASK-005E: keep quota `manual`/`unknown` unless a stable explicit source is documented.
  - Gate: prove no prompt/transcript/source path/command/tool content upload via tests.

- [ ] TASK-006: Pairing, device binding, token rotation (local admin)
  - One-time pairing **code** exchanged for a read-only token (token never in URL/QR; server
    stores hash). Device↔collector binding. `collector_id`/`device_id` charset enforced.

- [ ] TASK-007: OPTIONAL relay mode (cloud ingest) — last
  - Signed `POST /api/v1/collectors/{collector_id}/events` with `kid`, nonce(≥720 s), per-event
    results + dead-letter quarantine, batch/body limits, independent cloud sanitization gate.
  - Cert pin to long-lived root + cacerts refresh; signed OTA + rollback if OTA ships.
  - Admin: MFA required (non-localhost), CSRF, lockout; retention purge (30 d); encryption at rest.
  - **Single-owner only** — no public registration / shared hosting in v1.

- [ ] TASK-008: 24-hour stability + weak-network test
  - Restart recovery, Wi-Fi loss/recovery, malformed JSON, memory/flicker, poison-event drain.

## Done

- [x] Multi-AI council design review (4 AIs) + P0/P1 hardening of the doc set ✅ 2026-05-30
  - Local-first reframe; sanitization mechanism; multi-tenant decision (single-owner v1);
    hook fire-and-forget; ingest hardening; firmware reality; open-source scaffolding.
