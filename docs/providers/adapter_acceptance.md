# Adapter Acceptance

## Required Before Codex/Claude Adapters Ship

Tests must cover all fixtures in `tests/unit/fixtures/provider_events/`.

## Fixtures To Add

| Fixture | Expected |
|---------|----------|
| Codex `UserPromptSubmit` with prompt | prompt removed; status `THINKING` |
| Codex `PreToolUse` Bash `npm test` | raw command removed; category `test`; status `TESTING` |
| Codex `PermissionRequest` | status `WAITING`; no permission raw input |
| Claude `UserPromptSubmit` with prompt | prompt removed; status `THINKING` |
| Claude `PreToolUse` Write with file path/content | path/content removed; category `edit`; status `CODING` |
| Claude `Notification` permission wait | status `WAITING`; message classified only |
| Claude hook with `transcript_path` and `cwd` | both redacted; never uploaded |
| Token/cookie/API key in any provider payload | event rejected |

## Adapter Quality Gate

Each adapter must expose:

- `detect()`: is provider installed/configured?
- `install_hook_config(dry_run=True)`: proposed hook config only; no silent writes.
- `parse_hook_event(raw_json)`: converts one raw event to normalized local event.
- `sanitize_event(event)`: applies provider and global sanitization policy.
- `emit(event)`: sends to local queue/offline cache.

## Integration Gate

Before enabling by default:

- Manual provider works end-to-end.
- The sanitization gate (collector in local mode; collector + independent cloud gate in relay mode) rejects unsanitized provider fields.
- Browser simulator shows Codex and Claude sessions concurrently.
- WAITING alert preempts normal display.
- Quota displays as `unknown` or estimated unless manually configured.

