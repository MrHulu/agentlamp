# Switch Fast — change computer or WiFi in under a minute

> Relay mode only. This is the whole point of the relay: you have **many computers** and
> **many networks**, you switch often, and the orb must keep showing your agent state
> without you re-flashing the device or redeploying the cloud.
>
> ## 🚨 The one thing that makes this fast: the device's backend URL NEVER changes.
>
> The ESP32 polls **one fixed relay URL** (`https://<your-relay-host>/api/v1/device/...`).
> Switching computer is a **collector** operation. Switching WiFi is a **device-NVS /
> captive-portal** operation. Neither one touches the relay deployment and neither one
> touches the device's backend config. (Invariants I3 + I5 in
> `../devlog/16-relay-cloudflare-build-spec.md`.)

Throughout this runbook, `<your-relay-host>` is **your** relay hostname (e.g. the DNS
record you created in `../cloud/deploy.md`). There is **no hardcoded host / IP / account**
anywhere — set two env vars once and the commands below are copy-paste:

```sh
# do this once per shell (or put it in your shell profile / a .env you source).
#
# 🚨 TWO vars, and the difference matters:
#   AGENTLAMP_RELAY_HOST  — the var the COLLECTOR reads (config.py). It is a FULL URL,
#       WITH the https:// scheme. `agentlamp enroll`/`revoke` default --relay-host to it and
#       use it VERBATIM as the base URL (they do NOT prepend a scheme), so a bare host here
#       would make `agentlamp revoke` build a scheme-less, broken URL.
#   RELAY_HOST_BARE      — a plain hostname (no scheme), used ONLY for the curl / DNS
#       examples below and in ../cloud/deploy.md (curl/DNS want the bare form).
export AGENTLAMP_RELAY_HOST="https://<your-relay-host>"   # full URL, e.g. https://relay.example.com
export RELAY_HOST_BARE="<your-relay-host>"                # bare host, e.g. relay.example.com
```

> ## Two ways to invoke the CLI — pick one (they are identical)
>
> Every `agentlamp <cmd>` below has a **zero-install canonical form** that works straight from
> a fresh clone with no packaging step:
>
> ```sh
> cd src && ../.venv/bin/python -m collector.cli <cmd> ...      # zero-install canonical form
> ```
>
> If you want the shorter `agentlamp <cmd>` alias used throughout this runbook, do the
> **one-time** editable install from the repo root (then `agentlamp` is on your PATH from any
> directory; add the `keyring` extra if you want the OS keychain backend):
>
> ```sh
> .venv/bin/pip install -e .            # or:  uv pip install -e .   (one time)
> .venv/bin/pip install -e ".[keyring]" # optional: OS keychain backend for the collector secret
> ```
>
> After that, `agentlamp enroll/revoke/status/doctor` resolve to `collector.cli:main`.

---

## A. Switch computer in under a minute

You sat down at a different machine (new laptop, desktop, a borrowed box). You want the
orb to start reflecting *this* machine's agent activity. It is **three steps**, not one
magic command — enroll configures the stack, then you source the env it writes, then you
start the daemon.

### Step 1 — enroll (installs + configures the whole stack)

`enroll` **mints** a fresh `kid` + signing `secret` for this machine by default (256-bit,
stored in the OS keyring) and registers it with the relay's Durable Object over the admin
route — so you do **not** need to pre-provision anything per machine. (You *may* pass
`--kid` / `--secret` to reuse a pre-provisioned `AGENTLAMP_COLLECTOR_KEYS` pair instead — see
`../cloud/deploy.md` §3.) enroll installs everything on this machine:

- hooks (Codex + Claude adapters),
- a freshly generated keyring pepper + your local alias map,
- the collector `secret` stored in the OS keyring under `kid`,
- relay push enabled (writes a sourceable `~/.config/agentlamp/relay.env`),
- **registers** the `kid`+`secret` with the relay's Durable Object (enroll step 6, needs
  the admin token via `--admin-token` / `$AGENTLAMP_ADMIN_TOKEN`; use `--no-cloud-register`
  for a local-only setup).

