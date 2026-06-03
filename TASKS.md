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

- [ ] TASK-006: Pairing, device binding, token rotation (local admin)
  - One-time pairing **code** exchanged for a read-only token (token never in URL/QR; server
    stores hash). Device↔collector binding. `collector_id`/`device_id` charset enforced.

- [~] TASK-007: relay mode (cloud) — IN PROGRESS (Boss chose to build it 2026-06-02; it's the
  reason the screen uses WiFi: view it away from the laptop's LAN). 5 phases:
  - [x] **P1 signed ingest** ✅ — `POST /api/v1/collectors/{id}/events`: HMAC canonical-string
    verify, ±300s ts window (+server_time resync), nonce replay ≥720s, idempotency, body/batch
    limits, independent cloud sanitize gate, per-event results. `ingest.py` + route; 13 security
    tests (security_model.md acceptance); 160 server tests green; local mode untouched. Devlog 15.
  - [ ] P2 collector signed-push (daemon signs+POSTs in relay mode; dead-letter on reject).
  - [ ] P3 public-deploy security (TOTP admin non-localhost, device token/pairing, rate limits,
    retention purge 30d, audit, key rotate/revoke).
  - [ ] P4 device TLS (firmware HTTPS + pinned ISRG Root X1 + NTP + `…/cacerts` refresh).
  - [ ] P5 deploy + end-to-end (host w/ real TLS; collector→cloud→device over HTTPS from a
    foreign network). Hosting decision (Fly.io vs VPS+Caddy) at this phase.
  - **Single-owner only** — no public registration / shared hosting in v1.

- [ ] TASK-008: 24-hour stability + weak-network test
  - Restart recovery, Wi-Fi loss/recovery, malformed JSON, memory/flicker, poison-event drain.

## vNext — usability (post-TASK-005) · spec: `docs/devlog/08-vnext-requirements.md`

- [~] TASK-011 (P0): Verify + polish the physical LCD layout (R3) — DONE in software +
  flashed (twice); **pending Boss's physical-LCD eyeball sign-off**. Fixed: clean fleet
  labels + separate `xN` badge, `drawFit` shrink-then-ellipsize, disjoint name/badge/status
  pixel budgets, summary counts only rendered rows. TWO adversarial reviews: a 3-lens cloud
  review (5 defects) + a 4-AI council (8 defects across R1/R2/R3, cross-platform/-machine/DRY
  — see devlog 12). All fixed; firmware rebuilt + reflashed.
  - ✅ **DHCP-drift fixed permanently (mDNS server discovery)**: the Mac's IP had drifted
    `192.168.1.148 → .147`, killing the orb (it polled the IP pinned in NVS). Firmware now
    discovers the server via `<host>.local` mDNS (auto-follows the live IP) at boot + every 3
    transport fails; stored IP is the fallback. Proven on the boot log (`mdns: server ->
    http://192.168.1.147:8787` → `frame ok`). Self-heals replug / reboot / DHCP / network
    change — no reflash/re-provision. See devlog 12 "Follow-up". (Also closes part of R6-ops.)
  - Orb is LIVE again, rendering a staged worst-case fleet (33-char label + badges) for the
    eyeball.
- [~] TASK-012 (P1): Per-session identity (R4) — DONE via session titles (3-lens spike →
  Boss chose session_title). `claude --name`/`/rename` → the lamp shows the title instead of
  the folder; same-folder named sessions split into distinct rows; unnamed aggregate by
  project. Collector+server only (no firmware change). New `safe_title` sanitizer (drops
  path/secret titles, local readable / relay HMAC). LIVE end-to-end verified
  (`live-title-test` rendered on the orb). Codex has no title → folder fallback. Devlog 13.
  - 2-lens privacy leak-review found+FIXED a real HIGH (separator-injection reconstructed a
    secret past the pre-normalization scan) + MEDIUM (path/email adjacency erased):
    `contains_forbidden` now also scans an invisibles-stripped copy; `safe_title` rejects
    zero-width/control + hard-rejects `/\@` + re-scans the normalized label. 0 leaks on all 11
    attack vectors. 50 collector + 147 server tests green (26 new). 4 LOW = documented residual.
- [ ] TASK-013 (P1): Fleet status breakdown — show the mix, not just the dominant (R5).
  (Spike recommends this as the complementary precision win: `ai-center x5 · 3C 2R`.)
- [ ] TASK-014 (P2): Ops hardening — log rotation, commit launchd plists + runbook (R6).
- [ ] TASK-015 (P2): Quota/usage on the lamp — a real source beyond manual entry (R7).

## Done

- [x] USB-cable transport — the lamp no longer needs WiFi ✅ 2026-06-02
  - Boss took laptop+lamp out; lamp couldn't connect (saved WiFi `moza-office` absent; Mac on
    ethernet, no WiFi for the lamp). NOT a hardcode (WiFi creds live in NVS). Fix: feed the lamp
    over the USB cable it's already powered by — `usb_bridge.py` (launchd) writes `GET /frame` to
    `/dev/cu.usbmodem*`; firmware `readUsbFrame()` reads + renders it, prefers USB, WiFi dormant.
    Verified `via=usb` on boot, no WiFi. Found+fixed a real bug: USB-CDC RX FIFO (256 B) < frame
    (464 B) truncated frames → `Serial.setRxBufferSize` before `begin`. Devlog 14. WiFi+mDNS stay
    as fallback. (Stop the bridge before flashing — it holds the port.)
