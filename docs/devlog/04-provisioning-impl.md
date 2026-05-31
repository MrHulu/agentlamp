# Devlog 04 — Runtime WiFi Provisioning (SoftAP captive portal) + Typography

**Date:** 2026-05-30
**Agent role:** Firmware implementer (AgentLamp)
**Goal:** Two firmware changes on the Waveshare ESP32-S3-LCD-1.47B:
1. Replace compile-time WiFi credentials with a **runtime SoftAP captive-portal**
   provisioning flow. The repo must contain **ZERO** WiFi credentials.
2. Fix **typography** — text was too small to read at desk distance (~40 cm), especially
   the WiFiConfig helper/sub lines.
**Outcome:** SUCCESS — `pio run` builds cleanly (0 errors, 0 warnings). RAM **15.6%**
(51 140 B / 327 680 B), Flash **15.9%** (1 039 413 B / 6 553 600 B). No WiFi credential lives
in any source file. **Not flashed, not committed.**

> Builds on Devlog 03 (the WiFi-join → poll → render pipeline). The compile-time
> `secrets.h` (`WIFI_SSID` / `WIFI_PASS` placeholders) is **deleted**; creds now live only in
> NVS, entered through a phone browser on first boot.

---

## 1. Why this change

The device shipped with `secrets.h` holding `WIFI_SSID "REPLACE_ME"` / `WIFI_PASS "REPLACE_ME"`.
That means:
- Every owner must re-flash to set their WiFi → no field provisioning.
- Worse, anyone who fills in real creds and commits leaks them. This is an **open-source**
  project; a WiFi SSID/password must **never** live in any source file — not even a
  gitignored one (a gitignored file still tempts a `git add -f` or shows up in a tarball).

The firmware contract (`docs/firmware/firmware_contract.md` §WiFi Provisioning) already
specified **SoftAP captive portal (recommended)** with creds stored in NVS. This devlog
implements exactly that.

---

## 2. Files changed

| File | Change |
|------|--------|
| `firmware/src/secrets.h` | **DELETED** — held the WiFi placeholders. |
| `firmware/src/config.h` | **NEW** — non-secret committed defaults: `FRAME_BASE_URL` (`http://192.168.1.148:8787`), `DEVICE_ID` (`orb-01`), `DEVICE_TOKEN` (`dev-local-token`). No WiFi creds. |
| `firmware/src/provisioning.h` | **NEW** — `Provisioning` class: NVS read/write (Preferences namespace `agentlamp`), SoftAP, DNSServer captive portal, WebServer form. |
| `firmware/src/main.cpp` | Rewired WiFi flow: load creds from NVS → join or enter portal; provisioning state machine in `loop()`; BOOT-button re-provision; runtime server URL. |
| `firmware/src/renderer.h` | Typography bump across **every** scene (Task 2). |
| `firmware/platformio.ini` | Unchanged — `WebServer` / `DNSServer` / `Preferences` are Arduino-ESP32 core built-ins (no new `lib_deps`). |
| `docs/devlog/04-provisioning-impl.md` | This file. |

**Zero new PlatformIO dependencies.** Built-in `WebServer.h` (not an async lib, per the
brief — avoids adding `ESPAsyncWebServer`), `DNSServer.h`, `Preferences.h`, `WiFi.h`.

---

## 3. Provisioning state machine

