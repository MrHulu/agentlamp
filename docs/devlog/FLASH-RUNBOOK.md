# AgentLamp — Flash & Bring-Up Runbook

The exact inline steps the orchestrator (Claude) runs to flash the firmware onto the real
Waveshare ESP32-S3-LCD-1.47B and bring the whole pipeline up. Every command and path below
is the **real** one confirmed from the build journals and the live tree.

**Confirmed facts (from devlog 01–03, re-verified against the tree):**

| Item | Value |
|------|-------|
| Project root | `/Users/hulu/huluman/agentlamp` |
| venv Python | `/Users/hulu/huluman/agentlamp/.venv/bin/python` |
| Board | Waveshare ESP32-S3-LCD-1.47B (ESP32-S3R8, 8 MB OPI PSRAM, 16 MB flash) |
| Serial port | `/dev/cu.usbmodem1101` |
| Monitor baud | `115200` |
| Laptop LAN IP (en0) | `192.168.1.148` |
| Server bind (default) | `0.0.0.0:8787` |
| Frame URL on device | `http://192.168.1.148:8787` |
| Dev device id / token | `orb-01` / `dev-local-token` (defaults already in `secrets.h`) |
| Firmware build env | `waveshare-s3-lcd-147` |

> **Pre-flight — confirm the LAN IP hasn't changed (DHCP):**
> ```bash
> ipconfig getifaddr en0    # expect 192.168.1.148; if different, use the new value in Step 1
> ```
> Also confirm the board is attached: `ls /dev/cu.usbmodem*` should list `/dev/cu.usbmodem1101`.
> If the port number differs (e.g. `usbmodem1401`), substitute it in every command below.

---

## Step 1 — Fill in `firmware/src/secrets.h` (real WiFi + LAN URL)

`secrets.h` is gitignored and currently holds placeholders. Edit it in place — replace the
three placeholder lines with real values. **`DEVICE_ID` and `DEVICE_TOKEN` already match the
server defaults; leave them unless you changed the server env.**

File: `/Users/hulu/huluman/agentlamp/firmware/src/secrets.h`

```c
#pragma once
#define WIFI_SSID "<YOUR_WIFI_SSID>"           // was "REPLACE_ME"
#define WIFI_PASS "<YOUR_WIFI_PASSWORD>"        // was "REPLACE_ME"
#define FRAME_BASE_URL "http://192.168.1.148:8787"   // was "http://LAPTOP_LAN_IP:8787"
#define DEVICE_ID "orb-01"
#define DEVICE_TOKEN "dev-local-token"
```

Hard rules:
- The laptop and the device **must be on the same LAN/subnet** (the `192.168.1.x` here).
- `FRAME_BASE_URL` must be **`http://`** (not `https://`) — the firmware hard-rejects
  `https://` until pinned-CA relay mode lands (review fix P1-7).
- No trailing slash on `FRAME_BASE_URL`. Port `8787` must match the server bind.

Verify it's still ignored (must print the path = ignored):
```bash
git -C /Users/hulu/huluman/agentlamp check-ignore firmware/src/secrets.h
```

---

## Step 2 — Start the local frame server

Run it from the `server/` directory (the package resolves on `sys.path` from there).
Default bind is `0.0.0.0:8787` so it's reachable from the device on the LAN.

```bash
cd /Users/hulu/huluman/agentlamp/server && \
/Users/hulu/huluman/agentlamp/.venv/bin/python -m agentlamp_server
```

Expected startup log (uvicorn):
```
INFO:     Started server process [...]
INFO:     Uvicorn running on http://0.0.0.0:8787 (Press CTRL+C to quit)
```

> Run this in the background (or a separate terminal) so it keeps serving while you flash and
> monitor. To keep the env explicit you may prefix `AGENTLAMP_PEPPER_HEX=<32-byte-hex>` (else
> a per-process random pepper is used — fine for local mode).

Sanity-check the server is up and reachable on the LAN IP **before** flashing. (If the shell
routes through an HTTP proxy, add `--noproxy '*'`.)

```bash
curl --noproxy '*' http://192.168.1.148:8787/healthz
# -> {"ok":true,"service":"agentlamp-frame-server","v":1}

# the device frame (Bearer; token never in the URL):
curl --noproxy '*' -H 'Authorization: Bearer dev-local-token' \
     -H 'X-Frame-Schema-Version: 1' \
     http://192.168.1.148:8787/api/v1/device/orb-01/frame
# -> 200, header x-frame-schema-version: 1, JSON frame (likely scene "sleep" when empty)
```

If `healthz` works on `127.0.0.1` but not on `192.168.1.148`, the laptop firewall is blocking
inbound 8787 — allow Python to accept incoming connections (macOS: System Settings → Network →
Firewall, or temporarily disable for the bring-up).

---

## Step 3 — Flash the firmware

The firmware already compiled (devlog 03; `firmware.bin` 689 KB present). Upload it to the
board. The board enumerates as native USB and esptool resets it automatically — **no
BOOT-button hold was needed** during the probe in devlog 01, so try a plain upload first.

