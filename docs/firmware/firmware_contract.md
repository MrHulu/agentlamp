# Firmware Contract

## Target

Waveshare ESP32-S3-LCD-1.47B.

## Responsibilities

- Connect to Wi-Fi.
- Fetch compact frame JSON from the frame source — HTTP over the LAN (local mode) or HTTPS (relay mode).
- Validate frame schema, TTL, size, and sequence.
- Render local scenes.
- Drive RGB LED according to status/accent.
- Cache last valid frame.
- Show Offline/Stale fallback.
- Expose diagnostics over serial.

## Non-Responsibilities

- No HTML/CSS/JS rendering.
- No provider API calls.
- No session sorting.
- No quota risk calculation.
- No admin dashboard.

## Suggested Modules

```text
firmware/
├── platformio.ini
├── src/
│   ├── main.cpp
│   ├── config/
│   ├── board/
│   ├── network/
│   ├── core/
│   └── ui/
```

## Frame Client Rules

- Timeout: 2 seconds per request.
- Poll interval: default 5 seconds, server may suggest via frame `ttl`.
- Max body: 2048 bytes.
- Reject unknown `v`; ignore unknown fields within a supported `v`.
- Preserve last valid frame in RAM; optional NVS cache later.
- Compute staleness from local elapsed time since fetch (`millis()`), not RTC vs `server_time`.
- Base URL (LAN `http://<ip>:8787` or relay `https://…`) and device token are provisioned
  at pairing and stored in NVS/Preferences.

## Memory Budget (must fit the board)

The target board's PSRAM presence MUST be confirmed and recorded in `BUILD.md`. The 172×320
16-bit framebuffer alone is ~110 KB, which does **not** fit alongside TLS + JSON + WiFi in
the ~512 KB SRAM with comfortable margin — so **PSRAM is treated as a hard requirement** for
the framebuffer until proven otherwise on the exact board revision.

| Allocation | Budget | Location |
|------------|--------|----------|
| Framebuffer (172×320×2) | ~110 KB | PSRAM |
| TLS (WiFiClientSecure, relay mode) | 40-50 KB | SRAM |
| Frame JSON buffer + parsed (ArduinoJson) | ≤ 16 KB | SRAM |
| Render scratch | ~20 KB | SRAM |
| Stack/heap headroom | keep ≥ 40 KB free | SRAM |

No allocation in the render path (`new`/`malloc` in the per-frame loop is forbidden); use
static/pooled buffers. In **local mode** TLS may be omitted (plain HTTP over LAN), freeing
~50 KB.

## TLS / Certificate Lifecycle (relay mode)

- Pin a **long-lived root CA** (e.g. ISRG Root X1, valid into the 2030s), **not** an
  intermediate (Let's Encrypt rotates intermediates; pinning one risks a bricked device if
  it rotates while the device is offline). Bundle **2+ roots** for resilience.
- Cert refresh: the device fetches `GET /api/v1/device/{id}/cacerts` (authenticated with the
  same device token) and stores the bundle in NVS, validated before the main handshake.
- On TLS validation failure: show Diagnostics scene, keep cached frame, retry with backoff —
  never fall back to unverified HTTP in relay mode.

## OTA (optional — but signed if shipped)

- OTA is optional. **If** OTA ships, images MUST be signed (Ed25519); the bootloader verifies
  the signature before applying, and a failed/aborted update rolls back to the previous
  partition. An unsigned OTA path is remote code execution and is forbidden.
- A USB/serial reflash recovery path is always documented (factory reset).

## WiFi Provisioning

First boot has no credentials. Provisioning method (pick one, document in `BUILD.md`):
- **SoftAP captive portal** (recommended): device hosts a temporary AP + a small form to
  enter SSID/password and base URL; stored in NVS.
- BLE provisioning, or serial/USB as a headless fallback.
Re-provisioning: hold a button N seconds → clear NVS WiFi creds → re-enter portal.

## RGB Defaults

| Status | Effect |
|--------|--------|
| IDLE | dark blue breathing |
| THINKING | blue-purple breathing |
| CODING | purple pulse |
| WAITING | yellow blink |
| DONE | green bloom |
| ERROR | red blink |
| OFFLINE | red-blue alternate |
| STALE | white slow blink |

Brightness cap: 20%-35% by default.

## Hardware Notes

Use official board docs for final pins. Current known pins:

- LCD MOSI GPIO45.
- LCD SCLK GPIO40.
- LCD CS GPIO42.
- LCD DC GPIO41.
- LCD RST GPIO39.
- LCD backlight GPIO46. (EMPIRICALLY CONFIRMED on the real -1.47B hardware by a pin sweep
  2026-05-30: driving GPIO46 lights the panel; driving GPIO48 leaves the screen DARK. The
  espp BSP, the ahmadrezarazian LovyanGFX reference, and the generic Waveshare wiki give 48,
  but those describe the non-B variant — this **-1.47B** board uses 46. Firmware uses 46
  (platformio.ini `-D PIN_LCD_BL=46`); this doc is reconciled to the working code.)
- RGB LED GPIO38.

## Firmware Acceptance

- Valid frame renders.
- Invalid/malformed JSON does not crash; diagnostics shown without printing token or full
  frame over serial.
- Oversized body is rejected; cached frame retained.
- Unknown fields within a supported `v` are ignored (forward-compatible).
- Three failed requests show Offline; 403/`device_revoked` shows "PAIRING REQUIRED".
- Expired cached frame (by local elapsed time) shows Stale.
- Scene transition does not flicker under normal polling.
- First-boot provisioning portal stores creds in NVS; re-provisioning clears them.
- (If OTA enabled) unsigned image is rejected; failed update rolls back.

