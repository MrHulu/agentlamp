# Devlog 01 — Toolchain & Scaffolding Setup

**Date:** 2026-05-30
**Agent role:** Setup / DevOps (AgentLamp HARDWARE build)
**Goal:** Stand up the Python toolchain + project scaffolding so the Server and
Firmware phases can build.
**Outcome:** SUCCESS — all 6 steps completed, board probed read-only, no flash/erase, no commit.

---

## Environment

| Item | Value |
|------|-------|
| Host | macOS (Darwin 25.3.0, arm64 — Apple Silicon MacBook Air) |
| Project root | `/Users/hulu/huluman/agentlamp` (git, branch `main`) |
| Base `python3` | 3.14.3 |
| Board | Waveshare ESP32-S3-LCD-1.47B (ESP32-S3R8) |
| Serial port | `/dev/cu.usbmodem1101` (ESP32-S3 native USB) |
| Laptop LAN IP | **`192.168.1.148`** (en0) |

---

## Step 1 — Create venv + install toolchain

### Create the venv

```bash
python3 -m venv /Users/hulu/huluman/agentlamp/.venv
```

Output: `VENV_CREATED`. `.venv/bin/python -> python3.14`, Python **3.14.3**.
(`.venv/` is already covered by `.gitignore` — verified with `git check-ignore .venv` → ignored.)

### Upgrade pip

```bash
.venv/bin/pip install --upgrade pip
```

Output (tail): `Successfully installed pip-26.1.1` (from pip-26.0). Exit 0.

### Install packages

```bash
.venv/bin/pip install platformio esptool fastapi "uvicorn[standard]" httpx pydantic pytest
```

Ran in the background (PlatformIO pulls a large dep tree; uvicorn resolution took a while).
Exit **0**. Key installed versions:

| Package | Version |
|---------|---------|
| platformio | 6.1.19 |
| esptool | 5.2.0 |
| fastapi | 0.136.3 |
| uvicorn | 0.40.0 (+ uvloop 0.22.1, httptools 0.8.0, websockets 16.0, watchfiles 1.2.0, python-dotenv 1.2.2) |
| httpx | 0.28.1 |
| pydantic | 2.13.4 (pydantic-core 2.46.4) |
| pytest | 9.0.3 |

Notable transitive deps: starlette 0.52.1, anyio 4.13.0, cryptography 48.0.0,
pyserial 3.5, reedsolo 1.7.0, esptool stub flasher bundled.

---

## Step 2 — Verify toolchain

```bash
.venv/bin/pio --version          # -> PlatformIO Core, version 6.1.19
.venv/bin/python -m esptool version   # -> esptool v5.2.0 / 5.2.0
```

Both reported clean versions. ✓

---

## Step 3 — Board probe (READ-ONLY, no erase/write)

```bash
.venv/bin/python -m esptool --chip esp32s3 --port /dev/cu.usbmodem1101 flash_id 2>&1 | head -20
```

Connected on the **first try — no BOOT-button hold required**. (esptool notes
`flash_id` is deprecated in favor of `flash-id`; cosmetic only, ran fine.)

**Probe result:**

| Field | Value |
|-------|-------|
| Chip type | ESP32-S3 (QFN56), revision **v0.2** |
| Features | Wi-Fi, BT 5 (LE), Dual Core + LP Core, 240 MHz, **Embedded PSRAM 8 MB (AP_3v3)** |
| Crystal | 40 MHz |
| USB mode | USB-Serial/JTAG (native) |
| **MAC** | **44:1b:f6:86:59:68** |
| Flash manufacturer | `0x20` |
| Flash device | `0x4018` |
| **Detected flash size** | **16 MB** |
| Flash type (eFuse) | quad (4 data lines) |
| Flash voltage (eFuse) | 3.3 V |

This **confirms the board spec exactly**: ESP32-S3R8, 8 MB OPI PSRAM, 16 MB flash.
The firmware contract (`docs/firmware/firmware_contract.md` → Memory Budget) treats PSRAM
as a hard requirement for the 172×320×2 (~110 KB) framebuffer — **confirmed present (8 MB)**.

No erase, no write, no flash performed. esptool ran its stub flasher (RAM-only) and
hard-reset via RTS at the end; the board's flash contents were not modified.

