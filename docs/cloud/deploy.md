# Relay Deploy — exact owner-gated Cloudflare steps

> Relay mode only. This is the **owner-gated** procedure to stand up the AgentLamp relay
> on Cloudflare (Worker + Durable Object + KV). Everything *up to* deploy is built and
> locally verified by the build agents (`wrangler dev` + vitest, no cloud auth). The two
> steps below that touch your real Cloudflare account can only be run by **you**, once.
>
> ## 🚨 `wrangler login` is INTERACTIVE and OWNER-ONLY.
>
> It opens a browser for Cloudflare OAuth. **No agent / CI can run it.** Run it yourself
> one time on a machine with a browser. Until you have, every `wrangler deploy` /
> `wrangler secret put` / `wrangler kv` command in this file will fail with an auth error —
> that is expected, not a bug.
>
> ## 🚨 NO hardcoded account / zone / host (invariant I3).
>
> Nothing below bakes in a single account, zone, or hostname. You supply your own values
> as shell variables; `wrangler.toml` reads the host from a `[vars]` entry and the
> account id from your logged-in session. Do not paste a literal account id / IP / host
> into source.

---

## 0. Prerequisites (one-time, owner)

```sh
# install wrangler (Cloudflare's CLI) if you don't have it
npm i -g wrangler          # or: npm i -D wrangler  and use `npx wrangler`

# 🚨 INTERACTIVE — opens a browser, owner runs this once. Agents/CI cannot.
wrangler login

# sanity: confirm who you are logged in as
wrangler whoami
```

Set the values that are yours (no hardcodes — these live only in your shell / a sourced
`.env`, never committed):

```sh
# 🚨 Two host vars, deliberately different — see ../runbook/switch-fast.md for the full why:
#   RELAY_HOST_BARE      — bare hostname (NO scheme). Used here for curl / DNS / the
#                          wrangler --var, which all want the bare form.
#   AGENTLAMP_RELAY_HOST — FULL https URL (WITH scheme). This is the var the COLLECTOR reads
#                          (config.py) and `agentlamp enroll`/`revoke` use VERBATIM as the
#                          base URL — a bare value here breaks `agentlamp revoke`.
export RELAY_HOST_BARE="relay.example.com"               # the hostname you will serve the relay on
export AGENTLAMP_RELAY_HOST="https://$RELAY_HOST_BARE"   # full URL the collector/enroll read
export CF_ZONE="example.com"                              # the Cloudflare zone that owns that host
```

All commands below run from the cloud package directory (`src/cloud/`, where
`wrangler.toml` lives).

---

## 1. Create the KV namespace (non-urgent config / cache only — invariant I4)

```sh
# creates the namespace and prints an id; copy that id into wrangler.toml under
# [[kv_namespaces]] binding = "CONFIG"
wrangler kv namespace create CONFIG
```

> KV is **eventually consistent**, so it holds only non-revocation-critical config. All
> revocation-critical and strongly-consistent state (nonce set, idempotency map, device /
> collector registry, revocation, materialized frame state) lives in the **Durable
> Object**, never here. See `../architecture/architecture.md` → relay invariant I4.

---

## 2. Declare the Durable Object migration

The Durable Object class `RelayDO` (binding `RELAY`) is the singleton state machine. It is
created by a **migration** in `wrangler.toml`, not by an imperative command. Confirm the
migration block is present before first deploy:

```toml
# src/cloud/wrangler.toml  (excerpt — the build already scaffolds this; values below
# match the committed file verbatim)
name = "agentlamp-relay"
main = "src/index.ts"
compatibility_date = "2026-06-02"
compatibility_flags = ["nodejs_compat"]

# Non-secret config. RELAY_HOST ships a deliberately invalid placeholder; override at
# deploy via:  wrangler deploy --var RELAY_HOST:"$RELAY_HOST_BARE"   (bare host — this var is informational)
[vars]
RELAY_HOST = "relay.example.invalid"
RETENTION_DAYS = "30"
DEVICE_RATE_PER_MIN = "20"
COLLECTOR_RATE_PER_MIN = "60"

[[durable_objects.bindings]]
name = "RELAY"
class_name = "RelayDO"

[[kv_namespaces]]
binding = "CONFIG"
id = "REPLACE_WITH_KV_NAMESPACE_ID"      # paste the id printed by step 1
preview_id = "REPLACE_WITH_KV_PREVIEW_ID"

# first-deploy migration that creates the DO class. The DO uses the SQLite-backed
# storage class, so the directive is `new_sqlite_classes` (NOT `new_classes`).
[[migrations]]
tag = "v1"
new_sqlite_classes = ["RelayDO"]
```

The migration runs automatically on the **first** `wrangler deploy` (step 4). You do not
run a separate migrate command for the initial `new_sqlite_classes` migration.

---

## 3. Put the secrets (collector keys + device token — never in source, invariant I3)

These are **secrets**, set via `wrangler secret put` (interactive prompt for the value).
The names below match the Worker's real bindings: `AGENTLAMP_COLLECTOR_KEYS` and
`AGENTLAMP_DEVICE_TOKENS` are read by the Durable Object (`interface Env` in
`src/relay_do.ts`); `AGENTLAMP_ADMIN_TOKEN` is read by the Worker entry (`interface Env` in
`src/index.ts`) for the in-Worker `/admin` gate. They never appear in `wrangler.toml`, in
git, or in any frame:

