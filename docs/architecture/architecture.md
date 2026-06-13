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

> ## 🚨 RELAY INVARIANTS — read before touching any relay code (binding contract)
>
> These are lifted verbatim from the build spec
> (`../devlog/16-relay-cloudflare-build-spec.md` → NON-NEGOTIABLE INVARIANTS). Violating
> either one is a **NO-GO**, not a code-review nit.
>
> - **I1 — The cloud VALIDATES, it NEVER re-sanitizes.** The Python collector is the
>   *only* place raw → safe heuristic redaction (NFKC, zero-width strip, HMAC aliasing,
>   path/secret scrubbing) ever happens. The Cloudflare Worker / Durable Object only
>   **VALIDATES** the already-sanitized output: payload-key **allowlist** +
>   forbidden-key **reject** + **enum membership** + **neutral-alias shape** +
>   forbidden-pattern **reject** scan. The cloud must **reject, never coerce**. It is a
>   NO-GO if the cloud ever accepts a raw `cwd` / `prompt` / `model` / path and tries to
>   sanitize it in TypeScript. (Rationale lives in
>   `../security/sanitization_policy.md` → Cloud Requirements.)
> - **I3 — NO single-machine / single-network hardcodes in any relay path.** The relay
>   URL, device token, CA roots, collector `kid` / secret, and Cloudflare account / zone
>   all come from device NVS provisioning or from cloud / collector env + secrets — never
>   from source. The local-mode hardcodes in `../../firmware/src/config.h`
>   (`192.168.1.148`, `yangzhenzhous-macbook-air`) MUST NOT compile into a relay build.
>   The whole point of relay mode is "not tied to one Mac on one network."

The relay is a **Cloudflare-only** deployment: a single **Worker** (HTTP entry +
HMAC verify + edge rate-limit + uniform auth errors) in front of one **Durable Object**
(the strongly-consistent state machine) plus a **KV** namespace (non-urgent config only).

```text
Each computer (its own collector + its own kid)
  collector adapters → sanitizer (Python — the ONLY transform) → HMAC-signed push
        |
        | HTTPS POST  /api/v1/collectors/{kid}/events   (sanitized summaries, HMAC-signed)
        v
Cloudflare
  Worker  — verify HMAC · edge rate-limit · route · uniform 401/403/404
     └─ RPC ─→ Durable Object "relay"  (singleton; owns ALL revocation-critical state):
                 nonce / idempotency · device + collector registry + revocation ·
                 VALIDATE the sanitized event (I1, validate-only) · apply → materialized
                 state · frame generation · retention-purge + audit via DO alarms
  KV "CONFIG" — non-urgent config / cache ONLY (never revocation-critical)
        |
        | HTTPS GET  /api/v1/device/{device_id}/frame   (compact read-only frame JSON)
        v
ESP32-S3-LCD-1.47B  — same firmware, ONE fixed relay base URL (never changes)
  Authorization: Bearer <device_token>  ·  pinned ROOT CA bundle + NTP-before-TLS
  WiFi: multi-network NVS store + captive-portal fallback
```

#### Why the Worker + Durable Object + KV split (I4)

The **Durable Object owns all revocation-critical and strongly-consistent state** — the
nonce set, the idempotency map, the device / collector registry, revocation flags, the
materialized frame state, and the retention-purge / audit alarms. A revoked `kid` or
device must stop being accepted **immediately**, so it cannot live in eventually-consistent
KV. **KV holds only non-urgent config / cache.** The Worker is stateless: it verifies the
HMAC, applies an edge rate-limit, and forwards to the single DO instance.

#### Switching computer / network is a collector + device-NVS operation, never a redeploy

Because the device polls **one fixed relay URL**, neither switching computer nor switching
WiFi touches the relay or the device's backend config:

