# 07 — TASK-005: real Codex/Claude hooks → live orb (collector built)

> Implements the kickoff in `06-task005-kickoff.md`. The orb now reflects REAL
> agent activity automatically — no more manual `curl POST /admin/event`.

## What shipped

`src/collector/` (stdlib-only except the server it reuses):

| File | Role |
|------|------|
| `hook_sink.py` | The only thing a hook runs. Reads stdin, atomically enqueues raw hook JSON to `~/.agentlamp/queue/`, exits 0 in <1 s, zero network, never fails the host agent. `--provider claude\|codex`. |
| `daemon.py` | Long-running drainer: queue → `normalize` → POST `/admin/event` (loopback, proxy-bypassed) → delete; 422 → dead-letter (reason+hash only); transport fail → bounded retry; idle heartbeat. `--once` for one drain. |
| `normalize.py` | `hook_event_name` + `tool_name` → status enum + `tool_category`; `cwd` → neutral alias via the **reused** server sanitizer. Raw command/prompt/path read locally then discarded. |
| `netpost.py` | Proxy-bypassing POST (`build_opener(ProxyHandler({}))`, reused opener). Never reads/modifies the system proxy. |
| `config.py` | `$HOME` paths, keyed pepper (persisted 0600), alias map, server base; bridges to `agentlamp_server.sanitize`. |
| `install_hooks.py` | Prints / opt-in-merges provider hook config. Never writes silently. |
| `tests/test_collector.py` | 33 tests: fixtures, killer privacy test, proxy bypass, daemon drain/quarantine, real-server integration. |

## Hook formats — re-verified 2026-05-31 (corrections to the kickoff)

Both providers' hooks were re-fetched from official docs. Load-bearing corrections:

**Claude Code** (`code.claude.com/docs/en/hooks`):
- PostToolUse output field is **`tool_result`** (object `{type,text}`), **not** `tool_response`
  (older builds/blogs still say `tool_response`; guard for both — we never emit it anyway).
- Tool **failures are a separate `PostToolUseFailure`** event (`tool_name, tool_input, error`),
  not a field on `PostToolUse`. → wired to `ERROR`.
- **`PermissionRequest` is now a distinct event**; `Notification` carries a
  **`notification_type`** (`permission_prompt`/`idle_prompt`/…), not just `message`.
  → both paths wired to `WAITING`.
- Guaranteed common stdin: `session_id`, `cwd`, `hook_event_name` (`transcript_path` +
  `permission_mode` are CLI/tool-event additions).

**Codex CLI** (`developers.openai.com/codex/hooks`):
- Double-table `[[hooks.<Event>]]` / `[[hooks.<Event>.hooks]]` confirmed; `matcher` is a
  regex on `tool_name`. `PermissionRequest` is the real approval event. snake_case stdin
  (`session_id, turn_id, cwd, tool_name, tool_input, model, permission_mode`).
