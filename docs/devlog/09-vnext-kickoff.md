# 09 — vNext kickoff: recall everything + start the next iteration

> Read this FIRST in the new session. It restores the full context of the TASK-005
> session (collector + live-orb fixes) so you can continue without re-deriving it. The
> *what to build* is in `08-vnext-requirements.md`; this doc is the *how to recall + the
> hard-won facts + the ready prompt*.

## Recall the prior session with /ai-history

Run these in the new session to pull back the full detail of how everything was built and
every trap hit (the TASK-005 session was long — these queries surface the specific turns):

- `/ai-history AgentLamp TASK-005 collector hook_sink daemon normalize`
- `/ai-history AgentLamp orb offline collector heartbeat sleep semantics`
- `/ai-history AgentLamp device wedge code=-1 firmware self-heal reboot WiFi`
- `/ai-history AgentLamp readable labels project hash fleet HMAC local mode`
- `/ai-history AgentLamp idle_prompt WAITING notification false amber`
- `/ai-history AgentLamp launchd server daemon KeepAlive serial usbmodem`

## What's DONE (TASK-005, committed on `main`)

| commit | what |
|---|---|
| `8d525f4` | `feat(collector)`: real Codex/Claude hook pipeline (`src/collector/`) |
| `de1e9f4` | `fix(server)`: offline semantics + readable labels + fleet overview |
| `7b7b969` | `fix(firmware)`: self-heal reboot on a wedged network stack |

The orb reflects real agent activity automatically. The collector is `src/collector/`:
`hook_sink.py` (fire-and-forget queue writer) · `daemon.py` (drain→normalize→sanitize→POST)
· `normalize.py` (event→status, cwd→label) · `netpost.py` (proxy bypass) · `config.py`
(env + pepper) · `install_hooks.py` (provider config gen). 48 collector + 113 server tests.

## The running stack (live right now, launchd-managed)

```
ESP32 orb (self-heal fw)  ──WiFi "HULU"──  router  ──ethernet──  Mac
  ip 192.168.1.169                                                 ├─ server  :8787  (launchd com.hulu.agentlamp.server)
  polls 192.168.1.148:8787                                         └─ daemon         (launchd com.hulu.agentlamp.daemon)
                                                                        queue ~/.agentlamp/queue
```

