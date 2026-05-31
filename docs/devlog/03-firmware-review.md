# Devlog 03 — Firmware Review (independent chain reviewer)

**Date:** 2026-05-30
**Reviewer role:** Independent CHAIN REVIEWER (multi-ai chain step), cross-checked with Codex (`gpt-5-codex`, fresh).
**Scope:** `firmware/` (platformio.ini + src/*.h + main.cpp) against `docs/firmware/firmware_contract.md`, `docs/api/device_frame_api.md`, `docs/BUILD.md`, `docs/ui/display_spec.md`, `docs/devlog/03-firmware-impl.md`.

---

## Verdict: **REVISE**

The firmware **compiles** (verified — see below), the **panel config is correct** (pins + backlight=48 + column offset all confirmed against the authoritative espp BSP), and the **JSON parse / state machine is sound and contract-aligned**. But there are **2 P0** (one real robustness hole, one doc/firmware contradiction that must be reconciled) and **6 P1** correctness/UX gaps that should be fixed before this is called done. None block the build; all are fixable without restructuring.

The single most important *positive* finding: the **backlight pin = 48 is CORRECT** and the docs (BUILD.md / contract say 46) are the ones that are wrong. Codex flagged 48 as a P0 "dark screen" bug — that is a **false positive**; the espp board-support package the author cited lists `backlight_io = GPIO_NUM_48` verbatim. See P0-2.

---

## How the COMPILE claim was verified

The devlog claims `pio run` SUCCESS on first attempt, RAM 7.9% / Flash 10.5%, `firmware.bin` 689 KB. I verified this **without re-running the build** (review-only):

- `firmware/.pio/build/waveshare-s3-lcd-147/firmware.bin` exists, **689 KB**, built **2026-05-30 21:59** — *after* `main.cpp` was last edited (21:43).
- `firmware.elf` present (20 MB debug elf); `src/main.cpp.o` present, same 21:59 timestamp.
- `strings firmware.bin` contains the exact scene literals from `renderer.h` ("showing cached", "PAIRING REQUIRED", "AgentLamp", "frame source", "unreachable") → the binary was built from *this* source, not a stale scaffold.
- Every LovyanGFX symbol the code uses is declared in the installed LovyanGFX 1.2.x: `FreeSansBold18pt7b/24pt7b/12pt7b`, `FreeMonoBold18pt7b`, `Font2`, `Font4`, `fillRoundRect`, `drawCircle`, `fillCircle`, `setBrightness`, `setTextDatum`. ArduinoJson v7 (`JsonDocument`, `deserializeJson`, `JsonObjectConst`, `is<float>()`) and the Adafruit NeoPixel APIs are all valid.
- `default_16MB.csv` exists in `framework-arduinoespressif32/tools/partitions/`.

**COMPILE claim: CONFIRMED.** RAM 7.9% is plausible because rendering goes straight to the panel (no PSRAM framebuffer sprite), so SRAM is just WiFi/HTTP/JSON/render scratch.

---

## Panel config (area a) — verified against the espp BSP

I fetched the espp `ws-s3-lcd-1-47` header (cited by both the author and Codex):
`https://github.com/esp-cpp/espp/.../ws-s3-lcd-1-47.hpp`

| Signal | firmware | espp BSP | match |
|--------|----------|----------|-------|
| MOSI | 45 | 45 | ✓ |
| SCLK | 40 | 40 | ✓ |
| CS | 42 | 42 | ✓ |
| DC | 41 | 41 | ✓ |
| RST | 39 | 39 | ✓ |
| **Backlight** | **48** | **48** | ✓ |
| RGB LED | 38 | 38 | ✓ |

**All 7 pins match.** Backlight 48 is right (docs are stale → P0-2).

**Column offset (offset_x=34): CORRECT for LovyanGFX.** I traced `Panel_LCD::setRotation(0)` math by hand with the firmware's config (`memory_width=320, memory_height=172, panel_width=172, panel_height=320, offset_x=34, offset_rotation=0`):
- `_internal_rotation = 0` → no axis swap.
- `_width=panel_width=172`, `_height=panel_height=320` ✓ portrait.
- `_colstart = (rot&2)? mw-(pw+ox) : ox = 34` ✓ — the 172-wide window centered in the 240-col GRAM, (240−172)/2 = 34.
- `_rowstart = oy = 0` ✓.

Codex flagged the offset axis as P1 ("espp uses `lcd_offset_y=34`"). That is a **cross-library convention mismatch, not a bug**: espp's st7789 driver and LovyanGFX label the offset axis differently relative to MADCTL; both center the same window. The author copied the LGFX config verbatim from a proven LGFX repo (ahmadrezarazian). I judge this **NOT a defect** but flag a **latent** issue: at `setRotation(2)` (180°) the LGFX colstart would compute `320-(172+34)=114` (wrong) — only matters if rotation is ever changed; the firmware only ever calls `setRotation(0)`. (See P1-6.)

**One genuine discrepancy:** firmware sets `cfg.invert = true`; the espp BSP sets `invert_colors = false`. The LGFX reference repo used `invert=true`. invert is panel-batch/library dependent — **cannot resolve without hardware**; flag as P1-5 "verify on first flash; if colours are negative, flip invert."

---

## Codex raw findings (verbatim)

> - [P0] platformio.ini:33 / display.h:72 backlight GPIO48 vs docs GPIO46 → likely dark screen.  **[REVIEWER: FALSE POSITIVE — espp BSP confirms 48; docs are wrong.]**
> - [P0] main.cpp:105 oversized guard only fires when getSize()>0; chunked getSize()==-1 → getString() reads beyond 2KB before body.length() check.  **[CONFIRMED → P0-1]**
> - [P0] main.cpp:84 http.begin(url) no CA handling → insecure TLS fallback for https relay mode.  **[VALID but relay-mode is explicitly out-of-scope for v1 local mode; → P1-7]**
> - [P0] Provisioning not implemented (secrets.h compile-time, no SoftAP/NVS).  **[VALID but devlog §8 explicitly defers it; → P1-1]**
> - [P1] display.h offset on wrong axis vs espp.  **[FALSE POSITIVE — cross-library convention; see above]**
> - [P1] WiFi setup/error scene immediately overwritten by Boot in loop().  **[CONFIRMED → P1-2, material first-boot bug]**
> - [P1] Retry-After ignored (no collectHeaders()).  **[CONFIRMED → P1-3]**
> - [P1] Offline/Stale timestamps + uptime clock freeze (repaint gate).  **[CONFIRMED → P1-4]**
> - [P1] consecutiveFails uint8_t wraps at 255.  **[CONFIRMED, very low sev → P1-8]**
> - [P1] Build reproducibility weak: unpinned platform + caret lib ranges.  **[VALID, minor → P1-9]**

Codex did not re-run `pio run` (review-only). Codex sources: Waveshare 1.47B wiki, Waveshare non-B 1.47 wiki, espp board header.

---

## Reviewer findings — P0

### P0-1 — Oversized-body guard is bypassed for chunked / no-Content-Length responses
**Files:** `firmware/src/main.cpp:105-110`, `firmware/src/frame.h:14`
`int len = http.getSize();` returns **-1** when the server sends no `Content-Length` (chunked transfer) — confirmed in `HTTPClient.cpp` (`_size = -1`). Then `if (len > FRAME_MAX_BYTES)` is `-1 > 2048` = **false**, so the pre-read guard passes. `http.getString()` for `_size == -1` reserves 0 bytes and streams the **entire** body into a heap `String` with no cap (confirmed in `HTTPClient::getString` → `writeToStream`), only *then* is `body.length() > FRAME_MAX_BYTES` checked. A hostile or buggy chunked frame source can stream multi-MB and OOM the ESP32 before the post-check fires. The contract (`firmware_contract.md:44`, `device_frame_api.md:153`) requires a hard 2 KB cap and the devlog claims "a hostile body must not blow RAM" — this hole defeats that for the chunked case.
**Fix:** bound the streamed read — e.g. read from `http.getStreamPtr()` into a fixed 2049-byte buffer and bail at >2048; or reject `len < 0` outright (the local server always sets Content-Length); or `http.getString()` only after asserting `0 <= len <= FRAME_MAX_BYTES`.

### P0-2 — Doc/firmware contradiction on backlight pin (resolve the docs, not the firmware)
**Files:** firmware `platformio.ini:36` + `display.h:75` say **48**; `docs/BUILD.md:31` + `docs/firmware/firmware_contract.md:119` say **46**.
The **firmware is correct (48)** — confirmed against the espp BSP (`backlight_io = GPIO_NUM_48`) and the author's TFT_eSPI discussion citation. The **docs are stale and must be corrected to 48**, otherwise the next person hand-verifies against the wrong number and "fixes" a working pin. This is a P0 because it is a live, load-bearing contradiction in the contract that the firmware depends on. **Action: edit BUILD.md + firmware_contract.md to 48 (active-high PWM).** (No firmware change.)

---

## Reviewer findings — P1

### P1-1 — First-boot provisioning (SoftAP captive portal + NVS) not implemented
**File:** `main.cpp:13,228-242` — creds are compile-time `secrets.h`; on placeholder/failed-join it shows a hint scene and tells the user to reflash. Contract `firmware_contract.md:88-93,132` requires a SoftAP portal storing creds in NVS + re-provision clear. **Explicitly deferred in devlog §8** ("creds in secrets.h → reflash"; scene/branch in place so the portal drops in later). Accept as known-deferred scope, but it is an open **acceptance-criteria gap** — track it; do not let it silently become "done".

### P1-2 — WiFiConfig / join-failed scene is immediately overwritten by Boot (material first-boot UX bug)
**Files:** `main.cpp:228-242` (setup renders `wifiConfig` then returns) + `main.cpp:291` (`loop()` always calls `renderCurrent`) + `main.cpp:117-120` (`effectiveScene` returns `Scene::BOOT` when `!haveCached`).
On a fresh/un-provisioned device, setup paints the "SETUP / join AgentLamp-XXXX / open 192.168.4.1" screen, then the **very first `loop()` iteration** computes `effectiveScene → BOOT` (no cache, not offline, not pairing), sees `BOOT != shownScene(UNKNOWN)` → repaints Boot, **erasing the setup instructions the user needs**. Same for the join-failed scene. The setup hint is never visible.
**Fix:** add a latch (e.g. `Scene wifiHoldScene`/`bool inWifiConfig`) that `effectiveScene` honours so the config/fail screen persists, or early-return from `loop()` while un-provisioned/disconnected without calling `renderCurrent`.

### P1-3 — `Retry-After` is silently ignored on 429
**File:** `main.cpp:96-98`. `http.header("Retry-After")` always returns "" because `http.collectHeaders({"Retry-After"})` is never called (Arduino HTTPClient only stores headers registered via `collectHeaders`; `_headerKeysCount==0` → empty). So `backoff=0` and the code always falls back to the 60 s floor, never the server's suggested value. Contract `device_frame_api.md:165` says "honor `Retry-After`". Backoff still *works* (60 s default), so low impact, but the contract clause is unmet.
**Fix:** call `const char* keys[]={"Retry-After"}; http.collectHeaders(keys,1);` after `http.begin()`.

### P1-4 — Offline / Stale "last seen" + uptime clock freeze (repaint gate too aggressive)
**File:** `main.cpp:145-147`. Repaint only fires on scene-or-seq change. While parked in Offline/Stale (no new seq), "last seen Ns ago", "updated Nm ago", and the top-bar uptime clock stop advancing — they show the value from the first paint forever. Cosmetic but visibly wrong (a frozen "last seen 4s ago" after 10 min).
**Fix:** in Offline/Stale/idle, also repaint on a coarse timer (e.g. every 1 s or when the displayed seconds bucket changes), or exempt the time-bearing scenes from the seq gate.

### P1-5 — `invert=true` vs espp BSP `invert_colors=false` (verify on hardware)
**File:** `display.h:65`. The espp BSP says invert OFF; the LGFX reference repo (and this firmware) say ON. invert is panel-batch/library-convention dependent and cannot be resolved without flashing. **Action:** on first flash, if colours render as photo-negative, flip `cfg.invert`. Document the verified value.

### P1-6 — Latent: column offset is wrong if `setRotation(2/180°)` is ever used
**File:** `display.h:58` + LGFX `setRotation`. At rotation 0 the offset is correct (traced above). At 180° LGFX would compute colstart `320-(172+34)=114` (asymmetric/wrong). Firmware only ever calls `setRotation(0)`, so dormant. **Action:** if portrait-flip is ever added, re-derive the offset; leave a comment.

### P1-7 — Relay-mode HTTPS has no CA pinning (out of v1 scope, but guard it)
**File:** `main.cpp:84` `http.begin(url)` with a plain URL. If `FRAME_BASE_URL` is ever an `https://` relay, Arduino-ESP32 2.0.17 `HTTPClient::begin(String)` does not attach a CA → insecure TLS. Contract `firmware_contract.md:70-78` forbids unverified HTTP in relay mode and requires pinned roots. Local mode (plain HTTP over LAN) is the v1 path and is fine. **Action:** when relay mode lands, use `WiFiClientSecure` + `setCACert`; until then, assert the base URL is `http://` (or reject `https://` without a CA bundle) so it can't silently fall into insecure TLS.

### P1-8 — `consecutiveFails` (`uint8_t`) wraps at 255
**File:** `main.cpp:40,286`. After 255 continuous failures (~17 min at 4 s poll) it wraps to 0, momentarily dropping out of Offline (`>=3`) back to cached/Boot before climbing again. Extremely low severity. **Fix:** clamp (`if (consecutiveFails < 255) consecutiveFails++;`).

### P1-9 — Build reproducibility: unpinned `platform` + caret lib ranges
**File:** `platformio.ini:13,55-57`. `platform = espressif32` (no `@version`) and `^` lib ranges mean a future `pio run` may pull different toolchain/lib versions than the devlog's (espressif32@7.0.1, LovyanGFX@1.2.21, ArduinoJson@7.4.3). The current `.pio` artifacts prove *a* build happened, but the result isn't reproducible from config alone. **Fix:** pin `platform = espressif32@7.0.1` and exact lib versions for a release.

---

## Minor / informational (not counted P0/P1)

- `renderer.h:189` `tolower(*p)` on a (possibly signed) `char` is technically UB for negative values; benign here (ASCII status words only). Cast to `unsigned char` for cleanliness.
- `effectiveScene` maps a server-sent `scene:"pairing"` to `default → FOCUS` (Scene::PAIRING/WIFICONFIG not in the switch). Device-driven pairing (401/403/404 → DIAGNOSTICS) is the real path, so a server "pairing" scene just renders Focus. Minor scene-coverage gap.
- `accent` defaults to `MUTED` (grey) when the server omits it; in the Alert scene that would draw a grey ring instead of amber/red. The generator is expected to always set accent for alerts, but the firmware has no status→accent fallback for ALERT/FLEET/QUOTA (only FOCUS falls back to `statusColor`). Consider extending the FOCUS fallback to ALERT.
- No token leak over serial: **confirmed** — `setup()` prints device_id/url/PSRAM/heap only; the poll loop prints scene/seq/ttl/http-code; the token and full body are never printed. Contract acceptance met.
- Malformed JSON: **confirmed no-crash** — `deserializeJson` error → `return false` → counted as a fail, cache retained. Bounded `char[]` copies via `strlcpy`, arrays capped fleet≤6/quota≤2, no heap in the Frame model. Solid.
- Staleness via local `millis()` not RTC: **confirmed** (`main.cpp:122-125`), per contract.

---

## Bottom line

Strong, contract-aware firmware that genuinely compiles and whose panel config is **verified correct** (the backlight=48 the author defended is right; the docs are the ones to fix). Ship-blockers are small: close the chunked-body RAM hole (P0-1), reconcile the backlight doc contradiction (P0-2), and fix the first-boot scene-overwrite (P1-2) + Retry-After (P1-3) + frozen Offline/Stale timestamps (P1-4). The rest are deferred-scope or low-severity. **REVISE**, not FAIL.