```
                       ┌─────────────────────────────────────────────┐
   power on            │ setup():                                    │
       │               │  pinMode(GPIO0, INPUT_PULLUP) // BOOT btn    │
       ▼               │  creds = Provisioning::loadCreds()  // NVS   │
  ┌──────────┐         │  frameBaseUrl = creds.server (or FRAME_BASE) │
  │  boot()  │         └─────────────────────────────────────────────┘
  │  scene   │                         │
  └──────────┘            creds.hasWifi?│
                          ┌─────────────┴─────────────┐
                       NO │                            │ YES
                          ▼                            ▼
                  ┌───────────────┐            ┌────────────────┐
                  │ enterPortal() │            │ wifiConnect()  │
                  │  SoftAP+DNS+  │            │  (15s timeout) │
                  │  HTTP, SETUP  │            └────────────────┘
                  │  scene latched│              join ok? │
                  └───────────────┘          ┌────────────┴──────────┐
                          │               YES │                       │ NO
                          │                   ▼                       ▼
            ┌─────────────┴────────────┐  ┌────────────┐     ┌───────────────┐
            │ loop(): provisioningHalt │  │ POLL LOOP  │     │ enterPortal() │
            │  prov.service():         │  │ (devlog 03)│     │  (re-enter)   │
            │   dns.processNextRequest │  └────────────┘     └───────────────┘
            │   server.handleClient    │
            └─────────────┬────────────┘
                   user POSTs /save? │
                          ▼ YES
            ┌──────────────────────────┐
            │ reload creds from NVS     │
            │ endPortal()               │
            │ wifiConnect()             │
            │  ok → poll loop           │
            │  fail → enterPortal again │
            └──────────────────────────┘

  ANY state: BOOT (GPIO0) held LOW ≥3 s → clearCreds() (wipe NVS ssid/pass) → ESP.restart()
```

### Key behaviours

- **`provisioningHalt` latch preserved.** As in devlog 03, while the portal is up the main
  `loop()` returns early and never calls `renderCurrent()`, so the SETUP scene (the AP name +
  `192.168.4.1` the user needs) is not repainted over by `effectiveScene()` returning `BOOT`.
- **But the servers still run.** The latch branch now calls `prov.service()` every iteration,
  which pumps `dns.processNextRequest()` + `server.handleClient()` — so the captive portal is
  reachable *while* the SETUP scene holds. `delay(5)` keeps DNS/HTTP responsive.
- **NVS namespace `agentlamp`**, keys: `ssid`, `pass`, `server`. `loadCreds()` falls back to
  the compile-time `FRAME_BASE_URL` when `server` is unset.
- **AP:** `WiFi.mode(WIFI_AP_STA)` + `WiFi.softAP("AgentLamp-Setup", "agentlamp")`. I chose a
  **fixed WPA2 password `agentlamp`** (≥8 chars, the WPA2 minimum) over an open AP, so the
  setup network isn't trivially joinable by every device that scans for open APs. The AP IP is
  the ESP32 default `192.168.4.1`.
- **DNS captive portal:** `DNSServer` on port 53 with wildcard host `*` → `192.168.4.1`, so any
  hostname the phone tries (captive-portal probes included) resolves to the orb and pops the
  form. Convenience routes for the OS probe URLs (`/generate_204` Android, `/hotspot-detect.html`
  iOS/macOS) and a catch-all `onNotFound` all redirect to the form.

---

## 4. The captive-portal web form

`GET /` serves a single self-contained mobile HTML page (no external CSS/JS — the phone has no
internet while on the AP). Dark theme matching the orb, big tap targets, fields:

| Field | Type | Default |
|-------|------|---------|
| WiFi network | text (required) | — |
| WiFi password | password | — |
| Server URL | text | pre-filled `http://192.168.1.148:8787` (or last-saved from NVS) |

`POST /save` → `Provisioning::saveCreds()` writes ssid/pass/server to NVS, responds
`Saved ✓ — AgentLamp is connecting to <ssid>. You can close this page.`, sets a `_saved` flag.
The next `loop()` iteration reloads creds, tears the portal down, and attempts the join. If the
join fails (wrong password), the portal relaunches so the user can retry — no re-flash needed.

The user-typed SSID is HTML-attribute-escaped before being echoed back into the page
(`htmlEscape()` covers `& < > ' "`), so an SSID containing markup can't break the page or inject.

---

## 5. BOOT-button re-provisioning

`checkReprovisionButton()` runs at the top of every `loop()` iteration (whether polling or
parked in the portal):

- GPIO0 is the onboard **BOOT** button, read with `INPUT_PULLUP` (pressed = LOW).
- Hold LOW for **≥3 s** → `Provisioning::clearCreds()` removes the NVS `ssid`/`pass` keys (keeps
  `server` so the next portal pre-fills the last server URL) → `ESP.restart()`.
