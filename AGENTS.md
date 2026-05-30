# AGENTS.md

Codex entrypoint for AgentLamp.

Read and obey [CLAUDE.md](CLAUDE.md). `CLAUDE.md` is the single source of truth for mission, safety boundary, workflow, and control interfaces.

Critical local rules:

- No git commit or push unless Boss explicitly approves.
- Local mode is the default; the public cloud relay is optional (remote viewing only). v1 is single-owner.
- Device API is read-only.
- Nothing leaves the machine unsanitized; in relay mode the cloud stores only sanitized summaries (no credentials, raw prompts, transcripts, source, full paths, model ids, or plan tiers).
- ESP32 must not render HTML/CSS/JS dashboards.
- New implementation work starts from `TASKS.md` and must update `memories/consensus.md`.

