# Contributing to AgentLamp

AgentLamp is a teaching example of bridging hardware to AI-agent state **without leaking
sensitive data**. Contributions are welcome; the bar is set by that promise.

## Ground rules

- **Local mode is the default experience.** Don't make the cloud relay mandatory for any
  core feature.
- **Default-deny stays default-deny.** New fields are enums or keyed-hash labels, never
  free text. If you add a field that carries any user/project/account-derived value, it must
  go through the alias/hash mechanism in `docs/security/sanitization_policy.md` and ship a
  fixture proving an unmapped input emits nothing human-readable.
- **Contracts are the source of truth.** Update the relevant `docs/` contract in the same PR
  as the code, and keep examples consistent (generic aliases, enum values).

## PRs that require a security review before merge

Any change touching:
- `docs/security/**`, the sanitizer, or the alias/hash mechanism
- the collector ingest / auth path (HMAC, nonce, tokens)
- OTA signing or device pairing

…must get an explicit security review and add/update the matching fixtures. See
[`SECURITY.md`](SECURITY.md).

## Tests expected

- Sanitizer fixtures (`docs/security/sanitization_policy.md` → Required Fixtures) pass.
- Contract changes keep the device-frame size under 2 KB and the firmware memory budget.
- Provider adapter changes tolerate unknown hook event names (no hard-fail).

## Style

Match the surrounding docs/code. Keep contract docs terse and example-driven.
