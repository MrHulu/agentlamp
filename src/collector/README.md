# Collector (local mode)

Turns real Codex / Claude Code lifecycle hooks into the live orb status, behind a
default-deny privacy boundary. Contract: [`docs/collector/collector_contract.md`](../../docs/collector/collector_contract.md),
build notes: [`docs/devlog/06-task005-kickoff.md`](../../docs/devlog/06-task005-kickoff.md).

```
provider hook fires
  -> hook_sink.py   fire-and-forget: append raw hook JSON to ~/.agentlamp/queue/, exit 0 (<1s, no network)
  -> daemon.py      drain queue -> normalize.py -> SANITIZE (reuse server sanitize.py) -> POST /admin/event
  -> frame server   sanitizes again (2nd gate) -> device shows live status (~4s)
```

| File | Role |
|------|------|
| `hook_sink.py` | The only thing a hook runs. Self-contained, stdlib only. `--provider claude\|codex`. |
| `daemon.py` | Long-running drainer + heartbeat. Proxy-bypassed POST over loopback. `--once` for a single drain. |
| `normalize.py` | `hook_event_name` + `tool_name` → status enum + `tool_category`; `cwd` → neutral alias. Raw commands/prompts read locally then discarded. |
| `netpost.py` | Proxy-bypassing local POST (`ProxyHandler({})`; never reads/modifies system proxy). |
| `config.py` | Local paths ($HOME), keyed pepper, alias map, server base URL. Bridges to `agentlamp_server.sanitize`. |
| `install_hooks.py` | Prints / opt-in merges the provider hook config. Never writes silently. |

## Run

```bash
# 1. start the frame server (separate terminal)
cd server && ../.venv/bin/python -m agentlamp_server

# 2. start the collector daemon
.venv/bin/python src/collector/daemon.py          # runnable from anywhere (self-bootstraps src/ onto sys.path)
#   or:  cd src && ../.venv/bin/python -m collector.daemon

# 3. install the hooks (print first; merge is opt-in)
python3 -m collector.install_hooks --print all
```

## Privacy invariant

The daemon's POST body is enum/alias only: `cwd` → `project-<hmac6>` (never a
basename), session id → `hmac:<…>`, commands/prompts → a `tool_category` enum,
model → the provider enum. Tests: [`tests/test_collector.py`](tests/test_collector.py)
(`test_no_raw_anything_survives` is the load-bearing one). The queue holds the raw
hook JSON only transiently (mode 0600, under `$HOME`, deleted on drain).