- On reboot, `loadCreds()` finds no SSID → boots straight into the portal.
- Released before 3 s → the hold timer resets (no accidental wipe from a tap; tapping BOOT alone
  does nothing destructive).

This is the contract's "hold a button N seconds → clear NVS WiFi creds → re-enter portal".

---

## 6. Typography (Task 2)

Boss feedback: text is too small to read on the 172×320 at desk distance; on the WiFiConfig
scene the `SETUP` title is OK-ish but the helper/sub lines ("join AgentLamp-XXXX" etc.) are
too small. Mobile-typography floor: dominant word stays large, secondary lines must be
comfortably readable at ~40 cm (no tiny text).

LovyanGFX font heights (cap height, approx): `Font2` ≈ 14 px, `Font4` ≈ 26 px,
`FreeSans9pt7b` ≈ 18 px, `FreeSansBold12pt7b` ≈ 24 px, `FreeSansBold18pt7b` ≈ 36 px,
`FreeSansBold24pt7b` ≈ 48 px. The fix moves nearly every sub/helper/footer line off `Font2`.

| Scene / element | Before | After | Why |
|-----------------|--------|-------|-----|
| `topBar` (provider · clock) | `Font2` ~14 px | `Font4` ~26 px | top bar was squint-small |
| `bottom` (footer line) | `Font2` ~14 px | `Font4` ~26 px | "last seen 3m ago" etc. |
| `statusWordBig` dominant | 18 pt (≤5 ch), 12 pt (≥8 ch) | **24 pt** (≤5 ch), 18 pt (≥6 ch) | headline reads across the room; long words fit 172 w at 18 pt |
| **`wifiConfig` helper** (AP name) | `Font2` ~14 px | **`FreeSansBold12pt7b` ~24 px** | **Boss's specific complaint — now readable** |
| **`wifiConfig` footer** (`192.168.4.1`) | `Font2` ~14 px | `FreeSans9pt7b` ~18 px, accent colour | portal address pops |
| `wifiConfig` code (`SETUP`) | `FreeMonoBold18pt7b` | `FreeSansBold24pt7b` | bigger dominant word |
| `boot` "AgentLamp" | `FreeSansBold12pt7b` | `FreeSansBold18pt7b` | wordmark |
| `boot` "starting vX" | `Font2` | `FreeSans9pt7b` | |
| `focus` kicker / project / task | `Font2` / `Font4` / `Font2` | `FreeSans9pt7b` / `FreeSansBold12pt7b` / `FreeSans9pt7b` | |
| `fleet` rows | `Font2`, step 34 px | `Font4`, step 40 px | rows readable, still 5 fit |
| `quota` label / pct | `Font2` | `Font4`, bars taller (8→10 px), step 80→100 | |
| `alert` meta / sub | `Font2` | `FreeSansBold12pt7b` / `FreeSans9pt7b` | |
| `offline` / `stale` sub | `Font2` | `FreeSans9pt7b` | |
| `message` (PAIRING) l1/l2 | `Font2` | `FreeSans9pt7b` | |

**Bounds check.** All layouts stay centred within 172×320:
- `statusWordBig`: ≤5-char words at 24 pt FreeSansBold ≈ ≤120 px wide; 6+ char words drop to
  18 pt so "THINKING" (8 ch) ≈ 136 px < 172. No horizontal clip.
- `fleet`: 5 rows from y=78 step 40 → last baseline y=238, +~13 px descent ≈ 251, clears the
  bottom() summary at y=320−12. No vertical clip.
- `quota`: 2 bars from y=96 step 100 → second pct text bottoms out ≈ 260 < 308. Fits.
- The **provisioning portal scene** clearly shows the AP name (`join AgentLamp-Setup`, 24 px
  bold ink) + `browse 192.168.4.1` (18 px accent) at a readable size — the central goal.

