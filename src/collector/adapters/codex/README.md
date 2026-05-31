# Codex Adapter

Contract: `docs/providers/codex_adapter.md`. Implementation is the shared sink +
daemon in the parent dir — one `hook_sink.py`, selected per provider:

```
python3 <repo>/src/collector/hook_sink.py --provider codex
```

Wire it with `python3 -m collector.install_hooks --print codex` (use user-level
`~/.codex/config.toml`; Codex needs persisted hook trust). Primary source: Codex
lifecycle hooks.

Never upload:

- prompt text
- raw command
- raw tool input/output
- raw file path
- `cwd`
- local history path
- credentials

