# Provider Normalization

## Goal

Codex and Claude emit different local events. The collector converts them into one minimal,
sanitized event model before serving it locally (local mode) or uploading it (relay mode).

## Normalized Session

```json
{
  "provider": "codex",
  "provider_session_id": "hmac:7f3a9c…",
  "account_alias": "main",
  "project_alias": "project-a",
  "status": "CODING",
  "task_label": "implementing",
  "model": "codex",
  "started_at": 1716900000,
  "updated_at": 1716900480,
  "needs_attention": false,
  "confidence": "medium"
}
```

> `provider_session_id` is a **keyed HMAC** label (local pepper), not plain SHA256 of a
> low-entropy id. `account_alias`/`project_alias` come from the local alias map or an HMAC
> label — never a path basename or plan tier. `model` is the provider enum
> (`codex`\|`claude`\|`manual`\|`unknown`), never a real model id. `task_label` is from the
> controlled vocabulary. See `../security/sanitization_policy.md`.

## Normalized Quota Window

```json
{
  "provider": "claude",
  "account_alias": "work",
  "window_type": "5h",
  "used_ratio": 0.58,
  "remaining_ratio": 0.42,
  "reset_at": null,
  "confidence": "low",
  "is_estimated": true,
  "source_type": "manual"
}
```

## Status Mapping

| Normalized Status | Meaning | Display Priority |
|-------------------|---------|------------------|
| `IDLE` | Known account, no active task | Low |
| `THINKING` | User submitted prompt or model is processing | Medium |
| `READING` | Read/search/list operation | Medium |
| `CODING` | Write/edit/patch/shell operation | High |
| `TESTING` | Test/build/lint command category | High |
| `WAITING` | Waiting for user permission or input | Interrupt |
| `DONE` | Agent completed current turn/task | Low |
| `ERROR` | Tool/API/session error | Interrupt |
| `OFFLINE` | Collector/provider not seen recently | Interrupt if active |
| `STALE` | No fresh event past TTL | Warning |

## Provider Event Envelope

All provider adapters emit local events with this shape before collector signing:

```json
{
  "schema_version": 1,
  "provider": "claude",
  "adapter": "claude_hooks",
  "adapter_version": "0.1.0",
  "event_type": "session.status",
  "provider_event_name": "PreToolUse",
  "provider_session_id": "hmac:7f3a9c…",
  "event_time": 1716900398,
  "source_seq": 12,
  "payload": {
    "status": "CODING",
    "tool_category": "edit",
    "project_alias": "project-a"
  },
  "sanitization": {
    "policy_version": 1,
    "redactions": ["cwd", "transcript_path", "tool_input.file_path"],
    "confidence": "medium"
  }
}
```

## Tool Category Mapping

| Provider Tool Signal | Category | Status |
|----------------------|----------|--------|
| Read/search/list/glob/grep | `read` | `READING` |
| Write/edit/apply_patch | `edit` | `CODING` |
| Bash command containing test/build/lint/check keywords | `test` | `TESTING` |
| Other Bash/shell command | `shell` | `CODING` |
| MCP call | `mcp` | `CODING` unless allowlisted as read |
| Permission request | `approval` | `WAITING` |
| Hook failure/API error | `error` | `ERROR` |

Raw command strings are never uploaded. The collector may classify a command locally, then discard it.

The keyword set for the `test` category is **locally extensible** via collector config
(default: `test`, `spec`, `check`, `lint`, `build`, `ci`, `verify`, `validate`). The
classifier runs on the raw command **locally only** and emits only the category enum;
classification accuracy is best-effort and never blocks an event.

## Account Identity

The relay sees only **generic** account aliases — never the plan tier, which is
billing-identifying. Use neutral labels from the local alias map, or an HMAC label:

- `main`, `work`, `personal`
- `account-7f3a` (HMAC label when unmapped)

Forbidden as an alias: plan/tier names (`Max`, `Team`, `Pro`, `Plus`, `Enterprise`),
email addresses, account ids, organization ids, workspace ids. Adapter code must not
upload any of these even if the user "maps" an alias to them — the mapping value itself
must be a neutral label. See `../security/sanitization_policy.md` (Controlled Vocabularies).

## Quota Confidence

| Confidence | Source |
|------------|--------|
| `high` | Official explicit provider state |
| `medium` | Provider telemetry or stable local CLI/dashboard summary |
| `low` | Manual entry, local estimate, or weak inference |
| `unknown` | Not available |

MVP quota for both Codex and Claude starts as `manual` or `unknown`; do not claim exact quota until a stable source is proven.

