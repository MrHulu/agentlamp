# 08 — vNext requirements: make the multi-agent lamp actually usable

> Written 2026-05-31 after TASK-005 shipped (collector live, 3 commits: `8d525f4`
> collector, `de1e9f4` server fixes, `7b7b969` firmware self-heal). TASK-005 made the
> orb reflect real agent activity. **This doc is the backlog for the next version: the
> lamp works, but it is not yet *legible* for an operator running many concurrent
> sessions.** Each item below is a requirement with symptom → root cause → approach →
> acceptance → open questions, so the next session can pick any one up and execute.

## Where we are (entering vNext)

- The pipeline is live + self-healing: `hook_sink` (fire-and-forget) → `daemon`
  (drain → normalize → sanitize → proxy-bypassed POST to `127.0.0.1:8787/admin/event`)
  → frame server → ESP32 orb. Server + daemon run under launchd (`RunAtLoad`+`KeepAlive`).
- Verified live: a real `claude -p` session drives the full arc
  THINKING→READING→CODING→TESTING→ERROR→DONE→sleep; the device self-reboots out of a
  wedged WiFi stack; offline now fires only when the collector is genuinely dead;
  `idle_prompt` no longer false-lights WAITING; the orb shows readable folder names
  (`ai-center`) and a fleet overview (`ai-center x2`) when ≥2 agents are active.
- **The gap:** an operator running 5+ sessions (often in the SAME folder) still can't
  fully read or trust the lamp. The items below close that gap.

## Priority

- **P0 (do first):** R1 Codex on the lamp · R2 fleet count semantics · R3 LCD layout
  verification+polish (operator literally stares at this).
- **P1:** R4 per-session identity · R5 fleet status breakdown.
- **P2:** R6 ops hardening · R7 quota/usage display · R8 cosmetics.

---

## R1 — Codex sessions never appear on the lamp  (P0, TASK-009)

**Symptom.** Operator runs Codex CLI sessions concurrently with Claude; the orb shows
only Claude. Codex work is invisible.

**Root cause.** Only the **global Claude** hooks are installed
(`~/.claude/settings.json`, merged this session, backup `settings.json.bak-1780196851`).
Codex hooks (`~/.codex/config.toml`) were never installed, so Codex fires nothing.
`install_hooks.py` already *generates* the Codex block (`--print codex` /
`--write-codex`) but it was never applied.

**Approach.**
1. `cd src && ../.venv/bin/python -m collector.install_hooks --write-codex ~/.codex/config.toml`
   (user-level, NOT repo-local — GH openai/codex#17532: repo-local `.codex` hooks may
   not fire interactively).
2. Codex requires **persisted hook trust** (or `--dangerously-bypass-hook-trust` one-off).
   Resolve trust so hooks fire in normal interactive use; document the exact step.
3. **Live-verify Codex events** (this is the open item carried from devlog 07): the
   normalize.py Codex paths (`shell`/`apply_patch` tool names, `tool_input.command` as a
   list, `PostToolUse`-as-only-failure-signal → `_tool_failed`) were unit-tested but
   never driven by a real Codex session. Run a real `codex` task that reads/edits/tests
   and confirm the arc on the orb + the daemon log diag.

**Acceptance.** A real Codex session drives the orb (THINKING→CODING/READING/TESTING→
DONE); with a Claude session also running, the fleet shows both
(`provider` distinguishes them if same project, or two project rows). Codex tool failure
→ ERROR live (the synthetic path proven this session; need it live).

**Files.** `src/collector/install_hooks.py` (`CODEX_EVENTS`), `src/collector/normalize.py`
(`_tool_failed`, `_tool_category`, `_command_from_tool_input`), `~/.codex/config.toml`.

**Gotchas.** Codex stdin is snake_case (`session_id,turn_id,cwd,tool_name,tool_input,
model,permission_mode`). Legacy `--notify` is unrelated (argv[1], kebab-case). Codex
`SubagentStop` is subscribed and maps to DONE.

**Open question.** When a Claude and a Codex session share a folder, the fleet row groups
by *project* so they merge into one `ai-center xN` row — do we want a provider split
there? (Interacts with R2/R5.)

---

