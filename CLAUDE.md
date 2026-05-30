# AgentLamp - Autonomous Worker Brief

## Mission

Build AgentLamp: a premium physical desktop status terminal for AI coding agents,
published as a teaching example of bridging hardware to AI-agent state.

The system observes Codex, Claude, Cockpit Tools, CodexBar, and future providers through a
local collector that **sanitizes by default-deny**. **Local mode is the default:** the
collector serves a compact read-only frame over the LAN and the Waveshare ESP32-S3-LCD-1.47B
polls it directly — no cloud account, domain, or public TLS. **Relay mode is optional:** for
viewing the orb away from the LAN, the collector pushes HMAC-signed sanitized summaries to an
optional public cloud relay. **v1 is single-owner self-host.** See `docs/architecture/architecture.md`.

## Product Boundary

Allowed in v1:

- Display active sessions, quota windows, reset countdowns, alerts, stale/offline states, and device heartbeat.
- Aggregate sanitized state locally and serve the frame over the LAN (local mode, default).
- Optionally push collector events to a relay with HMAC signatures + replay protection (relay mode).
- Pull compact device frame JSON from the LAN collector (local) or the relay (relay), over the frame API.

Forbidden in v1:

- Automatic account switching.
- Quota evasion or task dispatch based on quota avoidance.
- Proxying to OpenAI, Anthropic, Codex, Claude, or browser sessions.
- Uploading cookies, refresh tokens, raw credential files, full prompts, full transcripts,
  source code, full local paths, real model identifiers, or account plan tiers.
- Multi-tenant / shared hosting (single-owner only in v1).
- ESP32 rendering a web dashboard.

## Control Interfaces

| File | Purpose |
|------|---------|
| `TASKS.md` | Boss/Secretary task queue |
| `PROMPT.md` | Worker loop and execution policy |
| `memories/consensus.md` | Current state, decisions, next action |
| `docs/` | Product, architecture, API, security, UI, firmware contracts |

## Work Rules

1. Read `memories/consensus.md` before starting work.
2. Take the top unchecked task from `TASKS.md`.
3. Update `memories/consensus.md` before and after substantial work.
4. Use docs as executable contracts. If implementation reveals a contract gap, update the doc first.
5. Do not commit or push without explicit Boss approval.
6. Never write secrets into the repo.
7. For code changes, use tests appropriate to the layer:
   - Cloud: unit + API integration tests.
   - Collector: sanitizer + signing + offline replay tests.
   - Firmware: schema parser tests where possible plus hardware smoke checklist.
   - UI simulator: browser screenshot checks.

## Recommended Team

| Role | Responsibility |
|------|----------------|
| CTO | Architecture, API contracts, data model |
| Security reviewer | Sanitization, HMAC, pairing, cloud exposure |
| Embedded firmware reviewer | ESP32 frame renderer, Wi-Fi/TLS/offline behavior |
| Fullstack engineer | FastAPI, admin, simulator, collector CLI |
| QA | Contract tests, failure injection, 24-hour stability plan |
| Product/UI reviewer | 172x320 display hierarchy and ambient RGB behavior |

## Initial Stack

| Layer | Stack |
|------|-------|
| Collector + local frame server (default) | Python 3.11+, FastAPI/uvicorn, httpx, pydantic, keyring, platformdirs, watchdog, tenacity; serves the frame + simulator over the LAN |
| Cloud relay (optional) | Same FastAPI app + PostgreSQL, Redis, Caddy; adds signed ingest, internet-exposed admin, multi-device relay |
| Firmware | PlatformIO, Arduino-ESP32 3.x, LovyanGFX or LVGL, ArduinoJson, WiFiClientSecure (relay) / HTTPClient (LAN), Preferences |
| Admin/simulator | 172x320 live preview served by the local frame server (local) or the cloud (relay) |

## Provider Support

Codex and Claude are P1 providers.

- Use lifecycle hooks as the primary signal source.
- Do not parse transcript/history files as the primary API.
- Do not upload prompts, transcript paths, current working directories, raw file paths, raw commands, raw tool payloads, or model output.
- Quota starts as manual/unknown until a stable explicit source is proven.

## Definition of Done

A task is done only when:

- The relevant contract doc is updated.
- Code or config changes are verified by tests or a documented manual checklist.
- Sensitive data paths are checked against `docs/security/sanitization_policy.md`.
- `memories/consensus.md` and `TASKS.md` reflect the new state.
