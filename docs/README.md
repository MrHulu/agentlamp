# Documentation Index

This directory is the implementation contract for AgentLamp.

Read order (canonical contracts — these are authoritative):

1. `product/product_spec.md`
2. `architecture/architecture.md` (local-mode-first; relay optional)
3. `security/security_model.md`
4. `security/threat_model.md`
5. `security/sanitization_policy.md`
6. `security/pairing_and_auth.md`
7. `api/device_frame_api.md`
8. `api/collector_ingest_api.md`
9. Layer contracts: `cloud/`, `collector/`, `firmware/`, `ui/`
10. Provider contracts: `providers/`
11. `BUILD.md` (hardware BOM + local-mode quickstart)

Historical only (do NOT implement directly — superseded by the canonical contracts above):

- `original/agentlamp_ai_spec.md` — archived brainstorming spec; carries a "do not implement" banner.
- `research/research-before-build.md` — pre-reframe research; some conclusions superseded by local-first.

Rule: when implementation and docs disagree, stop and update the contract before continuing.
