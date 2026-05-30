# Security Policy

AgentLamp's entire value rests on one promise: **no provider credentials, raw prompts,
transcripts, source code, full local paths, model identifiers, or account plan tiers ever
leave your machine.** Security reports are taken seriously.

## Scope of the promise

- **Local mode (default):** the agent-state frame travels your LAN only. No third party
  sees anything.
- **Relay mode (optional):** the optional public cloud relay receives only **sanitized
  summaries**. Even those carry behavioral metadata — see the honest accounting in
  [`docs/security/threat_model.md`](docs/security/threat_model.md) and the
  Cloud-Visible Data Inventory in [`docs/security/sanitization_policy.md`](docs/security/sanitization_policy.md).
- **v1 is single-owner self-host.** Do not host one relay for multiple unrelated people;
  multi-tenant isolation is a future extension, not a v1 guarantee.

## Reporting a vulnerability

Use **GitHub's private vulnerability reporting** — open the repository's **Security** tab and
click **"Report a vulnerability"**. This keeps the report private between you and the
maintainers; no public issue, no exposed contact address. Please do not open a public issue
for anything that could leak real user data. Expect an acknowledgement within a few days.

High-priority categories: any path that lets unsanitized data reach the relay; any
sanitization bypass; HMAC/replay/auth weaknesses; an unsigned OTA or device-token leak.

## Reviews required for contributions

Any PR touching `docs/security/`, the sanitizer, the alias/hash mechanism, the ingest auth
path, or OTA signing requires an explicit security review before merge (see
[`CONTRIBUTING.md`](CONTRIBUTING.md)).

## Out of scope (v1)

- Physical extraction of ESP32 flash (the device stores only a read-only, revocable token).
- Multi-tenant shared hosting (not a v1 feature).
- A relay operator inferring coarse behavioral metadata in relay mode (documented, not a bug
  — use local mode to avoid it).