---

## Step 4 — Scaffold directories

```bash
mkdir -p server/agentlamp_server docs/devlog
# firmware/ already existed (placeholder README + empty src/)
```

### Server (FastAPI app package) — `server/agentlamp_server/`

| File | Purpose |
|------|---------|
| `__init__.py` | Package marker + module docstring (`__version__ = "0.0.1"`) |
| `app.py` | FastAPI app implementing a **minimal slice of the device frame contract** |

`app.py` implements the authoritative contract from `docs/api/device_frame_api.md`:

- `GET /api/v1/device/{device_id}/frame` — Bearer-token auth, `X-Frame-Schema-Version`
  negotiation (`min(server, requested)`), returns Frame Schema **v1**.
- Error bodies `{"error": ..., "retry": ...}` matching the contract table:
  `401 bad_token` (no/wrong token), `404 unknown_device` (wrong device id).
- `GET /healthz` for liveness.
- Dev token/device id default to `dev-local-token` / `orb-01` (overridable via env
  `AGENTLAMP_DEV_DEVICE_TOKEN` / `AGENTLAMP_DEV_DEVICE_ID`), matching `secrets.h` placeholders.
- This is intentionally a **scaffold** — the real frame generator (priority scoring, quota
  risk, fleet truncation to the 2 KB budget) lands in the Server phase per `docs/cloud/`.

> Note: the repo also has a `src/` tree (`src/collector`, `src/cloud`, `src/admin`) of
> placeholder READMEs from the original spec. Per this task's instructions the FastAPI app
> was scaffolded under `server/agentlamp_server/` and the existing `src/` was left untouched.

**Functional smoke test** (FastAPI TestClient, port-free):

```
healthz    200  {'ok': True, 'service': 'agentlamp-frame-server', 'v': 1}
no-auth    401  {'error': 'bad_token', 'retry': False}
auth       200  X-Frame-Schema-Version=1, body v=1, device_id=orb-01
bad-device 404  {'error': 'unknown_device', 'retry': False}
```

All four match the contract. ✓

### Firmware (PlatformIO project) — `firmware/`

| File | Purpose |
|------|---------|
| `platformio.ini` | Pinned board + toolchain (created) |
| `src/main.cpp` | Scaffold entry point — serial diag of PSRAM/flash/heap (created) |
| `src/secrets.h` | Placeholder credentials, **gitignored** (created — see Step 5) |
| `README.md` | Pre-existing placeholder (left as-is) |

`platformio.ini` env `waveshare-s3-lcd-147`:

- `platform = espressif32`, `board = esp32-s3-devkitc-1` (electrically an ESP32-S3R8),
  `framework = arduino`.
- Memory overrides for the exact module: `board_build.arduino.memory_type = qio_opi`
  (8 MB OPI PSRAM), `board_upload.flash_size = 16MB`, `partitions = default_16MB.csv`.
- Native USB CDC: `ARDUINO_USB_MODE=1`, `ARDUINO_USB_CDC_ON_BOOT=1` so `Serial` works over
  `/dev/cu.usbmodem*`. `BOARD_HAS_PSRAM` defined.
- LCD geometry + pin map baked in as `-D` flags from `docs/BUILD.md` (Wiring/Pins):
  MOSI 45, SCLK 40, CS 42, DC 41, RST 39, BL 46, RGB LED 38; `LCD_WIDTH=172`, `LCD_HEIGHT=320`.
- `lib_deps`: ArduinoJson ^7 (LovyanGFX commented, to be enabled in the Firmware phase).
- `monitor_speed = 115200`, `upload_speed = 921600`.

`main.cpp` is a build-able stub: brings up native-USB serial, prints `DEVICE_ID`,
`FRAME_BASE_URL`, PSRAM size, flash size, free heap, then idles. The render loop / Wi-Fi /
frame client are TODO markers for the Firmware phase. (Not compiled here — `pio run` would
download the ESP32 platform/toolchain, deferred to the Firmware phase to avoid a large pull.)

---

## Step 5 — `secrets.h` with placeholders (gitignored)

Created `firmware/src/secrets.h` exactly as specified:

