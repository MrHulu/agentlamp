# Architecture

## Deployment Modes

AgentLamp ships **two** deployment modes. The collector, the sanitizer, the frame
contract, and the device renderer are **identical** in both — only the transport
between collector and device changes.

### Local mode (DEFAULT) — recommended for almost everyone

The device sits on the same desk and the same LAN as the laptop. There is **no
reason to route private agent state through the public internet** just to light up
a screen 30 cm away. In local mode the collector itself serves the frame over the
LAN and the device polls it directly.

```text
Local machine (one trust zone)
  collector adapters
  sanitizer (default-deny)
  aggregation + display priority
  LOCAL frame server  (binds LAN address, e.g. 192.168.x.x:8787)
        |
        | HTTP(S) GET over LAN, compact read-only frame JSON
        v
ESP32-S3-LCD-1.47B
  frame client → scene renderer → RGB → offline/stale cache
```

Local mode requires **no domain, no public TLS certificate, no cloud account, no
HMAC ingest hop**. It is the smallest attack surface and the easiest to reproduce,
which is why it is the default and the path the QUICKSTART teaches first.

### Relay mode (OPTIONAL) — only when you need to view the orb away from the LAN

When the device must show state while you are on a different network (orb at home,
laptop at the office), the collector pushes sanitized summaries to an optional
public **AgentLamp Cloud** relay, and the device polls the relay instead of the LAN.

```text
Local machine
  collector adapters → sanitizer → signed push client
        |
        | HTTPS POST, sanitized event summaries (HMAC-signed)
        v
AgentLamp Cloud (relay)
  collector ingest → auth/replay → event store → aggregation
  display priority engine → device frame API → admin + simulator
        |
        | HTTPS GET, compact read-only frame JSON
        v
ESP32-S3-LCD-1.47B (same firmware, different base URL)
```

Relay mode is where the entire ingest/HMAC/multi-hop security surface lives. If you
do not need remote viewing, do not deploy it.

> **Why this matters for the project's goal.** AgentLamp is published as a teaching
> example of *bridging hardware to AI-agent state*. Local mode lets a stranger get a
> working orb with a laptop and a $15 board and zero cloud — the lesson is learnable
> before any of the cloud complexity. Relay mode is the advanced chapter.

## Tenancy (v1)

**v1 is strictly single-tenant, single-owner self-host.** One deployment (local or
relay) belongs to exactly one person. There is no signup, no shared hosting, no
cross-account separation — because there is exactly one account.

- The relay build MUST NOT expose a public registration flow.
- Hosting one relay for multiple unrelated people is **explicitly out of scope for v1**
  and unsafe (see `device_collector_binding` below — it scopes data *within* one owner,
  it is not a tenant boundary).
- Multi-tenant support (`owner_id` on every row + token, per-tenant frame scoping) is a
  documented future extension, not a v1 feature. Until it exists, the README and
  `SECURITY.md` MUST state "one owner per deployment."

## Device ↔ Collector Binding

A device must not display events from collectors it was not paired with, even within a
single owner (e.g. a personal collector vs a work collector on two machines).

- Each `device` is bound to an explicit set of `collector_id`s (`device_feed`).
- Frame generation aggregates **only** events from bound collectors.
- An unbound device renders the Pairing scene, never another collector's data.

## Ownership Boundaries

| Layer | Owns | Must Not Own |
|-------|------|--------------|
| Collector | Local reads, sanitization, event signing, aggregation/priority (local mode), offline replay (relay mode) | Provider credentials, provider browser sessions |
| Cloud relay (relay mode only) | Auth, ingest, dedupe, state machines, priority, frame generation, admin | Provider credentials, raw local content, any unsanitized field |
| ESP32 | Fetch, parse, render, animation, RGB, offline/stale cache | Sorting sessions, quota risk calculation, provider logic |

> In **local mode** the collector owns aggregation + priority + frame generation
> directly (no cloud). In **relay mode** those move to the cloud. The display-priority
> rules are defined once in `cloud_contract.md` and reused by the local frame server.

## Data Flow

1. Collector reads local status from hooks, local files, CLI output, or manual input.
2. Collector normalizes provider data into an event envelope.
3. Collector sanitizes fields with default-deny allowlists (see `sanitization_policy.md`).
4. Aggregation computes sessions/quota/alerts and the display priority order.
5. **Local mode:** the collector's LAN frame server returns the compact frame on GET.
   **Relay mode:** the collector signs the exact payload hash and POSTs to the cloud;
   the cloud validates identity/timestamp/nonce/hash/HMAC, dedupes, aggregates, and
   serves the frame.
6. ESP32 polls the frame endpoint (LAN or relay) every 3-5 s and renders the scene.

## MVP Build Order

1. Contract docs (this set).
2. **Local frame server** + mock state + browser simulator (no cloud).
3. Manual collector adapter feeding the local frame server.
4. ESP32 frame renderer against the local frame server.
5. Codex/Claude hook adapters (after sanitizer fixtures pass).
6. Relay mode: signed cloud ingest + cloud frame API (optional, last).

## Storage Model

The collector (local mode) and the cloud relay (relay mode) share this materialized model:

- `providers`
- `accounts`
- `collectors`
- `collector_events`
- `sessions`
- `quota_windows`
- `alerts`
- `devices`
- `device_feed`  *(device → bound collector_ids; see Device ↔ Collector Binding)*
- `device_frames`
- `audit_logs`

## State Rules

- Events are append-only **within the retention window** (see Retention).
- Materialized session/quota/device state is rebuildable from sanitized events.
- Duplicate events do not change state twice.
- Late events can update history but must not resurrect timed-out sessions without a
  newer `updated_at`.
- Frame sequence increases only when rendered content or scene changes.

### Session Lifetime / Liveness (closes the "stuck CODING forever" gap)

Lifecycle hooks can be lost (agent killed → no `Stop`/`SessionEnd`). State must not
trust a missing close event. Explicit timeouts (defaults; tune in collector config):

| Transition | Trigger | Default |
|------------|---------|---------|
| active session → `STALE` | no event for this session | 120 s |
| active session → `OFFLINE`/closed | no event for this session | 600 s |
| all of a collector's sessions → `OFFLINE` | no collector heartbeat | 90 s |

The collector emits `collector.heartbeat` on a fixed interval; the aggregator (local or
cloud) applies these timeouts so a dead session can never render as active.

### Retention (closes the unbounded-history side-channel)

- Raw sanitized `collector_events` are purged after a default **30 days**; only the
  materialized state is kept long-term.
- A purge job runs on a fixed schedule and is auditable.
- In local mode retention applies to the collector's local store; in relay mode to the
  cloud DB.
- Rationale and the residual metadata exposure are documented in `threat_model.md`.