```bash
/Users/hulu/huluman/agentlamp/.venv/bin/pio run \
  -d /Users/hulu/huluman/agentlamp/firmware \
  -t upload \
  --upload-port /dev/cu.usbmodem1101
```

Expected tail:
```
Writing at 0x... (100 %)
Wrote ... bytes ...
Hash of data verified.
Hard resetting via RTS pin...
========================= [SUCCESS] ...
```

If upload fails to connect (`A fatal error occurred: ... Failed to connect`):
1. Hold **BOOT**, tap **RESET**, release **BOOT** (forces download mode), then re-run.
2. Confirm nothing else holds the port — `pio device monitor` from a previous step must be
   stopped first (only one process can own `/dev/cu.usbmodem1101`).
3. Re-check the port name: `ls /dev/cu.usbmodem*`.

---

## Step 4 — Monitor serial + read the bring-up log

```bash
/Users/hulu/huluman/agentlamp/.venv/bin/pio device monitor \
  -p /dev/cu.usbmodem1101 -b 115200
```

(Exit the monitor with `Ctrl-]`.)

**Expected serial log lines** (these are the literal `Serial.print` strings from
`firmware/src/main.cpp`):

```
=== AgentLamp firmware ===
device_id      : orb-01
frame_base_url : http://192.168.1.148:8787
PSRAM size     : 8388608                 (8 MB — confirms OPI PSRAM is on)
free heap      : <a few hundred KB>
wifi           : joining <YOUR_WIFI_SSID>
wifi           : connected, ip=192.168.1.xxx
wifi rssi      : -<NN>
frame ok       : scene=<N> seq=<S> ttl=<T>      (repeats every ~4 s)
```

Decode the `scene=<N>` integer (enum order in `theme.h`):

| N | Scene | N | Scene |
|---|-------|---|-------|
| 0 | BOOT | 6 | OFFLINE |
| 1 | PAIRING | 7 | STALE |
| 2 | FLEET | 8 | DIAGNOSTICS |
| 3 | FOCUS | 9 | SLEEP |
| 4 | QUOTA | 10 | WIFICONFIG |
| 5 | ALERT | | |

With **no state injected yet**, expect a steady `frame ok : scene=9 ...` (SLEEP).

**Other lines you may see (and what they mean):**
- `wifi : creds are REPLACE_ME -> WiFiConfig scene` — Step 1 not done; fix `secrets.h`, reflash.
- `wifi : JOIN FAILED -> WiFiConfig scene` — wrong SSID/PASS or out of range.
- `frame err : https relay needs pinned CA (unimplemented) -> refusing` — `FRAME_BASE_URL` is
  `https://`; it must be `http://` (Step 1).
- `frame err : http 401 -> PAIRING REQUIRED` / `http 403/404` — token/device mismatch; the
  device latches into the PAIRING-REQUIRED diagnostics scene and stops polling. Make sure the
  server's `DEVICE_ID`/`DEVICE_TOKEN` (defaults `orb-01`/`dev-local-token`) match `secrets.h`.
- `frame fail : code=<C> fails=<F>` — transport/oversize/bad-JSON/429/503; after `fails` hits
  **3** the screen flips to OFFLINE (cached frame retained).

**The token must never appear in the serial log** — only `scene/seq/ttl/http-code` summaries.
If you ever see the token printed, that's a regression.

---

## Step 5 — Physical verification checklist

With the monitor open and the server running, drive the device through its scenes by POSTing
to `/admin/event` (and `/admin/quota`) on the server, then watch the **screen** and **RGB LED**
change. The device polls every ~4 s, so allow a few seconds per step. Use `--noproxy '*'` if a
proxy is in the shell.

First, confirm the idle baseline:

| Inject | Screen should show | RGB LED |
|--------|-------------------|---------|
| *(nothing — fresh start)* | **SLEEP**: dim ambient idle screen | very dim blue |

### 5a — FOCUS (a live coding session)

```bash
curl --noproxy '*' -X POST http://192.168.1.148:8787/admin/event \
  -H 'Content-Type: application/json' \
  -d '{"provider":"claude","account":"work","status":"CODING","project":"project-a","task":"coding"}'
```
Expect: **FOCUS** scene — kicker `Claude work`, dominant word **CODING**, `project-a` + task,
`seq` footer. **LED = purple** (coding). Serial: `frame ok : scene=3 ...`.

### 5b — ALERT (waiting interrupt — the headline case)

```bash
curl --noproxy '*' -X POST http://192.168.1.148:8787/admin/event \
  -H 'Content-Type: application/json' \
  -d '{"provider":"codex","account":"main","status":"WAITING","project":"project-a","task":"waiting"}'
```
Expect: scene flips to **ALERT** — big coloured ring + `!` glyph, dominant **WAITING**,
`ACTION REQUIRED` headline footer. **LED = amber/yellow**. Serial: `frame ok : scene=5 ...`.
(WAITING +100 interrupts the CODING focus — this is the whole point of the orb.)