```c
#pragma once
#define WIFI_SSID "REPLACE_ME"
#define WIFI_PASS "REPLACE_ME"
#define FRAME_BASE_URL "http://LAPTOP_LAN_IP:8787"
#define DEVICE_ID "orb-01"
#define DEVICE_TOKEN "dev-local-token"
```

**Gitignore verification:**

```bash
git check-ignore firmware/src/secrets.h   # -> firmware/src/secrets.h  (IGNORED ✓)
```

The repo's `.gitignore` already has both `secrets.h` and an explicit `firmware/src/secrets.h`
line. A `git add --dry-run firmware/src/` confirms only `main.cpp` would be staged —
`secrets.h` is excluded. ✓ No real WiFi/token values were written.

> Reminder for the operator: before flashing, copy the laptop LAN IP into `FRAME_BASE_URL`
> → `http://192.168.1.148:8787`, and fill in `WIFI_SSID` / `WIFI_PASS`. The `DEVICE_ID` /
> `DEVICE_TOKEN` placeholders already match the dev server defaults.

---

## Step 6 — Detect laptop LAN IP

```bash
ipconfig getifaddr en0 || ipconfig getifaddr en1
```

- `en0` → **`192.168.1.148`**
- `en1` → empty (no IP; Wi-Fi/primary is on en0)

**The device should poll `http://192.168.1.148:8787`.** Substitute this for
`LAPTOP_LAN_IP` in `secrets.h` before flashing. (IPs from DHCP can change — re-run this
command if pairing later fails to reach the server.)

---

## Final state

```
agentlamp/
├── .venv/                          # gitignored — toolchain (pio 6.1.19, esptool 5.2.0, fastapi, pytest …)
├── server/
│   └── agentlamp_server/
│       ├── __init__.py
│       └── app.py                  # FastAPI frame server (scaffold, contract-aligned)
├── firmware/
│   ├── platformio.ini              # ESP32-S3R8 / 8MB OPI PSRAM / 16MB flash, native USB CDC
│   ├── README.md                   # (pre-existing placeholder)
│   └── src/
│       ├── main.cpp                # scaffold entry, serial diag
│       └── secrets.h               # placeholders, GITIGNORED
└── docs/devlog/01-setup.md         # this file
```

`git status --porcelain` (untracked only — **nothing committed**, per instructions):

```
?? docs/ui/mockups/      (pre-existing, not from this task)
?? firmware/platformio.ini
?? firmware/src/         (main.cpp only would stage; secrets.h ignored)
?? server/
```

---

## Problems & fixes

| # | Problem | Resolution |
|---|---------|------------|
| 1 | `esptool` warns `flash_id` is deprecated → use `flash-id`. | Cosmetic; command still works. Used the task-specified `flash_id`; noted `flash-id` is the new form for future runs. |
| 2 | `ipconfig getifaddr en1` returns nothing (non-zero exit). | Expected — Wi-Fi is on `en0`. The `||` fallback is correct; en0 gave `192.168.1.148`. |
| 3 | Repo already ships a `src/` tree (collector/cloud/admin placeholders) distinct from the task's `server/` package. | Followed the task spec: scaffolded `server/agentlamp_server/`, left `src/` untouched. Flagged here so the Server phase reconciles the two layouts. |
| 4 | Host base `python3` is 3.14.3 (very new). | venv built fine; all wheels resolved (cp314 wheels available for httptools/esptool/etc). No fallback needed. |

## Notes for the next phases

- **Server phase:** `server/agentlamp_server/app.py` is a contract-true scaffold (auth, schema
  negotiation, error envelope). Replace the static frame with the real generator
  (priority scoring + quota risk + 2 KB-budget fleet truncation per `docs/cloud/cloud_contract.md`).
  Run locally: `.venv/bin/python -m agentlamp_server.app` (from `server/`) or
  `.venv/bin/uvicorn agentlamp_server.app:app --host 0.0.0.0 --port 8787` — binds 8787 to match the contract.
- **Firmware phase:** `pio run` will pull the espressif32 platform + toolchain on first build
  (deliberately deferred here). PSRAM is confirmed present (8 MB) so the framebuffer budget holds.
  Verify the pin map against the official Waveshare wiki for **revision v0.2** before driving the LCD.
- **No flash/erase was performed** and **nothing was committed** — both per instructions.
