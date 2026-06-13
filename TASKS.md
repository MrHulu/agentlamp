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

## Open-source / public-repo polish (repo went public 2026-06-03 · github.com/MrHulu/agentlamp)

- [ ] TASK-016 (P1): CI — GitHub Actions running the suites on push/PR so the README "tests"
  badge is real (currently a static badge). Recipe: a **python** job
  (`pip install -e ".[all]" pytest httpx` → `pytest server/tests/ src/collector/tests/ -q`, ~305
  tests) + a **cloud** job (`cd src/cloud && npm ci && npx tsc --noEmit && npx vitest run`, 120
  tests); optionally a firmware `pio run -e waveshare-s3-lcd-147` job. Verify it goes green, then
  swap the static `tests-455-passing` badge for the live `actions/workflows/ci.yml/badge.svg`.
- [ ] TASK-017 (P2): `CODE_OF_CONDUCT.md` (Contributor Covenant). LICENSE / SECURITY.md /
  CONTRIBUTING.md already exist.
- [ ] TASK-018 (P2): Remove or repurpose the untracked `src/collector/usb_bridge.py` (the rejected
  USB-cable transport, superseded by the cloud relay — kept out of the repo, still on disk).

> **Relay is DEPLOYED + live** (Cloudflare Worker + Durable Object + KV; end-to-end verified
> 2026-06-03). The `wrangler login` + `wrangler deploy` step is owner-gated and documented in
> `docs/cloud/deploy.md`; the live URL + tokens live in `~/.config/agentlamp/relay-deploy.txt`
> (NOT in the repo). Remaining hardware-gated item: **TASK-011 (R3)** physical-LCD eyeball.

## Readers & multi-device (2026-06-07) · design-ready, owner-gated to land

> Investigation (devlog 17) confirmed multi-collector fan-in + multi-reader are **既有能力，
> 零核心改动**. Specs are implementation-ready; landing touches a live daemon + cloud admin →
> needs owner go. Plan: `docs/plans/2026-06-07-multi-device-cloud-aggregation.md`.

- [ ] TASK-019 (P1): Multi-device cloud aggregation — enroll machine #2 as a distinct collector
  (unique `collector_id`) + flip each daemon to relay + per-machine `account_alias` to keep
  machines distinct on the phone. **No cloud/collector code change** (single-DO fan-in already
  works). Spec: the 2026-06-07 plan §3–§4. Acceptance: plan §5.
- [~] TASK-020 (P1): iPhone widget reader — IMPLEMENTED + conformance-tested + review-hardened
  (not yet on-device). `readers/iphone-widget/`: `agentlamp-widget.js` is now the phone-facing
  **single-file** Scriptable template (white HULU card, Chinese labels, Claude/Codex quota
  remaining %, plan chips, reset times); `frame-view.js` remains shared pure logic for alerts/tests;
  tests now include `frame-view.test.cjs` + `widget-template.test.cjs` (**15 zero-dep Node tests**,
  all green). **2026-06-07 review round**
  (3 codex + 3 my-subagents, all findings verified): fixed C1 — auth failure (401/403/404) now
  shows **PAIRING REQUIRED** + drops cache (a revoked phone must not keep rendering last-good
  data; cache only on 429/5xx/transport); C2 — quota surfaces the **higher-risk** of w5/week
  (was hiding `week`); fleet `+N more` now recounts rows dropped by the local 3-row cap.
  Remaining = run on a real phone + capture an on-device screenshot for `readers/`.
- [~] TASK-021 (P2): instant alerts — IMPLEMENTED (client path) + tested + review-hardened.
  `agentlamp-alert.js` fires a notification (+ optional Pushcut webhook) on `scene=alert`,
  deduped via `frame-view.shouldAlert`. **2026-06-07 fixes:** C3 — dedup key now includes
  `primary.task` (a changed WAITING/ERROR task re-fires; was a 4-reviewer-consensus bug); C4 —
  the dedup key is persisted **only after** a delivery succeeds (a failed send retries instead
  of being permanently swallowed); local + webhook deliver independently; a revoked device fires
  a deduped **re-pair** notice. DEPLOY.md now documents the real iOS Shortcuts scheduling limit
  (no built-in N-minute trigger → Pushcut Automation Server / staggered automations / Worker-cron).
  Remaining = schedule on a real phone. Server-side Worker-cron variant owner-gated (relay I1–I5).
- [x] TASK-022: collector CLI admin-freshness fix ✅ 2026-06-07 — `agentlamp enroll` / `revoke`
  hit the relay's `/admin` routes, but `_admin_post` sent only the bearer; the DO's
  `checkAdminReplay` (devlog/16) **requires** a fresh `X-ACO-Timestamp` (±300s) + single-use
  `X-ACO-Nonce`, so live enroll/revoke would 401 `admin_stale`. Fixed `_admin_post` to mint +
  send both; tightened the test stub to mirror `checkAdminReplay` (so this class of bug can't
  hide again) + assert fresh-nonce-per-call. Found by the 2026-06-07 codex review (D4).

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
