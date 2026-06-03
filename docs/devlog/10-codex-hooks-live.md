# 10 — R1/TASK-009: Codex hooks live on the lamp (verified)

> 2026-05-31. Closes R1 of `08-vnext-requirements.md`. A **real interactive Codex
> session now drives the orb** (verified live), and the hook trust is persisted in
> the real `~/.codex` so it keeps working in Boss's normal workflow. One acceptance
> sub-item (tool-failure → ERROR) is a **Codex platform limitation**, documented below.

## What shipped

- `~/.codex/config.toml` — AgentLamp command hooks for 7 Codex lifecycle events
  (`SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, PermissionRequest,
  SubagentStop, Stop`) appended via `collector.install_hooks --write-codex`
  (additive, backed up to `config.toml.bak-*`). Each runs
  `…/.venv/bin/python …/src/collector/hook_sink.py --provider codex`, timeout 5s.
- **Hook trust persisted** in the real `~/.codex` (chose "Trust all and continue"
  at the boot dialog) → Boss's future interactive `codex` sessions drive the orb
  with no further action.
- No repo code change (collector behaviour unchanged); 48 collector + 113 server
  tests still green.

## The VERIFIED recipe (how Codex hooks actually fire — 0.135.0)

Codex gates hook execution behind **two** independent gates, both required:

1. **Directory trust** — first launch in a dir shows *"Do you trust the contents of
   this directory? … Trusting allows project-local config, hooks, and exec policies
   to load."* (For `~/.codex` with `[projects."/Users/hulu/huluman/agentlamp"]
   trusted` this is already satisfied for the repo dir.)
2. **Per-hook review/trust** — new hooks show *"N hooks are new or changed →
   1. Review hooks  2. **Trust all and continue**  3. Continue without trusting"*.
   Trust is recorded against the hook's hash and **persists**. A hook also has a
   separate **enabled** toggle (`[x]`/`[ ]`, space/enter in the `/hooks` browser);
   it must be **trusted AND enabled** to run.

Then the arc fires on the **first prompt** of a session.

## Hard-won findings (do NOT relearn)

1. **`codex exec` (non-interactive) does NOT fire lifecycle hooks.** Tools run, the
   model responds, but no hook command is invoked — verified with a probe wrapper
   under a throwaway `CODEX_HOME` (zero sentinel). Hooks are an interactive-TUI
   feature. The launchd daemon only ever sees Codex events from **interactive**
   `codex`, never `codex exec`.
2. **`--dangerously-bypass-hook-trust` alone was insufficient here.** Codex prints
   *"Enabled hooks may run without review for this invocation"* but un-reviewed
   hooks still did not fire. The reliable path is the boot **"Trust all and
   continue"** (or `/hooks` → `t`), which persists trust. (Matches open upstream
   #21639 flakiness.)
3. **`SessionStart` fires at *thread* scope = the first prompt, not at TUI launch.**
   A bare boot-and-quit fires nothing. Submit a prompt to see the arc.
4. **Stdin payload is snake_case**, exactly as `normalize.py` expects:
   `session_id, turn_id, cwd, hook_event_name, model, permission_mode, tool_name,
   tool_use_id, tool_input`. Codex's shell tool name is **`Bash`** (capitalised;
   `_SHELL_TOOLS` lowercases so it matches). `tool_input.command` is a string.
5. **Codex PostToolUse carries NO exit status.** For a failed command the payload is
   `{tool_name:"Bash", tool_input:{command:…}, tool_response:<str>, tool_use_id:…}`
   — **no `exit_code`/`success`/`error`**. `tool_response` is just the output:
   a silent non-zero exit (`sys.exit(3)`) → `tool_response:""` (identical to a
   successful no-output command); a stderr-producing failure → the stderr text as a
   string. See the ERROR limitation below.

## Live verification (evidence)

- **Raw real-codex capture** (isolated queue) normalised correctly:
  `SessionStart→IDLE · UserPromptSubmit→THINKING · PreToolUse(Bash)→READING/CODING ·
  PostToolUse→READING/CODING · Stop→DONE`, `provider=codex`, `project=agentlamp-cx-verify`.
- **Live orb** (default queue → launchd daemon → server): captured frame
  `primary={provider:"Codex", account:"main", status:"CODING",
  project:"agentlamp-cx-verify", task:"implementing"}`, scene `fleet`, with Claude
  rows (`channel-bridge x4`, `ai-center x7`) shown **simultaneously** — i.e. a real
  Codex session AND a Claude session both on the lamp. ✓ R1 acceptance.

## Known limitation — Codex tool-failure → ERROR (platform gap, NOT an AgentLamp bug)

Real Codex PostToolUse exposes **no tool exit status** (finding #5). Therefore:

- A **silent non-zero shell exit** is invisible to the hook → stays CODING (correct;
  there is no signal to act on).
- **Structured** tool failures (apply_patch / MCP returning a dict with
  `error`/`success:false`/`is_error`) are still caught by `normalize._tool_failed`.
- We deliberately **do NOT scan `tool_response` text for "error"** to synthesise an
  ERROR: on an always-on ambient orb that would cause **false amber/red alerts**
  (e.g. `grep error`, a test that prints "FAILED" mid-work) — the exact false-alarm
  class Boss rejected for `idle_prompt` (kickoff gotcha #4). Honest behaviour beats a
  fragile heuristic. Claude's ERROR path (`PostToolUseFailure` + error fields) is
  unaffected and works.

If first-class Codex error surfacing is wanted later, it needs an upstream signal
(an exit_code field) or an opt-in, separately-designed heuristic — a feature, not a
hotfix.

## Operator runbook (one-time, already done on this machine)

```
# hooks are installed in ~/.codex/config.toml; to (re)trust on any machine:
codex                       # launch interactive TUI in any trusted dir
# at "N hooks are new or changed" → choose "Trust all and continue"
#   (or run /hooks → select each → press t to trust)
# then submit any prompt; the orb reflects the session.
# inspect/disable later:  /hooks
```

Re-generate / re-merge the hook block (idempotent, backs up):

```
cd <repo>/src && ../.venv/bin/python -m collector.install_hooks --write-codex ~/.codex/config.toml
```
