# 06 — TASK-005 Kickoff: real Codex/Claude hooks → live status

> Written 2026-05-31 at the end of the hardware bring-up session, to start TASK-005 fresh.
> **Goal:** replace the manual `POST /admin/event` injection with REAL provider hooks, so the
> orb reflects actual Codex/Claude agent activity automatically. This is the difference
> between "a demo I poke with curl" and "a lamp that lights up when my agents work."

## Where things stand entering TASK-005

- **Hardware MVP works end-to-end** (devlog 05): `ESP32 ← WiFi(HULU) ← laptop frame server`.
  The orb renders whatever the server's state is; today that state is set by hand via
  `curl POST /admin/event`. The whole device + server stack is committed on branch `main`
  through devlog 05.
- **The adapter DESIGN already exists** — TASK-005 is implementation, not design:
  `docs/providers/codex_adapter.md`, `claude_adapter.md`, `provider_normalization.md`,
  `docs/collector/collector_contract.md`. Re-read these first; they have the event→status
  tables + config sketches.
- **The feed target (server side, already built):** `server/agentlamp_server/app.py`
  - `POST /admin/event` — body shorthand `{provider, account, status, project, task,
    tool_category, model, error_label, needs_attention, provider_session_id}` →
    `_to_envelope()` wraps it → the **default-deny sanitizer runs** (`sanitize.py`) →
    `state.apply_event()` updates the frame. `account`/`project` are expected to already be
    **neutral aliases**.
  - `POST /admin/quota`, `POST /admin/heartbeat`, `POST /admin/reset`, `GET /preview`.
  - Sanitizer to REUSE (don't reinvent): `server/agentlamp_server/sanitize.py`
    (alias map + keyed HMAC + enum coercion). Aggregation: `state.py`.

## Architecture to build

```
Claude/Codex hook fires (PreToolUse / Notification / Stop / UserPromptSubmit / SessionStart...)
   │  stdin = JSON {session_id, cwd, hook_event_name, tool_name, tool_input, ...}
   ▼
hook_sink   (FAST, <1s, fire-and-forget): append the raw hook JSON to
            ~/.agentlamp/queue/<ts>-<pid>.json  then  exit 0.   ZERO network here.
   ▼
collector daemon (background, long-running): drains ~/.agentlamp/queue/
   → normalize: hook_event_name + tool_name → Status enum + tool_category
   → SANITIZE: cwd → project_alias (alias map, unmapped → keyed HMAC, NEVER basename);
               drop tool_input.command / file_path / prompt entirely (enum only)
   → POST neutral shorthand to the frame server  /admin/event   (proxy-bypassed!)
   ▼
server sanitizes again (2nd gate) + recomputes frame → device shows live status (~4s)
```

Per-session: each `session_id` (Claude or Codex) is one fleet entry; the collector keys
state per session_id; the server aggregates focus (top priority) + fleet (overview).

## Hook formats — researched 2026-05-31, authoritative (verify still current)

### Claude Code — `code.claude.com/docs/en/hooks`
- **Config:** `~/.claude/settings.json` (user) or `.claude/settings.json` (project):
  `"hooks": { "<Event>": [ { "matcher": "<Tool|regex|empty>", "hooks": [ { "type":"command",
  "command":"/path/hook_sink", "timeout": 5 } ] } ] }`
- **Every hook stdin has:** `session_id`, `transcript_path`, `cwd`, `hook_event_name`.
- **Event → Status mapping:**
  | Hook event | payload of interest | → Status |
  |---|---|---|
  | `SessionStart` | `source`, `model` | IDLE |
  | `UserPromptSubmit` | `prompt` ⚠️ never upload | THINKING |
  | `PreToolUse` | `tool_name`, `tool_input` ⚠️ never upload raw | by tool: Read/Grep/Glob→READING; Write/Edit→CODING; Bash→TESTING if cmd has test/build/lint/check/ci/spec else CODING |
  | `Notification` (matcher `permission_prompt`) | `notification_type` | WAITING |
  | `PostToolUseFailure` | `tool_name` | ERROR |
  | `Stop` / `SessionEnd` | `stop_hook_active` | DONE (→ server ages to sleep) |
- **Fire-and-forget:** queue + `exit 0` in <1s; for `Stop`, if `stop_hook_active:true` exit 0 fast.
  `UserPromptSubmit` default timeout is 30s (others 600s) — still keep it instant.

### Codex — `developers.openai.com/codex/hooks`
- **Config:** `~/.codex/config.toml` inline `[hooks]` table, or `hooks.json`. Same event
  structure as Claude (`PreToolUse`, `PermissionRequest`, `PostToolUse`, `SessionStart`,
  `SubagentStart/Stop`, `UserPromptSubmit`, `Stop`). JSON on stdin, JSON on stdout. Only
  `type="command"` handlers run today. (Legacy `--notify` fires `AfterAgent`/`AfterToolUse`
  with a different payload — ignore unless needed.) `codex_adapter.md` already has the table.
- **`PermissionRequest`** → WAITING (Codex's equivalent of Claude's permission Notification).
  Note Claude's `PermissionRequest` does NOT fire in `-p` non-interactive mode.

## Files to create (collector/)

- `collector/hook_sink.py` — the fast queue writer. One script; the provider is passed via
  arg/env (`--provider codex|claude`) since Codex & Claude both just dump stdin JSON to the
  queue. ~15 lines: read stdin, write `~/.agentlamp/queue/<ts>.json`, exit 0.
- `collector/daemon.py` — long-running drainer: watch the queue dir, normalize, sanitize,
  POST to the server. Holds per-session state + a heartbeat loop.
- `collector/normalize.py` — `hook_event_name`+`tool_name` → Status + tool_category (port the
  `provider_normalization.md` table). Reuse `server/agentlamp_server/sanitize.py` for aliasing.
- Hook config snippets the user installs (Claude `settings.json` + Codex `config.toml`) —
  finalize the sketches in the adapter docs against the real formats above. Provide an
  installer command or doc, do NOT silently write to the user's settings.

## GOTCHAS (learned the hard way this session — these WILL bite)

1. **The Clash proxy eats LAN requests.** When the collector POSTs to the local server
   (`127.0.0.1` / `192.168.1.x`), `urllib`/`requests` route through `http_proxy=127.0.0.1:7897`
   and **time out silently**. Bypass per-script: `urllib.request.build_opener(ProxyHandler({}))`,
   `requests(..., proxies={})`, or `curl --noproxy '*'`. **NEVER touch the system proxy** —
   Boss death command. (This cost ~20 min this session; the manual tour scripts all failed
   silently until bypassed.)
2. **Sanitize cwd + tool_input — the product's whole trust claim.** Hooks hand you raw `cwd`,
   `tool_input.command`, `file_path`, `prompt`. NEVER POST those. `cwd` → `project_alias` via
   the alias map (`~/.config/agentlamp/aliases.toml`); unmapped → keyed HMAC (`project-<6hex>`),
   **never the directory basename**; commands/prompts collapse to a `tool_category` enum only.
   Prove it: grep the queue + the POST payloads for `/Users/`, raw commands, prompt text = none.
3. **Fire-and-forget or you slow every tool call.** `hook_sink` does queue-write + `exit 0` in
   <1s, ZERO network. The daemon does all the work.
4. **Multiple sessions = the fleet view.** Key state per `session_id`; the server already does
   focus/fleet aggregation + TTL liveness (STALE 120s / OFFLINE 600s). Send `/admin/heartbeat`
   periodically from the daemon so the collector isn't marked offline.
5. **Account alias.** Hooks don't carry a clean "account". Default to `main` (or read a local
   config); NEVER the email or plan tier. `model` collapses to the provider enum.
