# Product Spec

## Name

AgentLamp.

## One-Line Product

A premium physical desktop object that shows the state of AI coding agents, quota windows, waiting approvals, and errors without exposing sensitive account data.

## Users

- Boss operating multiple AI coding tools and accounts.
- Secretary/AI Center coordinating subordinate projects.
- Future operators who want ambient AI fleet awareness.

## Jobs To Be Done

- Know which AI agent needs attention.
- Know whether an account is approaching a quota limit.
- See whether a session is active, waiting, errored, done, stale, or offline.
- Keep provider credentials and local work content off any server (and, in local mode, off any network beyond the LAN).

## Deployment

- **Local mode (default):** collector serves the frame over the LAN, device polls it
  directly — no cloud account, domain, or public TLS. This is the primary experience.
- **Relay mode (optional):** public cloud relay only for viewing the orb away from the LAN.
- **v1 is single-owner self-host.** No shared/multi-tenant hosting. See `architecture.md`.

## MVP Modes (device scenes)

| Mode | Purpose |
|------|---------|
| Fleet | Compact provider/account/session overview |
| Focus | Highest-priority active session |
| Quota | Top 2 quota risks |
| Alert | Interrupt for waiting approval, error, quota danger, offline/stale |
| Diagnostics | Pairing, network, schema, and frame health |

## Non-Goals

- ESP32 web browsing.
- Automatic provider request execution.
- Automatic account switching.
- Automatic quota evasion.
- Uploading raw prompts, transcripts, source code, or credentials.

## Success Criteria

- Frame changes reach ESP32 within 5 seconds under healthy network.
- Device survives malformed JSON and network failures.
- Device shows Offline after 3 consecutive fetch failures.
- Cloud database contains no sensitive credential or raw local work content.
- Display remains readable at 172x320.