- [x] TASK-010 (P0): Fleet count = ACTIVE agents ✅ 2026-05-31
  - `_fleet_block` now counts only active sessions per project (`_is_active`: not
    idle/done/unknown/stale/offline), drops all-idle projects, row status = top active
    status; clean project label (no baked `xN`), count in the structured field. Verified
    live: real `ai-center` showed `x3` (active) not the prior inflated total. +3 tests
    (active-only count, drop-all-idle, clean-label). 115 server tests green. Shares
    `_is_active` with the scene selector so "how many busy" can't drift.
- [x] TASK-009 (P0): Codex sessions on the lamp ✅ 2026-05-31
  - Installed 7-event hooks in `~/.codex/config.toml` (additive, backed up); persisted
    hook trust in real `~/.codex` ("Trust all and continue"). LIVE-verified: a real
    interactive Codex session drove the orb (primary `Codex · CODING · agentlamp-cx-verify`)
    while Claude sessions showed in the same fleet. Arc IDLE→THINKING→READING/CODING→DONE
    confirmed on real captured payloads. 48 collector tests still green.
  - Findings + recipe: `docs/devlog/10-codex-hooks-live.md`. **`codex exec` does NOT fire
    hooks** (interactive only); SessionStart fires at first prompt (thread scope).
  - KNOWN LIMITATION: Codex PostToolUse carries no exit_code → a silent non-zero shell exit
    can't map to ERROR (platform gap). Structured/MCP failures + Claude ERROR unaffected; no
    false-positive heuristic added (would regress "no false amber"). Documented in devlog 10.
- [x] TASK-005: Wire Codex + Claude hook adapters (fire-and-forget) ✅ 2026-05-31
  - Live: real hook pipeline → orb; dual Claude+Codex normalize; readable local labels /
    relay HMAC; offline-only-on-dead-collector; fleet overview; firmware self-heal.
  - Commits `8d525f4` (collector) · `de1e9f4` (server) · `7b7b969` (firmware). 48+113 tests.
  - Follow-ups split into vNext (TASK-009..015) — see `docs/devlog/08-vnext-requirements.md`.
- [x] Multi-AI council design review (4 AIs) + P0/P1 hardening of the doc set ✅ 2026-05-30
  - Local-first reframe; sanitization mechanism; multi-tenant decision (single-owner v1);
    hook fire-and-forget; ingest hardening; firmware reality; open-source scaffolding.
