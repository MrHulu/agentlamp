# Claude Adapter

Contract: `docs/providers/claude_adapter.md`. Implementation is the shared sink +
daemon in the parent dir — there is **one** `hook_sink.py`, selected per provider:

```
python3 <repo>/src/collector/hook_sink.py --provider claude
```

Wire it with `python3 -m collector.install_hooks --print claude`. Primary source:
Claude Code lifecycle hooks (verified-current event set incl. `PostToolUseFailure`
+ `PermissionRequest`). Optional later source: redacted OpenTelemetry with
prompt/tool detail gates disabled.

Never upload:

- prompt text
- transcript path
- `cwd`
- raw command
- raw file path
- tool input/output
- model output
- credentials