```sh
# the HMAC verification key material the DO uses to verify collector pushes.
# Format: comma-separated "kid:secret" pairs ("k7:secretA,k8:secretB") — one record per
# enrolled collector kid; the DO's registry parses these and applies revocation.
wrangler secret put AGENTLAMP_COLLECTOR_KEYS

# the device bearer token(s) the /frame + /cacerts endpoints check (stored hashed at rest
# in the DO). Format: comma-separated "device_id:token" pairs (optional bootstrap seed).
wrangler secret put AGENTLAMP_DEVICE_TOKENS

# admin bearer for the /admin revoke routes (POST /admin/collectors/:kid/revoke and
# POST /admin/devices/:id/revoke). The Worker (src/index.ts) READS this and gates /admin with a
# CONSTANT-TIME compare against it. 🚨 If you do NOT set it, /admin is FAIL-CLOSED (returns 403,
# never open) — so revocation via the public route is unavailable until you set this. Cloudflare
# Access can ALSO gate /admin at the edge (MFA/TOTP, defense in depth); the bearer here is the
# in-Worker gate, not a belt-and-suspenders extra.
wrangler secret put AGENTLAMP_ADMIN_TOKEN
```

> Reminder: the device sends `Authorization: Bearer <device_token>` as a header only; the
> relay stores it **hashed**. Rotating it = `wrangler secret put AGENTLAMP_DEVICE_TOKENS`
> again (with the updated `device_id:token` pairs), then re-provision the device's NVS
> token. No firmware rebuild, no URL change.

---

## 4. Deploy

```sh
# pass the host as a var so nothing is hardcoded in wrangler.toml. RELAY_HOST is the BARE
# host (informational var). the first deploy also applies the v1 DO migration from step 2.
wrangler deploy --var RELAY_HOST:"$RELAY_HOST_BARE"
```

Verify it answers (these need no auth secrets to *reach*, they just prove the route is up
and returns the uniform auth errors — a bare unauthenticated `/frame` should give `401`):

```sh
curl -i "https://$RELAY_HOST_BARE/api/v1/device/test/frame"   # expect 401 (uniform auth error)
```

---

## 5. Add the DNS record for the relay host

Point the bare host `$RELAY_HOST_BARE` at the Worker (DNS uses the bare hostname, not the
full URL). With a Workers **custom domain** route this is the cleanest path (Cloudflare
provisions the TLS cert for you):

```sh
# Option A (recommended): attach the host as a Worker custom domain.
# In the dashboard:  Workers & Pages → agentlamp-relay → Settings → Domains & Routes
#                    → Add custom domain →  $RELAY_HOST_BARE
# This creates the DNS record AND issues the edge cert automatically.

# Option B: create a proxied DNS record yourself, then add a Workers route.
#   In the dashboard:  <CF_ZONE> → DNS → Add record
#     Type: AAAA (or CNAME)   Name: <subdomain of $RELAY_HOST_BARE>
#     Target: 100:: (placeholder; the Workers route does the real routing)
#     Proxy status: Proxied (orange cloud — required so the Worker intercepts)
#   Then:  Workers & Pages → agentlamp-relay → Settings → Domains & Routes
#          → Add route →  https://$RELAY_HOST_BARE/*
```

> The relay host is **fixed forever** after this — that is what lets "switch computer /
> switch WiFi in under a minute" work without ever touching the device
> (`../runbook/switch-fast.md`). If you ever move the relay to a new host, you must
> re-provision the device's NVS relay URL once; that is an explicit migration, not a
> routine switch.

---

## 6. Enroll your first computer against the live relay

Now that the relay answers, enroll a machine so it starts pushing (this is the same
sequence you'll run on every additional computer — full walkthrough in
`../runbook/switch-fast.md`):

```sh
# DEFAULT: with no --kid/--secret, enroll MINTS a fresh kid + 256-bit secret for this machine,
# stores the secret in the OS keyring, and (step 6) registers it with the live DO registry over
# the admin route — no wrangler redeploy. --relay-host is the real flag (there is no --relay-url)
# and defaults to $AGENTLAMP_RELAY_HOST — the FULL https URL, already scheme-prefixed (no https:// added).
agentlamp enroll \
    --relay-host "$AGENTLAMP_RELAY_HOST" \
    --collector-id laptop-1 \
    --admin-token "$AGENTLAMP_ADMIN_TOKEN"
# (Alternative — reuse a pre-provisioned AGENTLAMP_COLLECTOR_KEYS pair from step 3:
#    agentlamp enroll --relay-host "$AGENTLAMP_RELAY_HOST" --collector-id laptop-1 \
#                     --kid k7 --secret-stdin --admin-token "$AGENTLAMP_ADMIN_TOKEN")

# enroll only CONFIGURES the stack; it does not start a daemon. Source the env it wrote, then run:
[ -f ~/.config/agentlamp/relay.env ] && . ~/.config/agentlamp/relay.env
agentlamp status                                  # confirm signed push is configured here
cd ../../src && ../.venv/bin/python -m collector.daemon   # this is what actually pushes
```

---

## What is automated vs gated

| Step | Who | Why |
|------|-----|-----|
| Build Worker / DO / KV TS + scaffold `wrangler.toml` | build agent | no cloud auth needed |
| Local verify (`wrangler dev` + vitest vs the parity corpora) | build agent | runs offline |
| `wrangler login` | **owner, once** | 🚨 interactive OAuth, browser required |
| `wrangler kv namespace create` / `secret put` / `deploy` | **owner** | needs the logged-in session |
| DNS record / custom domain | **owner** | touches your real zone `CF_ZONE` |
| Device physical flash + eyeball | **owner** | needs hardware on a network |