| | |
|---|---|
| Repo | `/Users/hulu/huluman/agentlamp` (git `main`, no remote — local commits only) |
| Server | `cd server && ../.venv/bin/python -m agentlamp_server` (or via launchd) |
| Daemon | `.venv/bin/python src/collector/daemon.py` (or via launchd) |
| launchd | `~/Library/LaunchAgents/com.hulu.agentlamp.{server,daemon}.plist` (RunAtLoad+KeepAlive) |
| Restart a unit | `launchctl kickstart -k gui/$(id -u)/com.hulu.agentlamp.{server,daemon}` |
| Device serial | `/dev/cu.usbmodem1101` @115200 (native USB-CDC; reading does NOT reset) |
| Re-flash | `cd firmware && ../.venv/bin/pio run -e waveshare-s3-lcd-147 -t upload --upload-port /dev/cu.usbmodem1101` |
| Reset device | pulse EN via pyserial DTR/RTS (see the session's reset snippet) |
| Frame check | `curl --noproxy '*' -s -H "Authorization: Bearer dev-local-token" http://127.0.0.1:8787/api/v1/device/orb-01/frame` |
| Simulator | `GET http://127.0.0.1:8787/preview` (renders the frame at device res) |
| Global hooks | `~/.claude/settings.json` (Claude installed; Codex NOT yet — see R1). Backup `…/settings.json.bak-1780196851` |

## GOTCHAS — hard-won this session (do NOT relearn these)

1. **`curl`/daemon to the LAN must bypass Clash.** Always `curl --noproxy '*'`; the daemon
   uses `urllib build_opener(ProxyHandler({}))`. NEVER touch the system proxy.
2. **The orb's "offline" was the DEVICE'S firmware offline**, not the server scene. Debug
   the right layer: server frame says `sleep/focus` but orb shows offline ⇒ device can't
   poll. Read the serial (`frame fail code=-1 fails=N`). `code=-1` with WiFi connected = a
   wedged LWIP stack → the firmware now self-reboots after ~5 min (`FAIL_BEFORE_REBOOT=75`
   in `firmware/src/main.cpp`).
3. **Server "offline" scene = collector heartbeat stale ONLY.** Aged/finished/idle sessions
   sleep, never offline (`state.py::_effective_status` exempts DONE/IDLE; `_select_scene`
   never paints offline from aged sessions). The daemon heartbeats every 30s; threshold 90s.
4. **`idle_prompt` ≠ WAITING.** Only `permission_prompt`/`PermissionRequest`/
   `elicitation_dialog` light amber WAITING. `idle_prompt` (finished, waiting for input) →
   IDLE. (`normalize.py::_WAIT_NOTIFICATION_TYPES` / `_IDLE_NOTIFICATION_TYPES`.)
5. **Local labels vs relay HMAC.** Local single-owner mode (default `AGENTLAMP_LOCAL_LABELS=1`
   / server `AGENTLAMP_LOCAL_DISPLAY=1`) shows the real folder **basename** (`ai-center`).
   Relay/strict mode (`=0`) HMAC-collapses to `project-<hmac6>`. Full path / parent dirs /
   secrets / commands / prompts NEVER leak in either mode (tests prove both).
6. **`Stop` fires per turn** in interactive Claude → DONE → sleep during the wait (that's
   why DONE/IDLE must not age to offline).
7. **Restarting server/daemon resets in-memory session state** (empty until sessions re-fire
   their next hook). Brief, expected. Counts in the fleet only reflect recently-reported
   sessions.
8. **Env knobs** (state.py / config.py): `AGENTLAMP_LOCAL_LABELS`, `AGENTLAMP_LOCAL_DISPLAY`,
   `AGENTLAMP_STALE_AFTER_S` (120), `AGENTLAMP_OFFLINE_AFTER_S` (600),
   `AGENTLAMP_HEARTBEAT_STALE_S` (90), `AGENTLAMP_DRAIN_INTERVAL_S`, `AGENTLAMP_QUEUE_*`.
9. **Verify on the PHYSICAL LCD**, not just serial `frame ok`. The pixels were never seen
   this session — R3 is exactly this gap.

## What to build next

`docs/devlog/08-vnext-requirements.md` — R1–R8 with symptom/root-cause/approach/acceptance.
P0: **R1 Codex on the lamp · R2 fleet count semantics · R3 LCD layout verify+polish.**
Mirrored as TASK-009..TASK-015 in `TASKS.md`.

## Ready prompt for the new session

Paste this to start:

> Continue AgentLamp vNext. First read `docs/devlog/09-vnext-kickoff.md` (recall + gotchas
> + running stack) then `docs/devlog/08-vnext-requirements.md` (R1–R8 backlog). Use
> `/ai-history` with the queries in the kickoff doc to pull back the TASK-005 session
> detail. TASK-005 (collector + live orb) is DONE and committed on `main` (`8d525f4`,
> `de1e9f4`, `7b7b969`); the server+daemon run under launchd and the orb is live. Start
> with the P0 items: **R1 — install + live-verify Codex hooks so Codex sessions show on the
> lamp; R3 — render the fleet/focus scenes in the `/preview` simulator, screenshot the
> 2/3/6-project + long-name + alert cases, and fix any truncation/readability before
> re-flashing; R2 — fix the fleet count so it reflects *active* agents, not idle/done.**
> Constraints: don't commit without my approval (use `/archive`); never touch the system
> proxy (`--noproxy '*'`); verify on the physical LCD, not just serial; keep all 48+113
> tests green and add tests for new behavior; before any device flash, confirm the build
> then upload to `/dev/cu.usbmodem1101`. Server is already running via launchd; if not:
> `cd /Users/hulu/huluman/agentlamp/server && ../.venv/bin/python -m agentlamp_server`.