- **Switch computer** → run the collector on the new machine. `agentlamp enroll` installs the
  whole stack (hooks + keyring pepper / aliases + collector secret + relay push — invariant I5)
  and, **omitting `--kid` / `--secret`, MINTS a fresh `kid` + a 256-bit signing secret for you**
  — that is the headline one-liner: `AGENTLAMP_ADMIN_TOKEN=… agentlamp enroll --relay-host
  {RELAY_URL} --collector-id <name>`. Enroll's step 6 self-registers the minted pair with the
  DO's live registry over the authed `/admin` route (**no `wrangler deploy`**). The admin call
  carries the bearer **plus** the DO's required freshness headers (`X-ACO-Timestamp` +
  single-use `X-ACO-Nonce`). Passing explicit `--kid` / `--secret` is the **optional**
  rotation / pinning path; the static `AGENTLAMP_COLLECTOR_KEYS` binding (`../cloud/deploy.md`
  §3) is the bootstrap/seed alternative, no longer the only way in. Bring-up: `enroll` → start
  the daemon (config is read from `relay.json` directly — sourcing `relay.env` is optional).
  Revoke = `agentlamp revoke --kid <kid>` (the `/admin/collectors/{kid}/revoke` route removes
  the `kid` from the DO registry, effective immediately). An un-enrolled machine shows offline /
  stale, never "magically follows."
- **Switch WiFi** → the device auto-joins any stored network, else raises a captive portal
  (AP `AgentLamp-Setup-<suffix>`, password `agentlamp`). The form carries WiFi network /
  password / Server URL / Device token, but a routine switch changes only the two WiFi
  fields — the Server URL is pre-filled with the current relay and a blank token keeps the
  stored one, so the backend URL is unchanged.

Step-by-step copy-paste flows live in `../runbook/switch-fast.md`; the exact owner-gated
deploy (KV namespace + DO migration + secrets + `wrangler deploy` + DNS) is in
`../cloud/deploy.md`.

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
  and unsafe (see Device ↔ Collector Binding below — per-device feed scoping is an
  *aspirational* future control, not yet implemented; it is in any case not a tenant
  boundary).
- Multi-tenant support (`owner_id` on every row + token, per-tenant frame scoping) is a
  documented future extension, not a v1 feature. Until it exists, the README and
  `SECURITY.md` MUST state "one owner per deployment."

## Device ↔ Collector Binding

> **What v1 actually does (relay mode).** The Durable Object materializes **one
> owner-wide frame** from every accepted collector's events — there is a single
> `sessions` / `quota` state (`frame.ts` `buildFrame` / `applySanitizedEvent`), not a
> per-device slice. The `device_id` is an **auth / identity** value only: `GET
> /api/v1/device/{device_id}/frame` verifies the device's bearer token (and revocation,
> I4) and then stamps that `device_id` into the same shared frame. Every authorized
> device of the one owner sees the same materialized state.

**Aspirational (not yet implemented).** A finer-grained guarantee — a device displays only
events from collectors it was explicitly paired with, even within a single owner (e.g. a
personal collector vs a work collector) — is a documented future control:

- each `device` bound to an explicit set of `collector_id`s (a `device_feed`),
- frame generation filtered to **only** bound collectors,
- an unbound device rendering the Pairing scene instead of shared data.

None of this per-device feed filtering exists in the v1 relay yet; `buildFrame` does not
take a feed. Treat the bullets above as the planned shape, not current behavior.

## Ownership Boundaries

| Layer | Owns | Must Not Own |
|-------|------|--------------|
| Collector | Local reads, sanitization, event signing, aggregation/priority (local mode), offline replay (relay mode) | Provider credentials, provider browser sessions |
| Cloud relay (relay mode only) | Auth, ingest, dedupe, state machines, priority, frame generation, admin | Provider credentials, raw local content, any unsanitized field |
| Display / reader (ESP32 LCD, iPhone widget, … any future hardware) | Fetch, parse, render, animation, offline/stale cache | Sorting sessions, quota risk calculation, provider logic |

> In **local mode** the collector owns aggregation + priority + frame generation
> directly (no cloud). In **relay mode** those move to the cloud. The display-priority
> rules are defined once in `cloud_contract.md` and reused by the local frame server.

### Heterogeneous readers (hardware extensibility)