## R2 — Fleet count includes idle/done sessions (the number lies)  (P0, TASK-010)

**Symptom.** `ai-center x5` can show `x5` when only 2 agents are actually working — the
other 3 finished or went idle but still count. The number doesn't match "how many agents
are busy."

**Root cause.** `state.py::_fleet_block` groups **all** `ordered` sessions by project and
counts every one, regardless of effective status. The row's status is the highest-priority
one, but the count is total-present, not active.

**Approach.** Decide + implement count semantics. Candidate (recommended): the row count =
**active** sessions in the project (status not in IDLE/DONE/UNKNOWN/STALE/OFFLINE), and the
label reads `ai-center 2/5` (active/total) OR just `ai-center x2` counting active only.
Also reconsider whether idle/done sessions should appear as their own fleet rows at all (a
project where everything finished could just drop off, or show once as "done"). Keep the
firmware-side "N active" summary consistent with the per-row counts.

**Acceptance.** With 5 sessions in ai-center where 2 are CODING and 3 are DONE, the fleet
row reflects "2 working" unambiguously (decided format), tested in
`server/tests/test_frame.py`. No regression to the ≥2-active→fleet scene rule.

**Files.** `server/agentlamp_server/state.py` (`_fleet_block`, maybe `_select_scene`'s
`active` partition), `server/tests/test_frame.py` (`test_fleet_groups_by_project_with_count`).

---

## R3 — Physical LCD layout never visually verified  (P0, TASK-011)

**Symptom.** All multi-agent rendering was verified via serial `frame ok` + the server
frame JSON — **the actual 172×320 pixels were never seen.** Unknown: does `ai-center x2 ·
coding` fit, does a long name (`moza-perception-analysis x3`) truncate or overflow, are
the fleet rows readable, does the count badge look right.

**Root cause.** No visual verification step. The fleet row left label is bounded to 96px
(`renderer.h::fleet` `drawFit(r.provider, 14, y, 96, …)`); long `project xN` strings get
shrunk by `drawFit` and may become unreadable.

**Approach.**
1. Use the **browser simulator**: `GET http://127.0.0.1:8787/preview` renders the live
   frame at device resolution. Drive `/admin/event` (or `/admin/reset`) to stage scenes
   (single focus, 2-agent fleet, 6-project fleet, long names, WAITING alert) and screenshot
   each via the agent-browser/playwright skill. Review readability.
2. Fix the renderer where needed: font sizes (mobile-typography floor), truncation strategy
   for long project names (ellipsis vs shrink), the `xN` badge placement, fleet row pitch
   (currently `y += 40`, max 5 rows).
3. Re-flash + eyeball on the real device for the final check (operator confirms).

**Acceptance.** Screenshots of focus + 2/3/6-project fleet + long-name + alert, all
readable; operator signs off on the physical device.

**Files.** `firmware/src/renderer.h` (`fleet`, `focus`, `drawFit`), `firmware/src/frame.h`
(`FleetRow`), `server/agentlamp_server/preview.py`, the `/preview` route.

**Gotcha.** Re-flash = `cd firmware && ../.venv/bin/pio run -e waveshare-s3-lcd-147 -t
upload --upload-port /dev/cu.usbmodem1101`. Read serial @115200 over `cu.usbmodem*`
(does not reset; DTR/RTS pulse resets). The board is native USB-CDC.

---

## R4 — Same-folder sessions are indistinguishable  (P1, TASK-012)

**Symptom.** 5 sessions in `ai-center` are identical to the lamp; the operator can't map
the screen to one specific terminal.

**Root cause.** Inherent to a project-keyed display: same cwd → same label. The
per-session key (`provider_session_id`, an HMAC) is never shown.

**Approach (needs a design decision).** Options, pick one:
- **(a) Accept it as an aggregate** (current) — the lamp answers "how many agents, what
  collectively, who needs me," not "which terminal." Document this as the intended mental
  model and stop here.
- **(b) Per-session discriminator** — show a short stable tag per session (e.g. 2 chars of
  the session-id HMAC, or a small sequence number assigned on SessionStart) so 5 ai-center
  sessions render as `ai-center·a3`, `ai-center·7f`, … The operator still has no external
  reference to map a tag to a terminal, so this mostly helps see "these are distinct."