- **Gotchas:** hooks need *persisted trust* (or `--dangerously-bypass-hook-trust`);
  repo-local `.codex/config.toml` hooks may not fire interactively (openai/codex#17532) →
  use **user-level** `~/.codex/config.toml`. Legacy `--notify` fires `agent-turn-complete`
  via `argv[1]` (kebab-case), **not** `AfterAgent`/`AfterToolUse`.

## research-before-build

7 OSS candidates scanned (claude-lamp 252★, cursor_agent_status_light 176★, busylight
329★, echook 66★, lampia, monitor-esp32, Signal-Lamp). All rejected for our combination —
none has the **default-deny cwd→HMAC sanitizer**, a **LAN HTTP frame protocol to a
self-built ESP32**, a **7-state enum from `tool_name`**, AND **dual Claude+Codex**
normalization. Decision: **build**, reusing the proven *patterns* (fire-and-forget
file-queue + polling daemon + debounce from claude-lamp; dual-CLI event table from echook).

## Proxy bypass (kickoff GOTCHA #1, confirmed)

`urllib.request.build_opener(ProxyHandler({}))` with an **empty dict** installs no scheme
handlers and never reads `http_proxy`/`https_proxy`/`all_proxy`/`no_proxy` — an
unconditional per-request bypass that touches nothing in the system proxy. We call
`_OPENER.open()` (never `urlopen()`, which would route through Clash) and target **127.0.0.1**
(daemon and server are the same machine). `requests proxies={}` does NOT bypass — stdlib only.

## Acceptance (real Claude session drove the orb)

`claude -p` (read → write → `echo test ok`) in a `client-secret/` dir, hooks via
`--settings` (no global config pollution), daemon draining the live server:

```
sleep/IDLE → THINKING(purple) → READING(cyan) → CODING(purple) → TESTING(green) → DONE → sleep
  t=0          t=2.6            t=5.8          t=9.7           t=12.3          t=14.9
```

Privacy held end-to-end: `project = project-f1010d` (HMAC) — the dir name `client-secret`
never left the machine; queue empty + dead-letter clean afterward. `WAITING`
(PermissionRequest / Notification permission_prompt) is auto-approved in `-p` so it is
proven by the unit + real-server integration tests, not the live `-p` run.

## Go live

```bash
# 1. server (already runs through devlog 05)
cd server && ../.venv/bin/python -m agentlamp_server
# 2. daemon (any cwd)
.venv/bin/python src/collector/daemon.py
# 3. install hooks — print first; merge is opt-in (writes ~/.claude/settings.json globally)
python3 -m collector.install_hooks --print all
python3 -m collector.install_hooks --write-claude ~/.claude/settings.json   # affects ALL your Claude sessions
python3 -m collector.install_hooks --write-codex  ~/.codex/config.toml       # + codex trust
```

## Hardening (4-lens adversarial review → fixes)

A 4-lens review (privacy / proxy / fire-and-forget / correctness) was run before sign-off.
The proxy lens **PASSED** (the bypass could not be broken) and the wire trust claim held
(0/19 secret needles leaked into the POST body). Real findings, all fixed + tested (45 tests):

- **HIGH** poison record (non-string `tool_name`) crashed `normalize_record` → stalled the
  whole queue. Fixed two layers: `normalize` coerces `tool_name` to `str|None`; `daemon`
  wraps `normalize_record` in try/except → quarantine. One bad record can never stall the loop.
- **HIGH** Codex tool failures never rendered ERROR (Codex has no `PostToolUseFailure`, so
  `PostToolUse` is its only failure signal). Added `_tool_failed()` (exit_code≠0 / success:false
  / error) → ERROR. Verified live: a Codex `false` shell → `scene=alert status=ERROR accent=red`.
- **MEDIUM** `account` was emitted verbatim (an `AGENTLAMP_ACCOUNT` email/plan-tier would leak
  on the wire + 422-loop the lamp dark). Now routed through `_safe_account()` — `main`/`work`
  pass through, `hulu@example.com`/`Pro`/paths collapse to `account-<hmac4>`.
- **MEDIUM** queue/dead-letter unbounded + orphaned `*.tmp` never reaped. Added `daemon.reap()`:
  `*.tmp` TTL, aged-`*.json` TTL, oldest-drop over `MAX_QUEUE_FILES`, dead-letter cap.
- **MEDIUM** `hook_sink` could hang on a never-EOF stdin. Added a 0.8 s SIGALRM read deadline —
  the `<1 s` guarantee now holds regardless of host behavior.
- **MEDIUM** `SubagentStop`→THINKING could resurrect a DONE session → now `DONE` (doc-faithful).
- Transport failure (server down/restarting) now **retries indefinitely** (records stay queued,
  bounded by the reaper) instead of dead-lettering after a fixed cap — a server restart loses
  nothing. **LOW:** dirs created `0700`; `status_detail` (`compacting`/`subagent`) now forwarded
  by `_to_envelope`; TaskCreated/Completed mapped; netpost docstring corrected.

## Open items

- Live Codex run not yet done (same code path; unit-tested + the failure→ERROR path verified
  live via a synthetic Codex hook). Needs a trusted user-level `~/.codex/config.toml`.
- `WAITING` not exercised by a live `-p` session (auto-approve). An interactive session or a
  tool needing approval shows it; proven by unit + real-server integration tests.
- Global install (`install_hooks --write-claude`) is an opt-in step — it changes
  `~/.claude/settings.json` for ALL the user's Claude sessions, so it's left for the operator.
