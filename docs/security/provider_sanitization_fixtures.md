# Provider Sanitization Fixtures

This file defines sensitive provider inputs that tests must reject or redact.

## Codex Raw Event Examples

### User Prompt

Input contains:

```json
{
  "hook_event_name": "UserPromptSubmit",
  "prompt": "Implement the secret customer auth flow in /Users/hulu/work/client/app/auth.ts",
  "cwd": "/Users/hulu/work/client"
}
```

Expected upload (cwd `/Users/hulu/work/client` is in the local alias map → `project-a`;
if it were **unmapped**, `project_alias` must be a keyed-HMAC label like `project-7f3a`,
**never** the basename `client`):

```json
{
  "provider_event_name": "UserPromptSubmit",
  "payload": {
    "status": "THINKING",
    "task_label": "implementing",
    "project_alias": "project-a"
  },
  "sanitization": {
    "redactions": ["prompt", "cwd"]
  }
}
```

### Bash Tool

Input contains:

```json
{
  "hook_event_name": "PreToolUse",
  "tool_name": "Bash",
  "tool_input": {
    "command": "npm test -- --token sk-secret"
  }
}
```

Expected: event rejected because the command contains a token-like secret. If no secret is present, upload only `tool_category: "test"` and `status: "TESTING"`.

## Claude Raw Event Examples

### Write Tool

Input contains:

```json
{
  "session_id": "abc123",
  "transcript_path": "/Users/hulu/.claude/projects/x/abc123.jsonl",
  "cwd": "/Users/hulu/work/client",
  "hook_event_name": "PreToolUse",
  "tool_name": "Write",
  "tool_input": {
    "file_path": "/Users/hulu/work/client/src/auth.ts",
    "content": "export const token = 'secret';"
  }
}
```

Expected upload:

```json
{
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

### Notification

Input contains a permission or idle notification.

Expected:

- permission/input wait -> `WAITING`
- generic completion notification -> heartbeat/status only
- raw message not stored unless it matches a short allowlisted category

## Global Rejection Cases

Reject the entire event if sanitized output still contains:

- absolute or relative paths: `/Users/`, `/home/`, `C:\`, `D:\`, `./`, `../`
- `sk-`, `Bearer `, `Cookie:`, JWT-like strings, `BEGIN PRIVATE KEY`
- email addresses, account/org/workspace ids, ticket ids
- a real model identifier (anything outside the `model` enum) or a plan tier
  (`Max`, `Team`, `Pro`, `Plus`, `Enterprise`)
- **any** prompt/transcript text (not only >160 chars)
- code body with multiple braces/semicolons/line breaks
- any unknown field not in the schema (recursive default-deny)

## Required Fixtures (mechanism proof — the policy's trust claim)

These assert the alias/hash **mechanism**, not just pattern rejection. See
`sanitization_policy.md` → Required Fixtures.

| Fixture | Input | Must emit | Must NOT emit |
|---------|-------|-----------|---------------|
| `unmapped_cwd` | cwd with no alias-map entry | `project-<hmac6>` | the directory basename |
| `low_entropy_branch` | branch `main` / `feature/login` | keyed-HMAC label | `sha256("main")` (plain) |
| `plan_tier_account` | account `Claude Max` | generic alias (`work`) | `Max` |
| `real_model_id` | `claude-opus-4-…` / `ft:gpt-4o:acme:…` | `model: "claude"` / `"codex"` | the real id |
| `error_with_path` | error message containing a path | `error_label: "unknown"` | the path/message |
| `free_text_task` | arbitrary prompt fragment | a controlled `task_label` or `unknown` | the fragment |
| `unknown_field` | event carrying an extra key | (whole event rejected) | anything |
| `stable_label` | same raw input twice | identical HMAC label both times | a random/changing label |

