# Devlog 03 — ESP32-S3 Firmware (LCD + RGB LED + frame poll pipeline)

**Date:** 2026-05-30
**Agent role:** Firmware implementer (AgentLamp)
**Goal:** Build the PlatformIO firmware for the Waveshare ESP32-S3-LCD-1.47B that closes
the pipeline end-to-end: WiFi join → poll the device frame API every ~4s with a Bearer
token → validate (size / schema / unknown-field) → render the design-board scenes on the
ST7789 172×320 → drive the onboard WS2812 to the status accent. Compile cleanly with the
project toolchain. Do NOT flash. Do NOT commit.
**Outcome:** SUCCESS — `pio run` builds cleanly on the **first attempt** (0 compile errors,
0 compile warnings); `firmware.bin` (689 KB) produced. RAM 7.9% / Flash 10.5%. Not flashed,
not committed.

> Builds on Devlog 01 (toolchain + scaffold) and Devlog 02 (local frame server). The
> scaffold `main.cpp` (serial banner only) and scaffold `platformio.ini` (libs commented
> out, BL pin wrong) are replaced here by the real board + network + render modules.

---

## 1. Target hardware (validated, not guessed)

| Item | Value |
|------|-------|
| Board | Waveshare ESP32-S3-LCD-1.47**B** (non-touch) |
| MCU | ESP32-S3R8 — 8MB OPI PSRAM, 16MB flash, native USB (CDC-on-boot) |
| LCD | ST7789, 172×320 IPS, SPI |
| RGB LED | 1× WS2812 (NeoPixel), GPIO38 |
| Serial port | `/dev/cu.usbmodem1101` (devlog 01) |

The **B** (non-touch) variant uses an **ST7789** driver. (The *Touch* variant
ESP32-S3-Touch-LCD-1.47 is a different board with a **JD9853** driver + AXS5106L touch — do
not copy its config.) Confirmed by cross-checking three independent sources below.

---

## 2. Pin map + panel config — where the numbers came from

Per the golden rule (search before build), I did not invent any pin/offset. I pulled the
panel config from a **proven working LovyanGFX repo** and cross-checked the pin map against
the **espp board-support package** and **TFT_eSPI** community configs:

| Source | What it gave |
|--------|-------------|
| [ahmadrezarazian/Waveshare_ESP32-S3-LCD1.47_3D-Box](https://github.com/ahmadrezarazian/Waveshare_ESP32-S3-LCD1.47_3D-Box) (LovyanGFX `Panel_ST7789`) | Full LGFX panel config **verbatim** — offsets, invert, rgb_order, SPI host/freq |
| [esp-cpp/espp `ws-s3-lcd-1-47`](https://github.com/esp-cpp/espp) BSP header | Independent pin-map confirmation + **RGB LED = GPIO38** |
| [Bodmer/TFT_eSPI discussion #3527](https://github.com/Bodmer/TFT_eSPI/discussions/3527) | Confirms ST7789 + **BGR** color order + backlight = **48 (active-high)** + the HSPI-port requirement |

All three agree on the SPI pins. The scaffold had **BL=46 which is wrong** — every source
says **48**; 46 would leave the backlight off and the screen dark. Fixed in `platformio.ini`.

### Final pin map

| Signal | GPIO | Note |
|--------|------|------|
| LCD MOSI | 45 | |
| LCD SCLK | 40 | |
| LCD CS | 42 | |
| LCD DC | 41 | |
| LCD RST | 39 | |
| LCD BL | **48** | active-high PWM (was 46 in scaffold — corrected) |
| RGB LED | 38 | WS2812 / NeoPixel, single pixel |

### The offsets — and WHY they are mandatory

The ST7789 controller has a **240×320** GRAM, but this glass only exposes a **172**-wide
window centred inside it. The LovyanGFX panel config therefore needs a **column offset of
34** (`(240 − 172) / 2 = 34`). Without it the image is shifted left and the right 34-px
column is clipped / wraps around. Values copied verbatim from the reference repo:

```cpp
cfg.memory_width  = 320;   // controller native (landscape) memory
cfg.memory_height = 172;
cfg.panel_width   = 172;   // visible portrait panel
cfg.panel_height  = 320;
cfg.offset_x      = 34;    // <-- mandatory COLUMN OFFSET
cfg.offset_y      = 0;
cfg.offset_rotation = 0;
cfg.invert      = true;    // ST7789 on this board needs inversion ON
cfg.rgb_order   = false;   // false = BGR (or reds and blues swap)
```

SPI bus: `SPI3_HOST` (this is the "USE_HSPI_PORT" requirement other libraries hit on this
board), `freq_write = 80 MHz`, `spi_mode = 0`. (`memory_width/height` look "swapped" vs the
172×320 glass because the ST7789 GRAM is addressed landscape; LovyanGFX rotates to portrait
via `panel_width/height` + `offset_rotation=0`. This is exactly the reference's layout.)

---

## 3. platformio.ini rationale

```ini
[env:waveshare-s3-lcd-147]
platform = espressif32
board = esp32-s3-devkitc-1        ; generic S3 base; flash/PSRAM overridden below
framework = arduino
build_flags =
    -D ARDUINO_USB_MODE=1          ; native USB
    -D ARDUINO_USB_CDC_ON_BOOT=1   ; Serial over /dev/cu.usbmodem*
    -D BOARD_HAS_PSRAM             ; 8MB OPI PSRAM
    -D LCD_WIDTH=172  -D LCD_HEIGHT=320
    -D PIN_LCD_MOSI=45 ... -D PIN_LCD_BL=48 -D PIN_RGB_LED=38
board_build.arduino.memory_type = qio_opi   ; OPI PSRAM (R8 part)
board_upload.flash_size = 16MB
board_build.flash_mode = qio
board_build.partitions = default_16MB.csv
monitor_speed = 115200
upload_port  = /dev/cu.usbmodem1101
monitor_port = /dev/cu.usbmodem1101
lib_deps =
    bblanchon/ArduinoJson @ ^7.0.0
    lovyan03/LovyanGFX @ ^1.1.16
    adafruit/Adafruit NeoPixel @ ^1.12.0
```

Decisions:

- **`board = esp32-s3-devkitc-1` + overrides** rather than a Waveshare-specific board JSON:
  the module is electrically a plain ESP32-S3R8, and PlatformIO has no first-class board for
  this exact Waveshare part. `memory_type = qio_opi` is the key flag that actually turns on
  **OPI** PSRAM (the R8 part uses octal PSRAM; `qio_qspi` would fail to map it). The
  devkitc-1 JSON defaults to 8MB flash, so `board_upload.flash_size = 16MB` +
  `default_16MB.csv` partition override is required for the real 16MB part.
- **`ARDUINO_USB_CDC_ON_BOOT=1` + `ARDUINO_USB_MODE=1`**: native-USB CDC so `Serial` shows
  up as `/dev/cu.usbmodem1101` without a separate UART bridge.
- **LovyanGFX over TFT_eSPI/LVGL**: LovyanGFX has a first-class `Panel_ST7789` with explicit
  `offset_x/offset_y` config — the exact knobs this 172-wide panel needs — and the proven
  reference repo uses it. TFT_eSPI needs `#define USE_HSPI_PORT` + manual CGRAM offset hacks;
  LVGL is heavier than this low-text-density UI warrants.
- **Adafruit NeoPixel over FastLED**: single WS2812, RMT-backed on ESP32-S3, smaller and
  simpler than FastLED's controller machinery for one pixel.

---

## 4. Module layout

```
firmware/src/
├── secrets.h     # (scaffolded) WIFI_SSID / WIFI_PASS / FRAME_BASE_URL / DEVICE_ID / DEVICE_TOKEN
├── theme.h       # palette (from scenes.html :root) + Scene/Status/Accent enums + parsers
├── display.h     # LovyanGFX AgentLampDisplay panel class (the offsets live here)
├── led.h         # StatusLed — WS2812 wrapper, brightness-capped, idempotent setColor
├── frame.h       # Frame model (bounded char bufs) + parseFrame() (ArduinoJson v7)
├── renderer.h    # Renderer — one method per scene, draws to the panel
└── main.cpp      # WiFi join, poll loop, failure/stale state machine, scene dispatch
```

Single-responsibility split: the **panel offsets** live only in `display.h`; the **palette**
only in `theme.h` (mirrored from the mockup CSS so the device matches the design board); the
**frame validation** only in `frame.h`; **drawing** only in `renderer.h`; the **state
machine** only in `main.cpp`.

---

## 5. Scene render approach

Design language (from `docs/ui/mockups/scenes.html`): near-black base `#080a10`, **one
dominant status word** in the accent colour, a small mono top bar (`● who` left + clock
right), and a faint bottom meta line. I draw straight to the panel (no PSRAM sprite) — text
density is low enough that LovyanGFX's per-call clipping avoids visible flicker, and it keeps
SRAM cheap. Repaint is gated: a scene only redraws when the **scene or frame `seq` changes**
(anti-flicker per the contract "if `seq` unchanged, continue").

| Scene | Render approach |
|-------|-----------------|
| **Boot** | Orb glyph + "AgentLamp" + "starting vX" + "local mode" footer; dim stale-white LED. |
| **WiFiConfig / Pairing** | Big mono code (`SETUP`/`RETRY`/pair code) in cyan, "enter on laptop" helper, footer hint. Shown when `WIFI_SSID=="REPLACE_ME"` **or** join fails; cyan/red LED. |
| **Live / Focus** | kicker `provider account`, dominant status word (auto-shrinks font for long words like THINKING), `project` + `task`, `seq` footer. LED = status accent. This is the focus/status view. |
| **Fleet** | up to 5 rows `provider …… status-tag`, each tag in its own status colour; bottom `N active +M more`. |
| **Quota** | up to 2 horizontal bars; fill colour red ≥70% / amber ≥40% / teal else; `%` + `est` tag. |
| **Alert** | big coloured ring + `!` (waiting) or `x` (error) glyph, dominant status word, `provider account` + task, headline footer. Amber/red LED. Preempts normal scenes. |
| **Offline** | dominant `OFFLINE` in grey (no glow), "frame source unreachable", "last seen Nm ago". Dim grey LED. Shown after 3 consecutive fails. |
| **Stale** | last-good status word in stale-white (no glow), "showing cached", "updated Nm ago". Dim white LED. |
| **Diagnostics (PAIRING REQUIRED)** | shown on 401/403/404; stops normal polling; red LED. |
| **Sleep** | dim ambient (idle), very dim blue LED. |

The clock in the top bar is currently **uptime mm:ss** (no RTC / NTP yet — a wall clock is a
later add); staleness deliberately does **not** use it.

### LED ↔ status mapping (from the design legend + contract)

coding=purple, thinking=blue-purple, reading=cyan, testing=teal, waiting=amber, done=green,
idle=blue, error=red, offline=dim grey, stale=dim white. Brightness is globally capped at
~25% (`setBrightness(64)`) per the contract's 20–35% rule so a desk orb isn't a flashlight.

---

## 6. Frame validation + state machine (contract compliance)

`parseFrame()` (frame.h):

- **Reject body > 2 KB** — checked twice: `HTTPClient.getSize()` before reading, and
  `body.length()` after (defensive; the server already trims, but a hostile body must not
  blow RAM). `FRAME_MAX_BYTES = 2048`.
- **Reject unknown `v`** — `if (v != 1) return false`. (But *fields* are read with `| default`
  so **unknown fields are ignored**, not rejected → forward-compatible.)
- All strings copied into **fixed `char[]` buffers** via `strlcpy` — no heap in the model, so
  a garbage frame can't grow RAM. Arrays capped at fleet≤6 / quota≤2 (contract caps).
- Quota windows that are **omitted** (never null per the contract) map to a `-1` sentinel, not
  zero, so "no data" ≠ "0%".

`main.cpp` state machine:

- Poll every **4 s** (contract 3–5 s); **2 s** HTTP timeout (`setTimeout` + `setConnectTimeout`).
- **3 consecutive failures → Offline.** A failure is any transport error / oversized / bad
  JSON / 429 / 503; the **last valid cached frame is retained** across failures.
- **Staleness from LOCAL elapsed `millis()`**, never RTC vs `server_time`: `elapsed >
  ttl*1000*3` (a ttl×3 grace window) → Stale. A skewed device clock can't misjudge staleness.
- **401/403/404 → "PAIRING REQUIRED"** diagnostics scene and **stop normal polling**
  (`pairingRequired` latch).
- **429 → back off** to `max(poll*2, 60s)`, honouring `Retry-After`; resets to 4 s on next OK.
- **Never prints the token or the full body** over serial (contract acceptance) — only
  scene/seq/ttl/http-code summaries.

---

## 7. COMPILE GATE

Command:

```bash
/Users/hulu/huluman/agentlamp/.venv/bin/pio run -d /Users/hulu/huluman/agentlamp/firmware
```

### Result: SUCCESS on the first attempt (no fix iterations needed)

The first `pio run` on this machine cold-installed the whole toolchain before compiling
(this dominated the 908 s wall time — the actual C++ compile is a fraction of it):

```
Platform Manager: espressif32@7.0.1 has been installed!
Tool Manager: toolchain-xtensa-esp32s3@8.4.0+2021r2-patch5 has been installed!
Tool Manager: toolchain-riscv32-esp@8.4.0+2021r2-patch5 has been installed!
Tool Manager: framework-arduinoespressif32 @ ~3.20017.0  (= Arduino-ESP32 core 2.0.17)
Tool Manager: tool-esptoolpy@2.41100.0 / tool-scons@4.40801.0 has been installed!
Library Manager: ArduinoJson@7.4.3 has been installed!
Library Manager: LovyanGFX@1.2.21 has been installed!
Library Manager: Adafruit NeoPixel@1.15.5 has been installed!
...
Compiling .pio/build/waveshare-s3-lcd-147/lib38e/LovyanGFX/... (full LovyanGFX tree)
Compiling .pio/build/waveshare-s3-lcd-147/src/main.cpp.o
Linking .pio/build/waveshare-s3-lcd-147/firmware.elf
Retrieving maximum program size .pio/build/waveshare-s3-lcd-147/firmware.elf
Checking size .pio/build/waveshare-s3-lcd-147/firmware.elf
Advanced Memory Usage is available via "PlatformIO Home > Project Inspect"
RAM:   [=         ]   7.9% (used 25816 bytes from 327680 bytes)
Flash: [=         ]  10.5% (used 688877 bytes from 6553600 bytes)
Building .pio/build/waveshare-s3-lcd-147/firmware.bin
esp32s3 image... Merged 2 ELF sections. Successfully created esp32s3 image.
======================== [SUCCESS] Took 908.07 seconds ========================
```

### Final usage

| Metric | Used | Total | % |
|--------|------|-------|---|
| **RAM (SRAM)** | 25,816 B | 327,680 B | **7.9%** |
| **Flash** | 688,877 B | 6,553,600 B | **10.5%** |
| **`firmware.bin`** | 689 KB | — | — |

- **Compile errors: 0. Compile warnings: 0.** (The one `WARNING:` in the raw log is a benign
  *pip cache deserialization* notice during the esptool dependency install — not from the
  C++ build.) No fix iterations were required: the panel config was copied verbatim from the
  proven reference, the LovyanGFX font/API names are all valid, and the bounded
  ArduinoJson-v7 parser used only stable APIs.
- **RAM 7.9% is comfortable**: the build draws *no* PSRAM framebuffer sprite (direct-to-panel
  rendering), so the 25 KB SRAM is just WiFi/HTTP/JSON/render scratch — well inside the
  contract's "keep ≥ 40 KB free" headroom. PSRAM stays free for a future double-buffer sprite
  if smoother transitions are wanted.
- **Flash 10.5%** against the 16MB part (6.25 MB app partition from `default_16MB.csv`) leaves
  huge OTA headroom.

### Build environment note

`espressif32@7.0.1` pulls **Arduino-ESP32 core 2.0.17** (`framework-arduinoespressif32
~3.20017.0`), not the 3.x core. That is fine here: ArduinoJson v7, LovyanGFX 1.2.x, and the
`WiFi` / `HTTPClient` (incl. `http.header("Retry-After")`) APIs the firmware uses are all
stable across core 2.0.x ↔ 3.x. No code change was needed for the core version.

---

## 8. What was NOT done (by instruction / scope)

- **Not flashed** — the orchestrator flashes inline later. `pio run -t upload` was not run.
- **Not committed** — no `git add`/`commit`.
- **WiFi SoftAP captive portal** (contract §WiFi Provisioning) is **stubbed**: the firmware
  detects `SSID=="REPLACE_ME"` or a failed join and shows a WiFiConfig/Pairing scene, but the
  full AP + form + NVS write is a later add. Today's path is "creds in `secrets.h` →
  reflash". The scene + branch are in place so the portal drops in without restructuring.
- **NTP wall clock** — the top-bar clock is uptime mm:ss for now; staleness already uses
  `millis()` so this is cosmetic.
- **Relay-mode TLS** (`WiFiClientSecure` + pinned root CA) — local mode is plain HTTP over
  LAN per the contract; TLS is the relay-mode add-on.
- **NVS-cached frame / button-hold reprovision** — cache is RAM-only for v1 (contract allows
  "optional NVS cache later").

These are all explicitly contract-optional or later-phase; the end-to-end pipeline
(WiFi → poll → validate → render → LED) is complete and compiles.

---

## Review fixes

Second-pass review of the firmware against the contract (round 2). Each finding was
**verified against the actual library source / code path before touching anything** —
two were confirmed false positives and left as-is with evidence. Recompile proof is at
the end.

### P0 — fixed

**P0-1 · oversized-body guard bypassed for chunked / no-Content-Length responses** (`main.cpp` fetchFrame, `frame.h:14`)
- **Verified: REAL.** Traced `framework-arduinoespressif32/libraries/HTTPClient/src/HTTPClient.cpp`:
  `getSize()` returns `_size`, which is `-1` when the server sends no `Content-Length`
  (chunked) — set at `HTTPClient.cpp:126/1246` and only overwritten from a real
  `Content-Length` header at `:1283`. The old guard `len > FRAME_MAX_BYTES` is then
  `-1 > 2048` → **false**, so it falls through to `getString()`, whose body (`:982`)
  does `if (_size > 0 || _size == -1)` → `writeToStream()` streaming the **entire body to
  EOF** into an unbounded heap `String`. A hostile/buggy chunked frame source could OOM
  the ESP32 before the post-read `body.length()` check ever runs. Violates
  `firmware_contract.md` Acceptance ("oversized body is rejected") + `device_frame_api.md`
  ("a hostile body must not blow RAM").
- **Fix:** replaced `getString()` with a bounded streamed read via `getStreamPtr()` into a
  single fixed `static char buf[FRAME_MAX_BYTES + 1]` (2049 B). The loop reads at most
  `2049 - got` bytes per iteration and returns `-2` the instant `got > FRAME_MAX_BYTES`, so
  RAM is bounded **regardless of whether Content-Length is present**. Chunked/slow sources
  are additionally bounded by the existing 2 s HTTP timeout. `getSize() > cap` is still
  checked first as a cheap early reject when Content-Length *is* present.

### P0 — verified FALSE POSITIVE (docs corrected, firmware unchanged)

**P0-2 · backlight pin: firmware BL=48 vs docs BL=46** (`platformio.ini:36`, `display.h:75` vs `docs/BUILD.md`, `firmware_contract.md`)
- **Verified: FIRMWARE IS CORRECT at 48.** `display.h:75` (`cfg.pin_bl = 48`),
  `platformio.ini:36` (`-D PIN_LCD_BL=48`), and devlog §2 all agree on GPIO48, cross-checked
  against the espp BSP + ahmadrezarazian LovyanGFX reference + TFT_eSPI discussion #3527.
  GPIO46 would leave the backlight **off** → dark screen. Codex's flag that "48 is a
  dark-screen bug" is a **FALSE POSITIVE** — do not change the firmware.
- **Fix:** corrected the two **stale docs** to 48: `docs/BUILD.md` Wiring/Pins table
  (`LCD backlight | 48`) and `firmware_contract.md` Hardware Notes (`LCD backlight GPIO48`
  with a note on why 46 was wrong). Firmware untouched.

### P1 — fixed

**P1-2 · WiFiConfig / join-failed setup scene immediately overwritten by Boot** (`main.cpp` setup/loop)
- **Verified: REAL.** `setup()` only sets `shownScene = Scene::BOOT` on the WiFi-**success**
  path; the two early-return config/fail branches leave `shownScene == UNKNOWN`. On the first
  `loop()` iteration `effectiveScene()` returns `Scene::BOOT` (`!haveCached`), `BOOT != UNKNOWN`
  → repaint, **erasing the "SETUP / join AgentLamp-XXXX / 192.168.4.1" instructions** the user
  needs on a fresh device.
- **Fix:** added a `provisioningHalt` latch set in both config/fail branches; `loop()`
  early-returns while latched (so `renderCurrent()` never runs), keeps the background
  reconnect for the join-failed case, and clears the latch + forces a fresh poll/repaint once
  WiFi actually joins.

**P1-3 · Retry-After silently ignored on 429** (`main.cpp` fetchFrame)
- **Verified: REAL.** `HTTPClient::header(name)` (`HTTPClient.cpp:1081`) iterates
  `_headerKeysCount`, which is **0** until `collectHeaders()` is called (`:1069`), so
  `http.header("Retry-After")` always returned `""` → backoff always fell to the 60 s floor.
  Contract `device_frame_api.md` 429 row says "honor `Retry-After`".
- **Fix:** `http.collectHeaders({"Retry-After"}, 1)` before `GET()`; the existing 429 branch
  now reads a real value.

**P1-4 · Offline/Stale time text + top-bar clock freeze** (`main.cpp` renderCurrent)
- **Verified: REAL.** The repaint gate only fired on scene-or-seq change, so "last seen Ns
  ago" / "updated Nm ago" / the uptime clock never advanced while parked in Offline/Stale.
- **Fix:** added a coarse 1 s `tick` that forces a repaint when the effective scene is
  Offline or Stale (the only time-bearing parked scenes), gated by `lastTimeRepaintMs` so
  Live scenes keep their anti-flicker behaviour unchanged.

**P1-7 · relay-mode HTTPS would fall into insecure TLS** (`main.cpp` fetchFrame)
- **Verified: REAL (latent).** v1 is local-mode `http://`, but if `FRAME_BASE_URL` were ever
  `https://`, the single-arg `http.begin(url)` on Arduino-ESP32 2.0.17 uses an
  unverified-TLS client — `firmware_contract.md` §TLS forbids unverified relay TLS.
- **Fix:** added a guard that rejects any `https://` base URL (returns transport-fail + a
  serial note) until `WiFiClientSecure` + pinned-CA support lands. Local-mode `http://` is
  unaffected.

**P1-8 · consecutiveFails (uint8_t) wraps at 255** (`main.cpp` loop)
- **Verified: REAL (very low severity).** After ~17 min of continuous failure the counter
  would wrap 255→0 and momentarily drop out of Offline.
- **Fix:** clamped — `if (consecutiveFails < 255) consecutiveFails++;`.

**P1-9 · build reproducibility weak** (`platformio.ini`)
- **Verified: REAL.** `platform = espressif32` (floating) + caret (`^`) lib ranges let a fresh
  checkout drift to newer toolchain/libraries.
- **Fix:** pinned `platform = espressif32@7.0.1` and exact lib versions
  (`ArduinoJson @ 7.4.3`, `LovyanGFX @ 1.2.21`, `Adafruit NeoPixel @ 1.15.5`) — the versions
  devlog §7 recorded as actually resolved.

### P1 — tracked / verified, no code change

**P1-1 · SoftAP captive portal + NVS provisioning not implemented** — **known-deferred**, already
documented in §8 ("WiFi SoftAP captive portal is stubbed; today's path is creds in `secrets.h`
→ reflash"). Contract `firmware_contract.md` §WiFi Provisioning requires it; **open acceptance
gap tracked** for a later phase. No code change this round.

**P1-5 · `cfg.invert=true` vs espp BSP `invert_colors=false`** (`display.h:65`) — **hardware-dependent,
cannot resolve without flashing.** The LGFX reference repo used `true`; the espp BSP used
`false`; this is panel-batch / library-convention dependent. **Verify on first flash**: if
colours render photo-negative, flip to `false`. Left as `true` (matches the proven LGFX
reference the rest of the panel config came from). No code change without hardware.

**P1-6 · column `offset_x=34` at 180° rotation** (`display.h:58`) — **verified DORMANT.** Hand-traced
LGFX `Panel_LCD::setRotation`: at `setRotation(0)` `_colstart=34,_rowstart=0,_width=172,_height=320`
(correct); at 180° LGFX would compute `colstart = 320-(172+34) = 114` (wrong), but the firmware
**only ever calls `setRotation(0)`** (`main.cpp` setup), so this never fires. Codex's "offset on
the wrong axis vs espp `lcd_offset_y=34`" is a **FALSE POSITIVE** — cross-library convention
mismatch; both center the same 172-wide window. No code change.

### Recompile proof

```bash
/Users/hulu/huluman/agentlamp/.venv/bin/pio run -d /Users/hulu/huluman/agentlamp/firmware
```

```
Linking .pio/build/waveshare-s3-lcd-147/firmware.elf
Checking size .pio/build/waveshare-s3-lcd-147/firmware.elf
RAM:   [=         ]   8.5% (used 27872 bytes from 327680 bytes)
Flash: [=         ]  10.5% (used 688261 bytes from 6553600 bytes)
Building .pio/build/waveshare-s3-lcd-147/firmware.bin
Successfully created esp32s3 image.
========================= [SUCCESS] Took 11.38 seconds =========================
```

- **Compile errors: 0. Compile warnings: 0. [SUCCESS].**
- **RAM 8.5%** (27,872 B, up from 25,816 B) — the +2,056 B is the new fixed 2,049 B body
  buffer, moved off the heap into static RAM. Still far inside the contract's "keep ≥ 40 KB
  free" headroom.
- **Flash 10.5%** unchanged. Not flashed, not committed.