```sh
# from the repo root, after cloning/pulling the collector onto the new machine.
# --relay-host defaults to $AGENTLAMP_RELAY_HOST (the FULL https URL). With no --kid/--secret,
# enroll mints a fresh kid + high-entropy secret and registers it with the live DO registry
# (no `wrangler deploy`). Keep AGENTLAMP_ACCOUNT distinct per machine if the phone should
# show which computer a session is running on.
export AGENTLAMP_ACCOUNT="laptop-2"
agentlamp enroll \
    --relay-host "$AGENTLAMP_RELAY_HOST" \
    --collector-id laptop-2 \
    --write-claude ~/.claude/settings.json \
    --write-codex ~/.codex/config.toml \
    --admin-token-stdin <<<"$AGENTLAMP_ADMIN_TOKEN"
```

### Step 2 — source the env enroll wrote (so the daemon inherits the relay config)

```sh
# enroll printed this exact line; add it to your shell profile so every session has it.
[ -f ~/.config/agentlamp/relay.env ] && . ~/.config/agentlamp/relay.env
agentlamp status        # confirm mode=relay, host/kid set, secret present
```

### Step 3 — start the daemon (this is what actually signs + pushes)

```sh
# enroll does NOT start a daemon; nothing pushes until you run it.
cd src && ../.venv/bin/python -m collector.daemon
# (run it under your usual supervisor / launchd / nohup for a persistent orb.)
```

### (optional) Tear down the machine you walked away from

```sh
# Run on the OLD machine: forgets the local secret + disables push here AND hits the relay
# revoke route. There is no `disable-push` subcommand — local teardown is `revoke --kid`:
agentlamp revoke --kid k7 --admin-token "$AGENTLAMP_ADMIN_TOKEN"
```

### What enroll does NOT do (and why that's correct)

- It does **not** reuse another computer's `kid` / `secret` unless you explicitly pass them.
  By default it mints a fresh pair; each computer uses its **own** `kid` and its **own**
  local pepper — so revoking one machine never affects the others, and the relay operator
  can never link two machines by a shared secret.
- It does **not** start the daemon. Configuration (step 1) and running (step 3) are
  separate — `agentlamp status` shows OK after step 1, but nothing pushes until the daemon
  runs.
- It does **not** change anything on the device. The orb keeps polling the same URL; the
  moment the DO accepts signed events from the new `kid`, the orb's one owner-wide frame
  reflects this machine (there is no per-device feed binding in v1 — see
  `../architecture/architecture.md` → Device ↔ Collector Binding).

### Revoke a machine you no longer use

Revocation of the **key** is **strongly consistent** — it lands in the Durable Object, not
in eventually-consistent KV (`opRevokeKid` adds the `kid` to the revoked set and deletes it
from the live + enrolled registries). So **future pushes from that `kid` are rejected
immediately**, everywhere (invariant I4):

```sh
# Revoke a collector kid via the admin route. The Worker gates /admin with a constant-time
# bearer check against AGENTLAMP_ADMIN_TOKEN (set via `wrangler secret put` — see
# ../cloud/deploy.md §3); if that secret is unset the route is fail-closed (403). Cloudflare
# Access can ALSO gate /admin at the edge (MFA/TOTP) for defense in depth.
# Note: $RELAY_HOST_BARE is the bare host for curl; the admin route needs an explicit https://.
curl -fsS -X POST \
    -H "Authorization: Bearer $AGENTLAMP_ADMIN_TOKEN" \
    "https://$RELAY_HOST_BARE/admin/collectors/<kid>/revoke"

# Revoke a device token the same way:
curl -fsS -X POST \
    -H "Authorization: Bearer $AGENTLAMP_ADMIN_TOKEN" \
    "https://$RELAY_HOST_BARE/admin/devices/<device_id>/revoke"
```

