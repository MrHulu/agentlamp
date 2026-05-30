# Provider Integration

AgentLamp v1 must support Codex and Claude without depending on raw local transcripts.

## Priority

| Priority | Provider | Scope |
|----------|----------|-------|
| P0 | Manual provider | Proves ingest, sanitization, frame generation |
| P1 | Codex CLI/local | Hook events and optional CLI/cloud JSON status |
| P1 | Claude Code/local | Hook events and optional redacted OTel metrics/events |
| P2 | Cockpit Tools / CodexBar | Local-only bridge once P1 is stable |

## Stable Integration Principle

Use provider lifecycle events as signals. Do not treat local transcript/history files as the primary API.

Allowed:

- Hook event name.
- Session id after hashing or provider-local opaque id.
- Provider name.
- Sanitized account alias.
- Tool category, not raw tool input.
- Status transitions.
- Sanitized quota summary with confidence.
- Redacted OTel metrics/events where prompt/tool detail logging remains disabled.

Forbidden:

- Raw prompt.
- Raw transcript line.
- `transcript_path`.
- `cwd`.
- Raw file path.
- Raw shell command.
- Raw tool input/output.
- Provider auth cookies/tokens.

## Docs

- [Provider normalization](provider_normalization.md)
- [Codex adapter](codex_adapter.md)
- [Claude adapter](claude_adapter.md)
- [Adapter acceptance](adapter_acceptance.md)

