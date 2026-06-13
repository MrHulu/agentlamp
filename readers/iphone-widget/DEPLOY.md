# Deploy — iPhone widget reader

How to put AgentLamp on your iPhone home/lock screen. ~5 minutes, no App Store
purchase, no jailbreak, no developer account.

> ## 🚨 READ FIRST — three things that make or break this
>
> 1. **Widget = one script.** Paste [`agentlamp-widget.js`](agentlamp-widget.js) into one
>    Scriptable script named `AgentLamp`. Only the optional alert script needs
>    [`frame-view.js`](frame-view.js).
> 2. **🔒 Public-repo red line.** Every value here is a placeholder
>    (`{RELAY_URL}` / `{DEVICE_ID}` / `{DEVICE_TOKEN}` / `{ADMIN_TOKEN}`). Real values
>    live ONLY in `~/.config/agentlamp/relay-deploy.txt` on your Mac and on-device —
>    never typed into the repo, a URL, or a QR code. The token rides an
>    `Authorization` header only.
> 3. **Cloud must already have your data.** The widget only *reads*. The relay must be
>    live, your collector pushing to it (relay mode), and a device token enrolled —
>    see [Prerequisites](#prerequisites-on-your-mac--one-time).

## What you get

The same scenes the ESP32 lamp shows (focus / fleet / quota / alert), rendered as an
iOS widget that reads one **aggregated** frame across all your machines.

- **Current UI:** white HULU card, Chinese status labels, Claude/Codex provider colors, plan
  chips (`Max 20×`, `Pro`), absolute reset times, and 5h/7d quota **remaining** percentages.
- **Multi-device:** the widget subtitle can show each machine/account alias — give each
  machine a distinct `AGENTLAMP_ACCOUNT` (see [Telling machines apart](#multi-device--telling-machines-apart)).
- **Never blanks:** it caches the last good frame and shows it flagged offline when a transient
  fetch fails, so a blip doesn't wipe the screen.
- **Honest limit:** `refreshAfterDate` is only a *hint* — iOS owns the cadence (typically
  5–15 min, longer on low battery or if you rarely look). For faster "agent needs you" pings,
  add the [P2 alert script](#p2--instant-alerts) — its latency is **your automation interval**
  (≈ minutes), not seconds.

## Prerequisites (on your Mac — one time)

1. **Relay live + collector pushing.** If your collector is still in LOCAL mode, flip it to
   relay first — see [`../../docs/plans/2026-06-07-multi-device-cloud-aggregation.md`](../../docs/plans/2026-06-07-multi-device-cloud-aggregation.md) §4
   (one `agentlamp enroll` + daemon restart per machine).
2. **Device token — mint it, then enroll** for the phone (admin route, no browser, no redeploy).
   The device token is **owner-supplied** (unlike the collector secret, which the relay self-mints),
   so create one first and store it as `DEVICE_TOKEN` in `relay-deploy.txt`:

   ```sh
   python3 -c "import secrets; print(secrets.token_hex(32))"   # → save as DEVICE_TOKEN (never commit it)
   ```

   Then enroll it — the relay's Durable Object persists only its **hash**, never the raw token:

   ```sh
   # Read RELAY_URL / ADMIN_TOKEN / DEVICE_ID / DEVICE_TOKEN from relay-deploy.txt (don't echo them).
   TS=$(date +%s); NONCE=$(python3 -c "import secrets;print(secrets.token_hex(16))")
   curl -sS -X POST "$RELAY_URL/admin/devices/$DEVICE_ID/enroll" \
     -H "Authorization: Bearer $ADMIN_TOKEN" \
     -H "X-ACO-Timestamp: $TS" -H "X-ACO-Nonce: $NONCE" \
     -H "content-type: application/json" \
     -d "{\"token\":\"$DEVICE_TOKEN\"}"

   # Verify the phone's token can pull a frame (expect 200):
   curl -s -o /dev/null -w "%{http_code}\n" \
     -H "Authorization: Bearer $DEVICE_TOKEN" -H "X-Frame-Schema-Version: 1" \
     "$RELAY_URL/api/v1/device/$DEVICE_ID/frame"
   ```

## Add another computer (Mac) — collector deployment

The phone does **not** need another device token for a second computer. Add the new computer as
another collector; the existing phone token still reads the one aggregated frame.

```sh
# 1. Clone + install the CLI.
git clone https://github.com/MrHulu/agentlamp.git
cd agentlamp
python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/pip install -e ".[all]"

# 2. Read RELAY_URL / ADMIN_TOKEN from ~/.config/agentlamp/relay-deploy.txt on your main Mac.
#    Transfer them out-of-band; never commit or paste them into repo files.
export AGENTLAMP_RELAY_HOST="{RELAY_URL}"       # full https:// URL
export AGENTLAMP_ADMIN_TOKEN="{ADMIN_TOKEN}"

# 3. Pick a neutral machine label. Use the SAME label for collector_id and account.
export MACHINE_ALIAS="macbook"
export AGENTLAMP_ACCOUNT="$MACHINE_ALIAS"      # what the phone uses to distinguish machines

# 4. Enroll this Mac. With no --kid/--secret, the CLI mints a fresh kid + secret and
#    registers it with the relay Durable Object. Hooks are merged additively with backups.
.venv/bin/agentlamp enroll \
  --relay-host "$AGENTLAMP_RELAY_HOST" \
  --collector-id "$MACHINE_ALIAS" \
  --write-claude ~/.claude/settings.json \
  --write-codex ~/.codex/config.toml \
  --admin-token-stdin <<<"$AGENTLAMP_ADMIN_TOKEN"

# 5. Run once in the foreground to verify signing + quota push.
AGENTLAMP_ACCOUNT="$MACHINE_ALIAS" \
AGENTLAMP_OWNER_LABELS=1 \
AGENTLAMP_QUOTA_ENABLED=1 \
  .venv/bin/python src/collector/daemon.py --once
```

For persistent macOS launchd, use the same `ProgramArguments` as your main Mac, but set
`EnvironmentVariables` with at least:

```text
AGENTLAMP_ACCOUNT=<MACHINE_ALIAS>
AGENTLAMP_OWNER_LABELS=1
AGENTLAMP_QUOTA_ENABLED=1
```

Then load/kick the daemon:

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hulu.agentlamp.daemon.plist
launchctl kickstart -k gui/$(id -u)/com.hulu.agentlamp.daemon
.venv/bin/agentlamp status
```

`agentlamp status` should show `mode: relay`, the relay host, a `collector_id`, a `kid`, and
`secret: present`. Start a Claude/Codex session on each Mac; after the next pushes, the phone
reads the combined frame.

## Steps (on the iPhone) — the widget

1. **Install Scriptable** — free, App Store. (`Request.loadJSON()` is not a browser → no
   CORS issue; it calls the relay directly.)
2. **Save the widget** — Scriptable → `+` → paste [`agentlamp-widget.js`](agentlamp-widget.js)
   → name it exactly `AgentLamp` → fill the three constants (`RELAY_URL`, `DEVICE_ID`, `TOKEN`).
3. **Run once** (▶) inside Scriptable → large preview shows live data (or a clean
   `OFFLINE` card — it degrades, never crashes).
4. **Add the widget** — long-press home screen → `+` → **Scriptable** → **large** → drop it
   → long-press → **Edit Widget** → *Script* = `AgentLamp`. Lock screen: add the Scriptable
   small/inline widget the same way.

## Multi-device — telling machines apart

The aggregated frame can carry agents from several machines. The disambiguator is
`primary.account`, sourced from `AGENTLAMP_ACCOUNT`. Give each machine a **distinct
neutral alias** (e.g. `studio`, `macbook`) so the phone can tell which one is in focus.
Fleet-row–level separation (every row tagged by machine) is a documented future option — see
the multi-device plan §3 / D-items.

## P2 — instant alerts

The widget only refreshes every 5–15 min, so a `WAITING`/`ERROR` can sit unseen. The alert
script fixes that:

1. **Save** [`agentlamp-alert.js`](agentlamp-alert.js) as a Scriptable script (it also needs
   `frame-view.js` saved as a Scriptable script named exactly `frame-view`). Fill the same
   three constants.
2. **Schedule it — mind iOS's limitation.** Stock Shortcuts "Time of Day" automations only repeat
   **Daily / Weekly / Monthly** — there is **no built-in "every 5 minutes" trigger**. For real
   sub-daily polling pick one:
   - **Pushcut Automation Server** (recommended): an always-on spare device (old iPad / a Mac) runs
     `agentlamp-alert` on a timer → true N-minute cadence.
   - **Worker-cron** (owner-gated, server side) — the note below; fires even with the phone off.
   - **Staggered Personal Automations** — several fixed-time daily triggers as a coarse approximation.

   The script is read-only + deduped: the same standing alert never re-notifies; a **changed task**
   or a genuinely new alert does. Latency = whatever interval you schedule (minutes, not seconds).
3. **Optional — fire even when the phone is locked:** install **Pushcut** (free), create a
   Webhook, paste its URL into `PUSHCUT_WEBHOOK` in the script (local notification + webhook are
   tried independently, so one failing won't suppress the other).

> Server-side alternative (owner-gated, not shipped): a Worker `scheduled` cron → Pushcut
> webhook fires even when the phone is fully off, but it touches the security-critical relay
> (invariants I1–I5) + needs a deploy + a stored secret. Documented in the 2026-06-06 plan §5.

## Verify (end-to-end)

- [ ] Logic conformance (on any machine): `node --test readers/iphone-widget/test/frame-view.test.cjs readers/iphone-widget/test/widget-template.test.cjs` → all green.
- [ ] Widget shows real data; `scene=sleep` is fine when nothing runs.
- [ ] Two machines with distinct `AGENTLAMP_ACCOUNT` → widget subtitle distinguishes them.
- [ ] Toggle Airplane Mode → flips to the offline cached state then back, never blank/crash.
- [ ] Start ≥2 active agents → fleet rows appear.
- [ ] P2: cause a `WAITING` → notification on the next alert run (≤ your interval); the same alert
      does not re-notify, but a **changed task** on the same agent does.
- [ ] Revoke drill (below) → next refresh shows **`PAIRING REQUIRED`** (NOT a stale cached frame) and
      the cache is dropped. Re-enroll → recovers.
- [ ] `grep` this folder for real values → none (placeholders only).

```sh
# Revoke (drill / lost phone) — strongly-consistent, immediate:
TS=$(date +%s); NONCE=$(python3 -c "import secrets;print(secrets.token_hex(16))")
curl -sS -X POST "$RELAY_URL/admin/devices/$DEVICE_ID/revoke" \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "X-ACO-Timestamp: $TS" -H "X-ACO-Nonce: $NONCE" \
  -H "content-type: application/json" -d '{}'
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Error: module 'frame-view' not found` | Only applies to `agentlamp-alert.js`; save `frame-view.js` as a Scriptable script named exactly `frame-view`. The widget itself no longer imports it. |
| Offline cached state | Last fetch failed; showing the last good frame. Transient — clears on next success. |
| `PAIRING REQUIRED · HTTP 401/403/404` | Token stale / revoked / unknown device. The widget **refuses** to show cached data here (a revoked phone must not keep rendering agent state) and drops the cache → re-run the enroll command above to restore. |
| `OFFLINE` with timeout (no cache yet) | Relay unreachable or wrong `RELAY_URL` → check on cellular + the URL. |
| Widget not refreshing | iOS throttle (5–15 min, longer on low battery / rarely viewed) — `refreshAfterDate` is a hint, not a guarantee. Add the P2 alert for instant pings. |
| Two machines are indistinguishable | Give each launchd daemon a distinct `AGENTLAMP_ACCOUNT`; `--collector-id` alone is not the phone label. |
| `HTTP 429` | Polling too fast; the medium-widget cadence is well under the 20/min device cap. |

---

*Reference impl + deploy guide. Real relay/device/admin values stay outside the repo.*