- **(c) Session title** — **lead worth investigating:** Claude Code `SessionStart` stdin
  carries an optional `session_title`. If the operator titles sessions (or we derive one),
  the lamp could show it. Confirm the field exists + is populated in practice; if so, the
  normalize layer can pass a sanitized short title as a per-session label.

**Acceptance.** TBD by chosen option. If (c): a titled session shows its title on the orb;
untitled falls back to project + count.

**Files.** `src/collector/normalize.py` (SessionStart handling + a new per-session label),
`server/agentlamp_server/state.py` (Session fields + display), firmware focus/fleet rows.

**Open question.** This is the deepest UX question — recommend a short design spike before
coding: does the operator actually want per-terminal mapping, or is the aggregate enough
once R1–R3 land? Re-confirm with the operator after R1–R3.

---

## R5 — Fleet collapses the status mix  (P1, TASK-013)

**Symptom.** `ai-center x5 · coding` hides that it's really 3 coding + 2 reading.

**Root cause.** `_fleet_block` shows only the group's highest-priority status.

**Approach.** Either a compact mix in the row (`ai-center x5 · 3C 2R`) or a secondary line;
keep within the 96px label bound + the ≤5 fleet rows. Low effort, nice-to-have. Don't let
it crowd the actionable signal (WAITING/ERROR still interrupts via the alert scene).

**Acceptance.** Fleet row conveys the dominant + a hint of the mix without overflowing.

**Files.** `state.py::_fleet_block`, `firmware/renderer.h::fleet`.

---

## R6 — Ops hardening  (P2, TASK-014)

**Symptom / risk.** Long-running logs grow unbounded (`~/.agentlamp/{server,daemon}.log`);
launchd setup is undocumented in-repo; the daemon pepper lives at
`~/.config/agentlamp/pepper` (un-rotated). Server/daemon restart loses in-memory session
state (brief blank until sessions re-fire — acceptable but undocumented).

**Approach.** Log rotation (size cap + truncate, or `newsyslog`/launchd `StandardOutPath`
rotation); commit the two launchd plists as `docs/ops/launchd/*.plist` templates + a
`docs/ops/runbook.md` (install/start/stop/uninstall, the `LOCAL_LABELS`/`LOCAL_DISPLAY`/
liveness env knobs, the self-heal threshold). Note the pepper rotation story (rotating it
re-labels everything — fine in local mode where labels are basenames anyway).

**Acceptance.** Logs bounded; a fresh machine can reproduce the running stack from the
runbook; plists tracked in-repo (currently they live only in `~/Library/LaunchAgents/`).

**Files.** new `docs/ops/`, the two plists, maybe a `scripts/install-launchd.sh`.

---

## R7 — Quota / usage on the lamp  (P2, TASK-015)

**Symptom.** The orb can render a `quota` scene + quota windows, but nothing populates
them — the operator can't see Claude/Codex usage or reset countdowns.

**Approach.** v1 keeps quota `manual`/`unknown` (per adapter docs). A real source is a
feature: parse `claude` usage / a stable local summary, POST `/admin/quota`. Treat as
estimated unless a stable source is proven. Scope this as its own task; not urgent.

**Files.** `src/collector/` (a quota poller), `server` `/admin/quota` (exists),
`docs/providers/*_adapter.md` (Quota sections).

---

## R8 — Cosmetics / polish  (P2)

`xN` badge styling, fleet row pitch, transitions/easing between scenes, color tuning,
the em-dash empty-project placeholder, status word abbreviations. Batch these after R1–R5
land and the layout is verified (R3).

---

## Cross-cutting acceptance for vNext

- Every server/collector change keeps the 48 collector + 113 server tests green and adds
  tests for the new behavior.
- Privacy invariant unchanged: full path / parent dirs / secrets / commands / prompts never
  leave the machine; basename shows only in local mode; relay mode (`LOCAL_LABELS=0` /
  `LOCAL_DISPLAY=0`) still HMAC-collapses everything (proven by
  `test_no_raw_anything_survives_hmac_mode`).
- Anything touching the device is eyeballed on the physical LCD (R3 discipline), not just
  asserted via serial `frame ok`.