The new SETUP scene wording set by `main.cpp`:
- title `"connect wifi"` (or `"wifi failed"` after a failed join)
- code `"SETUP"`
- helper `"join AgentLamp-Setup"`
- footer `"browse 192.168.4.1"`
- fixed bottom line `"then enter your wifi"`

---

## 7. Compile result (the gate)

```
$ /Users/hulu/huluman/agentlamp/.venv/bin/pio run \
    -d /Users/hulu/huluman/agentlamp/firmware -e waveshare-s3-lcd-147
...
RAM:   [==        ]  15.6% (used 51140 bytes from 327680 bytes)
Flash: [==        ]  15.9% (used 1039413 bytes from 6553600 bytes)
========================= [SUCCESS] Took 11.38 seconds =========================
```

0 errors, 0 warnings. (Baseline before this change was RAM 8.5% / Flash 10.5%; the +7 % RAM /
+5 % Flash is the WebServer + DNSServer + Preferences stack and the larger embedded fonts — well
within the board's budget.)

### Credential-proof grep (the second gate)

```
$ grep -rEi 'HULU|asd5607093|WIFI_PASS|WIFI_SSID' firmware/ --exclude-dir=.pio \
    | grep -v 'Preferences\|nvs\|prefs'
(no matches — clean)
```

No real WiFi credential in any tracked source. (`.pio/` is gitignored build output; its only
incidental hits are a "HuLU" substring inside a LovyanGFX binary font blob and ESP32 framework
library file paths — neither is in the repo.) `firmware/src/secrets.h` is deleted. The remaining
`ssid`/`password` mentions in source are comments, the orb's **own** AP name `AgentLamp-Setup`,
its fixed AP password `agentlamp`, and NVS read/write code — none is the user's WiFi credential.

**Not flashed** (board at `/dev/cu.usbmodem1101` untouched). **Not committed.**

---

## 8. How the Boss uses the portal

First boot (or after a BOOT-button reset), the orb shows the **SETUP** scene:

1. On your **phone**, open WiFi settings and join the network **`AgentLamp-Setup`**
   (password: **`agentlamp`**).
2. A **captive-portal page pops automatically** (iOS/Android detect it). If it doesn't, open a
   browser and go to **`http://192.168.4.1`**.
3. Fill the form: **WiFi network** (your home SSID), **WiFi password**, and **Server URL**
   (pre-filled with the default frame server — change only if your laptop's LAN IP differs).
4. Tap **Save & Connect**. The page shows "Saved ✓" and the orb leaves the AP, joins your WiFi,
   and starts polling. The LCD goes from SETUP → BOOT → the live focus scene.
5. If the WiFi password was wrong, the orb relaunches the `AgentLamp-Setup` portal so you can
   retry — **no re-flash ever needed.**

**To re-provision** (move the orb to a new network): hold the **BOOT** button for **3 seconds**.
The orb wipes its saved WiFi and reboots into the `AgentLamp-Setup` portal.

---

## 9. Risks / follow-ups

- **AP password is a fixed default** (`agentlamp`). Anyone in RF range during the brief setup
  window could join the setup AP and read the form. For a desk orb this is low-risk and the
  industry norm (many consumer devices ship an open setup AP); a per-device random AP password
  printed on a sticker is a future hardening step, not needed for v1.
- **NVS creds are stored unencrypted** in the default NVS partition (standard ESP32 behaviour).
  NVS encryption (flash-encryption-backed) is a later hardening option; out of scope for v1.
- **Server URL is plaintext `http://`** by design (local LAN mode). The existing relay-TLS guard
  in `fetchFrame()` (devlog 03) still rejects `https://` until a pinned CA lands, so a user who
  types an `https://` server URL will see transport failures — acceptable until relay mode ships.
- **Not yet validated on hardware** — compile-only per the brief. A bench test should confirm:
  the AP appears, the captive portal pops on a phone, a real SSID/pass joins, and the BOOT-hold
  reset works. The state machine and APIs are standard Arduino-ESP32, so the risk is low, but
  the contract's "Firmware Acceptance" line ("First-boot provisioning portal stores creds in NVS;
  re-provisioning clears them") is verified by code/compile here, not yet on-device.
