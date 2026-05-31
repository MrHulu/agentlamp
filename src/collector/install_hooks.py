#!/usr/bin/env python3
"""Generate the provider hook config that wires AgentLamp's ``hook_sink``.

Prints ready-to-paste config; it NEVER silently writes the user's settings
(collector_contract + adapter docs). An explicit ``--write-claude`` / ``--write-codex``
performs an ADDITIVE merge with a timestamped ``.bak`` backup.

  python3 -m collector.install_hooks --print claude
  python3 -m collector.install_hooks --print codex
  python3 -m collector.install_hooks --print all          # default
  python3 -m collector.install_hooks --write-claude ~/.claude/settings.json
  python3 -m collector.install_hooks --write-codex  ~/.codex/config.toml

Event set is the VERIFIED-current one (2026): adds PostToolUseFailure (the
separate Claude failure event) and PermissionRequest (the distinct approval
event) beyond the original kickoff sketch.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import time

_THIS = pathlib.Path(__file__).resolve()
HOOK_SINK = _THIS.parent / "hook_sink.py"
REPO_ROOT = _THIS.parents[2]
_VENV_PY = REPO_ROOT / ".venv" / "bin" / "python"

# Claude: omit "matcher" to fire for all tools/notifications.
CLAUDE_EVENTS = (
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "PostToolUseFailure", "PermissionRequest", "Notification", "Stop", "SessionEnd",
)
# Codex: same lifecycle minus Claude-only Notification/SessionEnd; add compaction.
CODEX_EVENTS = (
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "PermissionRequest", "SubagentStop", "Stop",
)


def _python() -> str:
    return str(_VENV_PY) if _VENV_PY.exists() else "python3"


def _command(provider: str) -> str:
    return f"{_python()} {HOOK_SINK} --provider {provider}"


def claude_hooks_block() -> dict:
    cmd = _command("claude")
    hooks: dict = {}
    for ev in CLAUDE_EVENTS:
        hooks[ev] = [{"hooks": [{"type": "command", "command": cmd, "timeout": 5}]}]
    return {"hooks": hooks}


def codex_hooks_toml() -> str:
    cmd = _command("codex")
    lines = ["# AgentLamp collector hooks — append to ~/.codex/config.toml",
             "# (user-level config; repo-local .codex hooks may not fire interactively, GH #17532)"]
    for ev in CODEX_EVENTS:
        lines.append("")
        lines.append(f"[[hooks.{ev}]]")
        lines.append(f"[[hooks.{ev}.hooks]]")
        lines.append('type = "command"')
        lines.append(f'command = "{cmd}"')
        lines.append("timeout = 5")
    return "\n".join(lines) + "\n"


def _print(target: str) -> None:
    if target in ("claude", "all"):
        print("# ── Claude Code ──  merge into ~/.claude/settings.json (user) "
              "or .claude/settings.json (project)\n")
        print(json.dumps(claude_hooks_block(), indent=2))
        print()
    if target in ("codex", "all"):
        print(codex_hooks_toml())
    print("# Then start the daemon:  cd %s/src && ../.venv/bin/python -m collector.daemon"
          % REPO_ROOT, file=sys.stderr)


# --------------------------------------------------------------------------- #
# Explicit additive merge (opt-in; backs up first; never clobbers existing).
# --------------------------------------------------------------------------- #
def _backup(path: pathlib.Path) -> pathlib.Path | None:
    if not path.exists():
        return None
    bak = path.with_suffix(path.suffix + f".bak-{int(time.time())}")
    shutil.copy2(path, bak)
    return bak


def write_claude(path_str: str) -> int:
    path = pathlib.Path(os.path.expanduser(path_str))
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text() or "{}")
        except ValueError:
            print(f"refusing to merge: {path} is not valid JSON", file=sys.stderr)
            return 1
    cmd = _command("claude")
    hooks = existing.setdefault("hooks", {})
    added = 0
    for ev in CLAUDE_EVENTS:
        arr = hooks.setdefault(ev, [])
        # Skip if our exact command is already present (idempotent).
        if any(
            any(h.get("command") == cmd for h in entry.get("hooks", []))
            for entry in arr if isinstance(entry, dict)
        ):
            continue
        arr.append({"hooks": [{"type": "command", "command": cmd, "timeout": 5}]})
        added += 1
    bak = _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2) + "\n")
    print(f"merged {added} Claude hook event(s) into {path}"
          + (f" (backup: {bak.name})" if bak else ""))
    return 0


def write_codex(path_str: str) -> int:
    path = pathlib.Path(os.path.expanduser(path_str))
    cmd = _command("codex")
    block = codex_hooks_toml()
    current = path.read_text() if path.exists() else ""
    if cmd in current:
        print(f"codex hooks already present in {path} (idempotent, no change)")
        return 0
    bak = _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sep = "" if current.endswith("\n") or not current else "\n"
    path.write_text(current + sep + "\n" + block)
    print(f"appended Codex hooks to {path}" + (f" (backup: {bak.name})" if bak else ""))
    print("note: Codex requires persisted hook trust (or --dangerously-bypass-hook-trust).",
          file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--write-claude" in argv:
        i = argv.index("--write-claude")
        return write_claude(argv[i + 1] if i + 1 < len(argv) else "~/.claude/settings.json")
    if "--write-codex" in argv:
        i = argv.index("--write-codex")
        return write_codex(argv[i + 1] if i + 1 < len(argv) else "~/.codex/config.toml")
    target = "all"
    if "--print" in argv:
        i = argv.index("--print")
        if i + 1 < len(argv):
            target = argv[i + 1]
    _print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