6. **Server must be running + device pointed at it.** `cd agentlamp/server &&
   .venv/bin/python -m agentlamp_server`. Re-check the laptop IP (`ipconfig getifaddr en0`,
   was `192.168.1.148`); if DHCP changed it, the device's stored `FRAME_BASE_URL` (NVS) needs
   updating via the portal — or keep the laptop IP stable.

## Test / acceptance

- Install the Claude hooks → run a real Claude Code session → watch the orb:
  prompt→THINKING(blue-violet) → tool→CODING(purple)/READING/TESTING → permission→WAITING(amber)
  → finish→DONE(green)→sleep. Same for Codex.
- Two agents at once → fleet view with both.
- Sanitizer end-to-end: run an agent in a `client-secret/` dir → orb shows `project-<hmac>`,
  never the real name; queue + payloads contain no raw path/command/prompt.

## Key paths / facts

| | |
|---|---|
| Project | `/Users/hulu/huluman/agentlamp` (git `main`, committed through devlog 05) |
| Server | `server/agentlamp_server/` — `app.py` (`/admin/event`), `sanitize.py`, `state.py` |
| Adapter design | `docs/providers/{codex_adapter,claude_adapter,provider_normalization}.md`, `docs/collector/collector_contract.md` |
| Venv / tools | `.venv/bin/{python,pio}` |
| Board | Waveshare ESP32-S3-LCD-1.47B on `/dev/cu.usbmodem1101`; backlight GPIO46; LED is NEO_RGB |
| LAN server | `http://192.168.1.148:8787` (re-check `ipconfig getifaddr en0`) |
| Start server | `cd server && ../.venv/bin/python -m agentlamp_server` |

## Recall the prior (this) session with /ai-history

Run these in the new session to pull back the full detail:
- `/ai-history AgentLamp hardware bring-up backlight GPIO46`
- `/ai-history AgentLamp LED NEO_RGB color order vivid palette`
- `/ai-history AgentLamp Clash proxy LAN 192.168.1.148 noproxy`
- `/ai-history AgentLamp SoftAP provisioning WiFi NVS`
- `/ai-history AgentLamp TASK-005 hook collector`
