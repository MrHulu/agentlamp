# 05 — Hardware Bring-Up Journal (real ESP32-S3-LCD-1.47B)

> Date: 2026-05-30 → 05-31. The bench build (devlog 01–03) compiled and the server
> ran, but nothing had touched the real board. This journal is the actual on-hardware
> bring-up: flash → debug → fix → verify, end to end, with the operator (Hulu) reading
> the physical screen + LED since the orchestrator can't see them. Outcome: **AgentLamp
> runs end-to-end on the real board — live agent status on the LCD + RGB, verified.**

## Method

Tight loop: flash a change → capture serial via a pyserial reader (`/tmp/cap_serial.py`,
the native-USB CDC port `/dev/cu.usbmodem1101` re-enumerates after each reset, so the
reader waits for it) → ask the operator what the screen/LED actually showed → diagnose
→ fix → reflash. Hardware-specific facts were proven on the board, never assumed.

## The bugs found and fixed on hardware

### 1. Screen totally dark — backlight pin was wrong (GPIO48 → **GPIO46**)
First flash: firmware ran (serial healthy, PSRAM 8 MB up, chose WiFiConfig), but the
**screen was completely dark** — even a raw `digitalWrite(BL,HIGH)` bypassing LovyanGFX
didn't light it, at HIGH or LOW. So GPIO48 (from the espp/non-B docs + an earlier review's
"46→48 correction") was **not** the backlight on this -1.47**B** board.
- **Diagnostic:** drove 21 safe candidate GPIOs HIGH at once (excluding flash/PSRAM 26-37,
  USB 19/20, the LCD SPI pins, the RGB LED) + a white panel fill → screen lit white with
  "SWEEP" text → so the backlight IS a reachable pin AND the ST7789/SPI/offset are all
  correct. Then a per-pin cycle that drew "PIN <N>" while powering each candidate alone —
  the screen lit showing **"PIN 46"**.
- **Fix:** `display.h` `cfg.pin_bl = 46`; `platformio.ini` `PIN_LCD_BL=46`. The earlier
  review's 46→48 "correction" was the actual bug (non-B docs don't match the B board).
- **Lesson:** for a board variant, sweep the real hardware; don't trust same-family docs.

### 2. Fonts unreadable, then overflowing — `drawFit()` auto-shrink
Helper text was too small (operator: "join barely readable"); a first bump over-corrected
and **clipped** long strings off-screen ("join AgentLamp-Setup" at 12pt bold ≈ 260 px on a
172 px screen). Root cause: hard-coded font sizes ignore string length × glyph width.
- **Fix:** every text line now renders through `renderer.h drawFit()` — it measures
  `textWidth()` down a font ladder and picks the **largest font that fits the usable width**,
  so any string (long status words like "THINKING", long AP names) auto-shrinks and never
  clips, staying as large as possible. The provisioning scene was also restructured into
  short, big two-step lines.

### 3. One transient WiFi join failure dropped to the portal — retry hardening
After a reflash the device read its NVS creds, tried to join once (15 s), hit a transient
timeout, and fell straight to the captive portal showing "wifi failed". A `RESET`-button
press reproduced it. Known-good creds must not be abandoned on a single blip.
- **Fix:** `main.cpp` boot join now retries **5×** (showing "CONNECTING · retry n/5",
  `WiFi.disconnect()` between) before assuming the creds are wrong and re-entering the
  portal. Confirmed: a failed attempt 1/5 now recovers instead of dropping out.

### 4. RGB LED colours scrambled — WS2812 is **RGB, not GRB**
Status colours were wrong on the LED (amber showed green, red showed green). Derived from
clean data (the value 255 always landing in the green channel) that R and G were swapped.
- **Fix:** `led.h` `NEO_GRB` → `NEO_RGB`. Verified on hardware: amber→amber, red→red.

### 5. LEDs looked pale/washed-out — separate **vivid LED palette**
After the order fix the hues were right but every colour looked pale, and brightness
25%→63% made no visible difference. Cause: the LCD palette is intentionally *soft*
(e.g. ERROR = `#ff5470`, a rose, not pure red); on a bare WS2812 point source those soft
colours read as washed-out regardless of brightness.
- **Fix:** `theme.h` gained `ledStatusColor()` / `ledAccentColor()` — a **saturated**
  palette (near-pure channels: red `255,0,0`, amber `255,150,0`, purple `185,0,255`,
  teal-green `0,255,110`, …) used to drive the LED only; the LCD keeps its soft design
  colours. Brightness raised 64→160. Result: vivid, distinguishable glow.

### 6. Quota-danger alert read "IDLE" in red, indistinguishable from ERROR
A quota-≥90 % alert arrives as `scene=alert, status=IDLE, accent=red`, so the device drew
the big word "IDLE" in a red ring (operator: "a red IDLE") and lit the LED **blue**
(`ledForStatus(IDLE)`), and even once corrected it was the same red as ERROR.
- **Fix:** the alert scene now (a) shows **"QUOTA"** as the dominant word when the status
  isn't WAITING/ERROR, and (b) distinguishes the three alert types by hue on both the
  ring/word and the LED — **WAITING = amber, ERROR = red, QUOTA = orange**.

### Also caught (operational, not firmware)
- The scene-driving tour scripts using `urllib` silently failed: the host's Clash proxy
  (`http_proxy=127.0.0.1:7897`) captured even LAN requests to `192.168.1.148`. Fixed by
  giving the *script* a no-proxy opener (`ProxyHandler({})`) — **the system proxy was never
  touched** (Boss death-command). `curl` already used `--noproxy '*'`.

## Final verified state

| Thing | State |
|-------|-------|
| Board | Waveshare ESP32-S3-LCD-1.47B, MAC `44:1b:f6:86:59:68`, 8 MB PSRAM, port `/dev/cu.usbmodem1101` |
| WiFi | provisioned at runtime via SoftAP portal (no creds in repo); robust 5× retry reconnect |
| Pipeline | ESP32 → WiFi → `http://192.168.1.148:8787` local frame server → JSON → render. Live, ~4 s poll. |
| Screen | backlight GPIO46, ST7789 172×320, centered (offset 34), colours correct, fonts auto-fit (no clipping) |
| LED | RGB order, vivid saturated palette, brightness ~63%; WAITING amber / ERROR red / QUOTA orange / CODING purple / etc. |
| Scenes verified | Boot, WiFiConfig/portal, Focus(live status), Alert(waiting/error/quota), Quota, Offline, Stale, Sleep |
| **Operator verdict** | **"没问题了" — all good. Test verification PASSED.** |

## Open / next

- Real provider adapters (Codex/Claude hooks → collector) — TASK-005; today states are
  injected manually via `/admin/event`.
- DONE status currently transitions straight to the Sleep scene (no lingering "DONE"); a
  brief DONE display is a possible polish.
- NTP wall clock (top bar shows uptime mm:ss placeholder).
- Nothing committed yet (awaiting Boss `/archive`); the device depends on the laptop server
  running for now.
- The board files (`server/`, `firmware/`, `docs/devlog/`) are uncommitted on branch `main`.