### 5c — ERROR alert

```bash
curl --noproxy '*' -X POST http://192.168.1.148:8787/admin/event \
  -H 'Content-Type: application/json' \
  -d '{"provider":"claude","account":"work","status":"ERROR","project":"project-a","task":"error"}'
```
Expect: **ALERT** with `x` glyph, dominant **ERROR**, **LED = red**.

### 5d — QUOTA danger

```bash
curl --noproxy '*' -X POST http://192.168.1.148:8787/admin/quota \
  -H 'Content-Type: application/json' \
  -d '{"provider":"codex","account":"main","window_type":"5h","used_ratio":0.95,"confidence":"high"}'
```
Expect: a **red** quota-danger alert (used_ratio ≥ 0.90 forces the alert + red accent). The
QUOTA scene itself draws horizontal bars (red ≥ 70 % / amber ≥ 40 % / teal else) with a `%` +
`est` tag. **LED = red**.

### 5e — FLEET (multiple sessions, none alerting)

```bash
# reset, then add several non-alerting sessions
curl --noproxy '*' -X POST http://192.168.1.148:8787/admin/reset
curl --noproxy '*' -X POST http://192.168.1.148:8787/admin/event -H 'Content-Type: application/json' \
  -d '{"provider":"claude","account":"work","status":"CODING","project":"project-a","task":"coding"}'
curl --noproxy '*' -X POST http://192.168.1.148:8787/admin/event -H 'Content-Type: application/json' \
  -d '{"provider":"codex","account":"main","status":"THINKING","project":"project-b","task":"thinking"}'
curl --noproxy '*' -X POST http://192.168.1.148:8787/admin/event -H 'Content-Type: application/json' \
  -d '{"provider":"claude","account":"alt","status":"READING","project":"project-c","task":"reading"}'
```
Expect the device on FOCUS for the top-priority session; the **FLEET** view lists rows
`provider …… status-tag` with each tag in its own colour + a `N active +M more` footer. LED
follows the focused session's status (purple/cyan/etc.).

### 5f — OFFLINE (server unreachable)

Stop the server (Ctrl-C in Step 2's terminal, or kill it). Within ~12 s (3 failed polls at
4 s) the device should show:

Expect: **OFFLINE** — dominant **OFFLINE** in grey (no glow), "frame source unreachable",
"last seen Nm ago" (this counter should **advance** — review fix P1-4). **LED = dim grey**.
Serial: `frame fail : code=... fails=1/2/3` then `scene=6`. Restart the server → it recovers
back to the live scene on the next successful poll.

### 5g — Sanitizer reject (the trust claim, end-to-end)

Prove a raw-path leak is rejected by the server before it ever reaches the device:
```bash
curl --noproxy '*' -X POST http://192.168.1.148:8787/admin/event \
  -H 'Content-Type: application/json' \
  -d '{"provider":"claude","account":"work","status":"CODING","project":"/Users/hulu/secret-repo"}'
# -> 422 {"rejected":true,"reason":"forbidden:/Users/","payload_hash":"..."}  (no leaked value)
```
The device frame is unchanged (the event was rejected at the trust boundary). This is the
default-deny sanitizer doing its job.

### Pass criteria

- Screen lights up (backlight on — confirms GPIO48 is right, not the stale-doc 46) and shows
  the boot screen, then SLEEP.
- Each injected state above changes **both** the screen scene **and** the RGB LED colour as
  listed, within ~1 poll interval (~4 s).
- Colours are **not photo-negative**. If reds/blues look swapped or the whole image is
  inverted, flip `cfg.invert` in `firmware/src/display.h` (review note P1-5) and reflash.
- OFFLINE's "last seen" timer advances (not frozen) when the server is down.
- The token never appears in the serial log.

> Optional cross-check: open the live simulator on the laptop —
> `open http://192.168.1.148:8787/preview` — it renders the **exact same frame JSON** the
> device sees, side-by-side with the device's screen.

---

## Quick reference — one-screen recap

```bash
# 0. confirm IP + port
ipconfig getifaddr en0          # 192.168.1.148
ls /dev/cu.usbmodem*            # /dev/cu.usbmodem1101

# 1. edit firmware/src/secrets.h: WIFI_SSID, WIFI_PASS, FRAME_BASE_URL=http://192.168.1.148:8787

# 2. start server (keep running)
cd /Users/hulu/huluman/agentlamp/server && \
/Users/hulu/huluman/agentlamp/.venv/bin/python -m agentlamp_server

# 3. flash
/Users/hulu/huluman/agentlamp/.venv/bin/pio run -d /Users/hulu/huluman/agentlamp/firmware \
  -t upload --upload-port /dev/cu.usbmodem1101

# 4. monitor
/Users/hulu/huluman/agentlamp/.venv/bin/pio device monitor -p /dev/cu.usbmodem1101 -b 115200

# 5. drive scenes (see Step 5) via POST /admin/event and /admin/quota on :8787
```
