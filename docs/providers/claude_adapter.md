# Claude Adapter

## Target

Support Claude Code local sessions through hooks. Optional redacted OpenTelemetry can improve quota/usage and active-time insight after hooks are stable.

## Supported Sources

| Source | MVP | Notes |
|--------|-----|-------|
| Claude Code hooks | Yes | Primary session lifecycle source |
| Notification hook | Yes | Waiting-for-permission/input alert source |
| OTel metrics/events | Later | Useful for active time, tokens, costs; keep content gates disabled |
| Transcript JSONL | No | Contains prompts/tool content/paths; not a primary API |
| `/usage` CLI output | Later/manual | Can inform quota estimate only after stable parser and sanitizer |

## Hook Events

> **Hook names are not a stable API** (verified against Claude Code hook docs as of
> 2026-05-30; treat as unstable and version this mapping). An unrecognized event MUST NOT
> hard-fail: map to the closest status or `UNKNOWN`, preserve the event **name** for
> diagnostics, never silently no-op. The `hook_sink` is **fire-and-forget** — append the raw
> event to a local queue and return in <1 s; the background daemon does all
> sanitize/sign/upload (see `../collector/collector_contract.md` → Hook Ingestion). Zero
> network I/O in the hook.

Required event handling:

| Claude Event | Normalized Event | Status |
|--------------|------------------|--------|
| `SessionStart` | `session.upsert` | `IDLE` |
| `UserPromptSubmit` | `session.status` | `THINKING` |
| `PreToolUse` | `session.status` | `READING`, `CODING`, or `TESTING` by tool category |
| `PermissionRequest` | `alert.raise` | `WAITING` |
| `Notification` | `alert.raise` or `collector.heartbeat` | `WAITING` if permission/input wait, otherwise heartbeat |
| `PostToolUse` | `session.status` | Preserve active status |
| `PostToolUseFailure` | `session.status` | `ERROR` |
| `SubagentStart` / `TaskCreated` | `session.upsert` | `THINKING` |
| `SubagentStop` / `TaskCompleted` | `session.status` | `DONE` for subagent |
| `PreCompact` / `PostCompact` | `session.status` | `THINKING` with compacting detail |
| `SessionEnd` | `session.close` | `DONE` or `IDLE` |
| `Stop` | `session.close` | `DONE` |

## Sanitized Hook Payload

Claude hook input commonly includes:

- `session_id`
- `transcript_path`
- `cwd`
- `hook_event_name`
- event-specific fields such as tool name, tool input, notification message, or stop reason

The collector must upload only:

```json
{
  "provider": "claude",
  "adapter": "claude_hooks",
  "provider_event_name": "PreToolUse",
  "provider_session_id": "hmac:7f3a9c…",
  "payload": {
    "status": "CODING",
    "tool_category": "edit",
    "project_alias": "project-a"
  },
  "sanitization": {
    "redactions": ["transcript_path", "cwd", "tool_input.file_path", "tool_input.content"]
  }
}
```

Do not upload:

- `transcript_path`
- `cwd`
- prompt text
- `tool_input.file_path`
- `tool_input.content`
- `tool_result` (current field name) / `tool_response` (older builds — guard for both)
- raw Bash command
- transcript lines
- model output

## Claude Settings Sketch

The collector is a single fire-and-forget sink — `src/collector/hook_sink.py --provider claude`
— wired to each lifecycle event. Generate the current, ready-to-paste block (it
resolves absolute paths + the repo venv) instead of copying a static sketch that can drift:

```bash
python3 -m collector.install_hooks --print claude     # print only (never writes)
python3 -m collector.install_hooks --write-claude ~/.claude/settings.json   # opt-in additive merge + .bak
```

Verified-current event set (2026): `SessionStart`, `UserPromptSubmit`, `PreToolUse`,
`PostToolUse`, **`PostToolUseFailure`** (the *separate* failure event — `PostToolUse`
itself carries no error and its output field is `tool_result`, not `tool_response`),
**`PermissionRequest`** (a distinct approval event), `Notification` (uses
`notification_type` = `permission_prompt`/`idle_prompt` to flag a wait), `Stop`,
`SessionEnd`. `matcher` is omitted so every tool/notification fires the sink.

Shape (per event):

```json
{ "hooks": { "<Event>": [ { "hooks": [
  { "type": "command", "command": "<python> <repo>/src/collector/hook_sink.py --provider claude", "timeout": 5 }
] } ] } }
```

## Optional OTel Source

Claude Code OTel is useful for:

- active time
- session counts
- tool decision events
- API errors
- token/cost counters

Default content gates must stay disabled:

- do not enable prompt logging
- do not enable tool input details
- do not enable tool content logging
- do not enable raw API body logging

OTel-derived data enters the same sanitizer and normalized event model as hooks.

## Quota

No exact Claude quota source is assumed in v1.

Allowed:

- manual 5h/weekly quota entry
- low-confidence observed reset estimates
- redacted usage/active-time metrics as supporting context

Forbidden:

- uploading `/usage` raw output if it contains account identifiers or detailed history
- parsing transcript for usage
- storing Claude account tokens or browser state

## Acceptance

- Prompt submit moves session to `THINKING` without uploading prompt text.
- Permission/input notification produces `WAITING`.
- Tool events classify read/edit/test locally and upload only category.
- OTel, if enabled, uses redacted defaults and passes sanitizer tests.

