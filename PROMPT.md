# AgentLamp - Worker Loop

## Loop

1. Read `memories/consensus.md`.
2. Read `TASKS.md`.
3. Pick the highest-priority unchecked task.
4. Read only the docs relevant to that task.
5. If the task touches a contract, update docs before implementation.
6. Implement in small, testable steps.
7. Verify locally.
8. Update `TASKS.md` and `memories/consensus.md`.
9. Stop and report when Boss approval is needed for commit, push, deployment, credential entry, domain setup, or hardware flashing.

## Decision Policy

Prefer boring technology and narrow contracts.

For MVP, optimize for:

- security boundary correctness
- frame API stability
- simulator-driven UI iteration
- hardware smoke tests after the frame contract is stable

Avoid:

- provider scraping before sanitization tests exist
- large firmware UI frameworks before a simple frame renderer works
- cloud storage of rich task text
- coupling quota logic to provider-specific assumptions

## Implementation Sequence

1. M0 docs and contracts.
2. M1 cloud mock frame API and simulator.
3. M2 collector manual push and HMAC verification.
4. M3 ESP32 mock frame display.
5. M4 Codex/Claude adapters behind sanitizer.
6. M5 pairing, OTA, heartbeat, and theme system.
7. M6 enclosure and 24-hour stability pass.

## Reporting

Every report should include:

- completed task id
- changed files
- verification command or manual checklist
- risks found
- next action

Do not report "done" without verification evidence.

