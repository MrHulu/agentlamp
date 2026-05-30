# AgentLamp

Physical AI agent status terminal for observing Codex, Claude, and future provider sessions from a Waveshare ESP32-S3-LCD-1.47B device.

## Mission

A secure, read-only, **glanceable** AI agent status orb you build yourself:

- A local collector reads sensitive local state (Codex / Claude CLI sessions, quota, alerts) and **sanitizes by default-deny** before anything leaves the process.
- **Local mode (default):** the collector serves a compact JSON frame over your LAN; the ESP32 polls it directly. No domain, no public TLS, no cloud account.
- **Relay mode (optional):** for viewing the orb away from the LAN, the collector pushes HMAC-signed sanitized summaries to an optional public AgentLamp Cloud relay.
- No provider cookies, refresh tokens, raw prompts, transcripts, source code, full local paths, model identifiers, or account plan tiers are ever uploaded.

Published as a **teaching example of bridging hardware to AI-agent state** — local mode is learnable with a laptop and a ~$15 board before any cloud complexity. **v1 is single-owner self-host** (no shared/multi-tenant hosting).

## Current Scope

MVP is documentation-first and contract-first, **local mode first**:

1. Freeze product, architecture, and the sanitization mechanism.
2. Implement the **local LAN frame server** + browser simulator (no cloud).
3. Implement a manual collector adapter feeding the local frame server.
4. Implement the ESP32 frame renderer against the local frame server.
5. Add Codex and Claude adapters only after sanitization fixtures pass.
6. Add optional relay mode (signed cloud ingest) last.

## Getting Started

See [`docs/BUILD.md`](docs/BUILD.md) for the hardware bill of materials, wiring, and the
end-to-end local-mode quickstart (cloud-free). Security posture — what an attacker can and
cannot learn: [`SECURITY.md`](SECURITY.md) + [`docs/security/threat_model.md`](docs/security/threat_model.md).

## Project Layout

```text
agentlamp/
├── AGENTS.md
├── CLAUDE.md
├── PROMPT.md
├── TASKS.md
├── docs/
│   ├── original/
│   ├── product/
│   ├── architecture/
│   ├── api/
│   ├── security/
│   ├── firmware/
│   ├── cloud/
│   ├── collector/
│   ├── ui/
│   └── research/
├── src/
│   ├── cloud/
│   ├── collector/
│   └── admin/
├── firmware/
├── tests/
├── scripts/
└── memories/
```

## Canonical Docs

- [Original imported spec](docs/original/agentlamp_ai_spec.md)
- [Product spec](docs/product/product_spec.md)
- [Architecture](docs/architecture/architecture.md)
- [Device frame API](docs/api/device_frame_api.md)
- [Collector ingest API](docs/api/collector_ingest_api.md)
- [Security model](docs/security/security_model.md)
- [Threat model](docs/security/threat_model.md)
- [Sanitization policy](docs/security/sanitization_policy.md)
- [Pairing and auth](docs/security/pairing_and_auth.md)
- [Firmware contract](docs/firmware/firmware_contract.md)
- [Cloud contract](docs/cloud/cloud_contract.md)
- [Collector contract](docs/collector/collector_contract.md)
- [Provider integration](docs/providers/README.md)
- [Codex adapter](docs/providers/codex_adapter.md)
- [Claude adapter](docs/providers/claude_adapter.md)
- [Display spec](docs/ui/display_spec.md)
- [Research before build](docs/research/research-before-build.md)

## Hard Boundary

Do not build this as an ESP32 web browser. The device fetches JSON frames and renders them locally.

Do not implement automatic account switching, quota evasion, provider request proxying, or cloud credential storage.

## License

MIT — see [`LICENSE`](LICENSE). Contributions touching `docs/security/` or any sanitization /
auth path require a security review (see [`SECURITY.md`](SECURITY.md) and [`CONTRIBUTING.md`](CONTRIBUTING.md)).
