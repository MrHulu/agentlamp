# Codex Adapter

## Target

Support local Codex CLI sessions first. Codex cloud task status can be added later as a separate source.

## Supported Sources

| Source | MVP | Notes |
|--------|-----|-------|
| Codex hooks | Yes | Primary source for local session lifecycle |
| Codex `notify` | Optional | Good for turn-complete side-channel, not enough for full state |
| `codex exec --json` | Optional | Only for non-interactive tasks launched through our wrapper |
| `codex cloud list --json` | Later | Cloud tasks; useful for remote thread status |
| `history.jsonl` | No | Local transcript store; do not parse as primary API |
| OpenTelemetry | Later | Useful for audits; must keep prompt redaction enabled |

## Hook Events

Use Codex lifecycle hooks configured in `~/.codex/config.toml`, project config, plugin config, or managed config.

> **Hook names are not a stable API** (verified against Codex hook docs as of 2026-05-30;
> treat as unstable and version this mapping). An unrecognized event MUST NOT hard-fail:
> map to the closest status or `UNKNOWN`, preserve the event **name** for diagnostics, never
> silently no-op. The `hook_sink` is **fire-and-forget** — it appends the raw event to a
> local queue and returns in <1 s; the background daemon does all sanitize/sign/upload
> (see `../collector/collector_contract.md` → Hook Ingestion). It performs zero network I/O.

Required event handling:

| Codex Event | Normalized Event | Status |
|-------------|------------------|--------|
| `SessionStart` | `session.upsert` | `IDLE` |
| `UserPromptSubmit` | `session.status` | `THINKING` |
| `PreToolUse` | `session.status` | `READING`, `CODING`, or `TESTING` by tool category |
| `PermissionRequest` | `alert.raise` | `WAITING` |
| `PostToolUse` | `session.status` | Preserve current active status or map failure to `ERROR` |
| `PreCompact` / `PostCompact` | `session.status` | `THINKING` with `status_detail: "compacting"` |
| `SubagentStart` | `session.upsert` | `THINKING` |
| `SubagentStop` | `session.status` | `DONE` for subagent |
| `Stop` | `session.close` | `DONE` |

## Sanitized Hook Payload

Codex hook input may include `cwd`, tool input, tool response, permission details, and a turn/session id. The collector must reduce it to:

```json
{
  "provider": "codex",
  "adapter": "codex_hooks",
  "provider_event_name": "PreToolUse",
  "provider_session_id": "hmac:7f3a9c…",
  "turn_id": "hmac:turn-…",
  "payload": {
    "status": "TESTING",
    "tool_category": "test",
    "project_alias": "project-a"
  }
}
```

Do not upload:

- raw `cwd`
- raw `tool_input.command`
- raw `tool_input.file_path`
- raw `tool_response`
- prompt text
- local `history.jsonl` path

## Codex Config Sketch

This is a contract sketch, not a ready-to-run command:

```toml
[[hooks.UserPromptSubmit]]
[[hooks.UserPromptSubmit.hooks]]
type = "command"
command = "python3 /path/to/agentlamp/src/collector/adapters/codex/hook_sink.py"
timeout = 5
statusMessage = "Updating AgentLamp"

[[hooks.PreToolUse]]
matcher = ".*"
[[hooks.PreToolUse.hooks]]
type = "command"
command = "python3 /path/to/agentlamp/src/collector/adapters/codex/hook_sink.py"
timeout = 5

[[hooks.PermissionRequest]]
matcher = ".*"
[[hooks.PermissionRequest.hooks]]
type = "command"
command = "python3 /path/to/agentlamp/src/collector/adapters/codex/hook_sink.py"
timeout = 5

[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = "python3 /path/to/agentlamp/src/collector/adapters/codex/hook_sink.py"
timeout = 5
```

## Cloud Task Source

If Codex cloud support is added, use `codex cloud list --json` as read-only discovery. Normalize only:

- task id hash
- title label after sanitization
- status
- updated timestamp
- environment alias
- URL only if Boss permits dashboard linking

## Quota

No exact quota source is assumed in v1.

Allowed:

- manual quota entry
- observed/estimated reset window with `confidence: low`
- explicit provider state if a stable source is later documented

Forbidden:

- scraping account pages with credentials through cloud
- uploading browser session data
- claiming `high` confidence from local transcript patterns

## Acceptance

- Permission request produces `WAITING` alert within one collector push.
- Tool events update status without uploading command/file content.
- Stop event closes or lowers priority of the session.
- Sanitizer tests reject `cwd`, local paths, commands, prompts, and provider tokens.