> ## What revoke does NOT do: it does not purge already-materialized sessions
>
> Revoke stops *new* pushes from the `kid` at once, but it does **not** reach into the
> materialized frame and delete that machine's sessions — there is no per-collector session
> purge in v1. Those sessions instead **age out by liveness**: an active session with no
> new event goes `STALE` after 120 s and `OFFLINE` after 600 s, and once no collector
> heartbeat has arrived for 90 s (with sessions present) the whole fleet renders offline
> (`frame.ts` `effectiveStatus` / `selectScene`; the hourly DO retention alarm only purges
> by age, not on revoke). So after you revoke your *only* enrolled machine the orb settles
> to **offline / stale** within ~the liveness window — never another machine's data, never
> a "magically following" state (invariant I5) — it just is not instantaneous for the
> already-drawn sessions.

---

## B. Switch WiFi in under a minute

You moved the orb to a different network (home → office → a hotspot). The device handles
this itself; you do not reconfigure the backend.

### Case 1 — the network is already known to the device (zero touch)

The device stores **multiple** WiFi networks in NVS. When it boots (or loses its current
AP), it scans and **auto-joins** any stored network in range. If your new location is one
it already knows, there is nothing to do — power it on and it reconnects, then resumes
polling the same relay URL.

### Case 2 — a brand-new network (WiFi fields only, via captive portal)

If no stored network is in range, the device raises a **captive portal**:

```text
1. On your phone/laptop, join the temporary WiFi the orb advertises:
     SSID:      AgentLamp-Setup-<device_id_suffix>
     Password:  agentlamp                       (fixed AP password — WPA2, see AP_PASS)
2. A setup page opens automatically (or browse to http://192.168.4.1).
3. The form has four fields: WiFi network, WiFi password, Server URL, Device token.
   For a routine WiFi switch, change ONLY the two WiFi fields — the Server URL is
   pre-filled with the current relay URL and the Device token is left blank (which keeps
   the existing token). Leave both prefilled/blank and they stay as provisioned.
4. Save. The device ADDS the network to NVS (it does not clobber the others, so next time
   it is Case 1) and rejoins.
```

So the portal **does** surface the relay URL + device token fields, but for a routine WiFi
switch you touch neither: the Server URL field shows the current relay (saving it unchanged
is a no-op) and a blank Device token leaves the stored one untouched. The relay URL / token
/ CA roots remain NVS-provisioned, not invented at network-time (invariant I3) — changing
the relay URL there is an explicit migration, not a routine switch.

### If the orb stays on "offline" after a WiFi switch

That is almost always a **collector** issue, not a network one — the device reconnected
fine but no machine is currently pushing. Check, on whichever computer should be feeding:

```sh
agentlamp status        # is push enabled and signing succeeding?
```

If a machine is pushing and the orb is still offline, the device's TLS may need a CA
refresh (it pulls `/api/v1/device/<device_id>/cacerts` after an NTP sync) — power-cycle
the orb once so it re-runs NTP-before-TLS and refreshes its pinned root bundle.

---

## Quick reference

| You want to | Run | Touches device? | Touches relay deploy? |
|-------------|-----|-----------------|-----------------------|
| Use a new computer | `AGENTLAMP_ACCOUNT=<alias> agentlamp enroll --relay-host "$AGENTLAMP_RELAY_HOST" --collector-id <alias> --admin-token-stdin`, then start the daemon with the same `AGENTLAMP_ACCOUNT` | no | no |
| Stop an old computer | `agentlamp revoke --kid <kid> --admin-token "$AGENTLAMP_ADMIN_TOKEN"` (or admin revoke) | no | no |
| Rejoin a known WiFi | (nothing — auto-joins) | no | no |
| Join a new WiFi | captive portal, WiFi fields only (relay URL / token stay prefilled) | yes (NVS only) | no |
| Deploy / move the relay itself | see `../cloud/deploy.md` | no | yes (owner-gated) |