Readers are **interchangeable consumers of the `GET /api/v1/device/:id/frame` contract**, not a
code module. The ESP32 LCD (C++) and the iPhone Scriptable widget (JS) share **zero rendering
code** — only the versioned schema-v1 frame + Bearer auth + transport. That wire contract *is*
the hardware-abstraction boundary: a new hardware type (Android widget, e-ink board, desktop
menubar, …) attaches by implementing "fetch → parse → render" against the same frame, with **no
collector or cloud change** — exactly as the iPhone did. **Adding a reader is never a core
refactor**, and there is deliberately no shared "Reader" class to abstract — an LCD renderer and
an iOS-widget renderer have nothing renderable in common; forcing shared code would be the wrong
abstraction.

**One known shape limit (reactive, not yet built):** the frame is currently sized for the lamp
(`FLEET_MAX_ROWS=5`, `FRAME_BYTE_CAP=2048` — a 1.47" LCD). Phone-sized widgets fit it fine. A
future *larger* display that wants more rows should negotiate via an optional `X-Frame-Profile`
header (the reader already sends `X-Frame-Schema-Version`); the frame builder shapes the caps per
profile, absent header → default `lamp`. ~30 additive lines at the existing seam, fully backward-
compatible. **Build it when a hardware type actually strains the lamp shape — not before** (today
ESP32 + phone both fit).

> Reader catalog (supported hardware + per-device code/deploy): [`../../readers/`](../../readers/).

### Scaling envelope (single-owner by design)

The relay routes **all** collectors and **all** device reads to one global Durable Object
(`idFromName("relay")`). That is the right call **at single-owner scale** — it gives strongly
consistent, zero-config fan-in (one shared frame, see the multi-device plan) — but it is a
deliberate ceiling, not a free lunch. Per Cloudflare's own guidance a single DO has a soft
~1,000 req/s limit, and `blockConcurrencyWhile()` held across I/O (fetch/KV/R2) collapses
throughput further; the documented scaling pattern is to **shard by your unit of coordination**
and keep the DO doing fan-out with heavy aggregation moved to a separate worker. AgentLamp's
load (a handful of computers + a phone polling every 5–15 min) is orders of magnitude under that
ceiling, so the singleton stays. **Escape hatch if this ever grows to a fleet / multi-tenant
product:** shard the DO by owner — `idFromName(owner_id)` instead of the constant `"relay"` —
which also restores the per-owner isolation v1 intentionally skipped (see *Tenancy (v1)*). Don't
build that until real multi-owner load demands it.
> Source: Cloudflare, *Rules of Durable Objects* (best-practices) + *Limits*.

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
- `device_feed`  *(aspirational: device → bound collector_ids; NOT implemented in the v1
  relay, which materializes one owner-wide frame — see Device ↔ Collector Binding)*
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

The collector daemon emits `collector.heartbeat` on a fixed interval so an idle-but-present
owner never decays to offline; the aggregator (local or cloud) applies these timeouts so a
dead session can never render as active. The transport differs by mode:

- **Local mode** → the daemon POSTs an empty body to `/admin/heartbeat` over loopback
  (`daemon._heartbeat`).
- **Relay mode** → the daemon pushes a **signed, payload-less** `collector.heartbeat`
  ingest event to `/api/v1/collectors/{kid}/events` (`daemon._relay_heartbeat` →
  `relaypost.push_heartbeat`); the loopback `/admin/heartbeat` does not reach the cloud.
  The relay short-circuits `event_type == "collector.heartbeat"` **before** the
  validate-only gate (it carries no payload, so there is nothing to sanitize) and just
  bumps `last_collector_heartbeat`. Any accepted session event also refreshes that clock,
  so the explicit heartbeat only fires while the owner is idle. If `now -
  last_collector_heartbeat > 90 s` while sessions exist, the whole fleet renders offline
  (`frame.ts` `selectScene`).

### Retention (closes the unbounded-history side-channel)

- Raw sanitized `collector_events` are purged after a default **30 days**; only the
  materialized state is kept long-term.
- A purge job runs on a fixed schedule and is auditable.
- In local mode retention applies to the collector's local store; in relay mode to the
  cloud DB.
- Rationale and the residual metadata exposure are documented in `threat_model.md`.
