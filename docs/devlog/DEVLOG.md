# AgentLamp — Master Development Journal

**Project:** AgentLamp — a desk orb (Waveshare ESP32-S3-LCD-1.47B) that displays the
live status of your Codex / Claude coding agents, driven by a local LAN frame server.
**Build window:** 2026-05-30 (single day, three phases).
**Mode:** Local-mode-first — no cloud, no domain, no TLS, no HMAC ingest. The device
polls a Python frame server on the laptop over the LAN.
**Repo:** `/Users/hulu/huluman/agentlamp` (git, branch `main`).
**Overall outcome:** SUCCESS end-to-end on the bench — server runs and serves valid
schema-v1 frames (105/105 tests pass), firmware compiles cleanly (`firmware.bin` 689 KB,
RAM 8.5% / Flash 10.5%), board probed and confirmed. **Not yet flashed; nothing committed.**
The remaining step is the physical flash + bring-up — see
[`FLASH-RUNBOOK.md`](FLASH-RUNBOOK.md).

---

## Per-phase journal index

| Phase | Journal | What it covers | Result |
|-------|---------|----------------|--------|
| 01 — Setup | [`01-setup.md`](01-setup.md) | venv + toolchain, board probe, scaffolding, `secrets.h`, LAN IP | SUCCESS |
| 02 — Server impl | [`02-server-impl.md`](02-server-impl.md) | collector + local LAN frame server, default-deny sanitizer, fixtures, simulator | SUCCESS (105 tests) |
| 02 — Server review | [`02-server-review.md`](02-server-review.md) | Claude + Codex (gpt-5.5) chain review of the server | REVISE → all fixed |
| 03 — Firmware impl | [`03-firmware-impl.md`](03-firmware-impl.md) | ESP32-S3 firmware: WiFi → poll → validate → render LCD + RGB LED | SUCCESS (compiles) |
| 03 — Firmware review | [`03-firmware-review.md`](03-firmware-review.md) | Claude + Codex (gpt-5-codex) chain review of the firmware | REVISE → all fixed |

---

## The whole build, chronologically

### Phase 01 — Toolchain & scaffolding

Stood up the Python toolchain so both later phases could build, then probed the real board
read-only.

1. **venv + packages.** `python3 -m venv .venv` (host base Python **3.14.3**), then
   `pip install platformio esptool fastapi "uvicorn[standard]" httpx pydantic pytest`.
   Key versions: PlatformIO **6.1.19**, esptool **5.2.0**, FastAPI **0.136.3**,
   uvicorn **0.40.0**, pydantic **2.13.4**, pytest **9.0.3**. `.venv/` is gitignored.
2. **Board probe (READ-ONLY, no erase/write).**
   `python -m esptool --chip esp32s3 --port /dev/cu.usbmodem1101 flash_id` — connected on
   the **first try, no BOOT-button hold**. Confirmed the spec exactly:
   ESP32-S3 (QFN56, rev v0.2), **8 MB embedded OPI PSRAM**, **16 MB flash**, MAC
   `44:1b:f6:86:59:68`, native USB-Serial/JTAG. PSRAM present matters: the firmware's
   framebuffer budget assumes it.
3. **Scaffolding.** Created `server/agentlamp_server/` (a contract-true FastAPI scaffold:
   Bearer auth + schema negotiation + error envelope, static frame) and `firmware/`
   (`platformio.ini` pinned to the S3R8 / 8 MB OPI PSRAM / 16 MB flash, native USB CDC;
   a serial-diag `main.cpp` stub).
4. **`secrets.h`** created with placeholders (`WIFI_SSID`/`WIFI_PASS=REPLACE_ME`,
   `FRAME_BASE_URL=http://LAPTOP_LAN_IP:8787`, `DEVICE_ID=orb-01`,
   `DEVICE_TOKEN=dev-local-token`) and **verified gitignored** (`git check-ignore` →
   ignored; never holds real values).
5. **LAN IP detected.** `ipconfig getifaddr en0` → **`192.168.1.148`** (en1 empty). This is
   the address the device must poll: `http://192.168.1.148:8787`.

> Note carried forward: the repo also ships a placeholder `src/` tree
> (`collector`/`cloud`/`admin` READMEs) distinct from the real `server/` package; it was
> left untouched per the task spec.

### Phase 02 — Collector + local LAN frame server

Replaced the scaffold `app.py` with the real generator. Local mode means the collector
itself owns aggregation + priority + frame generation and serves the frame over the LAN —
no cloud hop. Pipeline:

```
provider event envelope (admin/event inject, or future Codex/Claude hook adapter)
        │
        ▼  sanitize.py   default-deny: enum | user-alias | keyed-HMAC; recursive unknown-field reject
        ▼  state.py      materialized sessions + quota; liveness TTL; priority scoring; scene selection; 2 KB trim
        ▼  app.py        FastAPI: GET /frame (Bearer) · POST /pair · POST /admin/* · GET /preview
        ▼  preview.py    live 172×320 simulator, renders the EXACT frame JSON, polls every 3 s
```

Highlights:

- **Frame Schema v1** served with exactly the contract keys (`v, device_id, scene,
  headline, primary{…}, fleet[], quota[], accent, ttl, seq, server_time`). Bearer auth,
  **token never in the URL**, token stored only as a SHA-256 hash. Schema negotiation
  returns `min(server, requested)`. Error envelope `{"error","retry"}` per the table
  (`401 bad_token`, `404 unknown_device`, `503 frame_unavailable`).
- **The sanitizer is the product's trust claim — default-deny.** A raw signal becomes a
  controlled enum, a user-controlled alias, or a keyed-HMAC label — never a guess. Unmapped
  cwd → `project-<hmac6>` (never a directory basename, never plain `sha256("main")`); real
  model ids collapse to the provider enum; error strings with a path/secret drop to
  `unknown`; any unknown field or raw-leak key (`cwd`, `prompt`, `transcript_path`, …)
  **rejects the whole event**. Audit is counts-only (reason + payload hash, never the value).
- **Priority + scene rules verbatim from `cloud_contract.md`** (WAITING +100, ERROR +90,
  CODING +70, … with low-quota/pinned/stale modifiers). Codex + Claude share one queue.
  Scene precedence: heartbeat-lost → offline → alert (WAITING/ERROR/quota ≥ 90 %) →
  all-offline → stale → all-idle → sleep → else focus.
- **2 KB hard cap** enforced server-side; liveness TTL STALE 120 s / OFFLINE 600 s;
  `seq` increments only on content change.
- **Live simulator** at `/preview` (self-contained, no CDNs) renders from the real frame
  JSON, shows payload byte size with an over-2 KB warning, and has inject buttons.

First pass: **85/85 tests** pass; live `curl` against `python -m agentlamp_server`
captured a real 438-byte alert frame.

### Phase 03 — ESP32-S3 firmware

Closed the loop on-device: WiFi join → poll the frame API every ~4 s with a Bearer token →
validate (size / schema / unknown-field) → render the design-board scenes on the ST7789
172×320 → drive the onboard WS2812 to the status accent.

- **Hardware validated, not guessed.** Board is the **B (non-touch)** variant → **ST7789**
  driver (the Touch variant's JD9853 config was explicitly avoided). Pin map + panel config
  pulled from a **proven working LovyanGFX repo** and cross-checked against the **espp BSP**
  and a **TFT_eSPI** discussion — all three agree. The scaffold's **BL=46 was wrong**; every
  source says **48** (46 would leave the backlight off → dark screen). The **column offset
  of 34** is mandatory: the ST7789 has a 240-wide GRAM but the glass exposes only a 172-wide
  window centred in it (`(240−172)/2 = 34`).
- **Module split** (single-responsibility): `theme.h` (palette + enums), `display.h`
  (LovyanGFX panel — the offsets live here), `led.h` (WS2812 wrapper, ~25 % brightness cap),
  `frame.h` (bounded-buffer parser, ArduinoJson v7), `renderer.h` (one method per scene),
  `main.cpp` (WiFi + poll loop + failure/stale state machine).
- **Frame validation:** reject body > 2 KB, reject unknown `v`, ignore unknown *fields*
  (forward-compatible), all strings copied into fixed `char[]` (no heap in the model),
  arrays capped fleet ≤ 6 / quota ≤ 2.
- **State machine:** poll every 4 s, 2 s HTTP timeout; 3 consecutive failures → Offline
  (last good frame retained); staleness from local `millis()` (never RTC vs `server_time`);
  401/403/404 → "PAIRING REQUIRED" + stop polling; 429 → back off honoring `Retry-After`.
  Never prints the token or full body over serial.

First pass: `pio run` **SUCCESS on the first attempt**, 0 errors / 0 warnings,
`firmware.bin` 689 KB, RAM 7.9 % / Flash 10.5 %.

---

## The two chain reviews (multi-AI, cross-model)

Each phase ended with an independent chain review (Claude chain-reviewer + a fresh Codex run),
in line with the project's "council before mandatory" discipline. Both verdicts were **REVISE,
not FAIL** — green build, correct core, fixable gaps. Every finding was reproduced before it
was touched, and false positives were proven false rather than blindly "fixed".

### Server review (`02-server-review.md`) — Codex gpt-5.5, reasoning=high

Codex independently re-ran the suite (85 passed in its venv too). It found **3 P0 + 6 P1**,
all reproduced by the chain reviewer with a minimal script against the repo `.venv`.
**Zero Codex false positives** on the server.

| ID | What was caught | Fix | Proof |
|----|-----------------|-----|-------|
| **P0.1** | Aliases were **not** default-deny on the *event pipeline* — `project_alias="client-acme-prod"` and `"a"*3000` emitted verbatim (the fixtures only tested the `project_alias()` function, never the emit path). | Added a positive shape gate (`looks_like_neutral_alias` + `coerce_alias`) that HMAC-collapses anything not positively neutral. | `client-acme-prod` → `project-<hmac>` (no leak); `a`*3000 → 14 chars; neutral aliases survive. New pipeline fixtures + live curl. |
| **P0.2** | 2 KB cap **not guaranteed** — `_enforce_byte_cap` trimmed only quota/fleet; a 3000-char `project_alias` → 3261-byte frame. | Clamp the primary string fields (longest-first, halving to a floor with `…`) as a last resort. | A directly-injected 5000-char Session → 1826-byte frame; new regression. |
| **P0.3** | Pairing token **leak** — `POST /pair` returned the dev token for `orb-01` on any/absent code, violating the one-time-code contract. | Require a valid issued one-time code for **every** device incl. the dev device; otherwise `400 bad_pairing_code`. | Bogus/absent → 400; issued code redeems once then replays as 400. Live curl. |
| **P1.1** | WAITING/ERROR alert could be **suppressed by priority modifiers** (low-quota +30 / pinned +50 made CODING win → `focus`). | `_select_scene` now scans **all** sessions for the interrupt condition, not just `ordered[0]`. | Both repros now yield `alert`; ERROR too. |
| **P1.2** | Quota danger **ignored with no sessions** (`if not ordered: return sleep` ran first). | Quota-danger check moved **before** the no-session sleep branch. | quota 0.95 + zero sessions → `alert`/`red`. |
| **P1.3** | Malformed `X-Frame-Schema-Version: abc` → FastAPI default `422 {detail}` (not the contract envelope); non-int raised raw `ValueError`. | Coerce the header defensively (garbage → server default); wrap `int()` so it raises `SanitizationError` → mapped to the `{error,retry}` 503. | `abc` header → 200 v=1; `build_frame(…,"not-an-int")` → `SanitizationError`. |
| **P1.4** | Schema drift — quota emitted only one of `w5`/`week`; `fleet_more` appeared top-level but the test whitelisted it (exactness never enforced). | Merge per-window quota into one record carrying both `w5`+`week`; document `fleet_more` as an optional top-level key; remove the test escape hatch. | Two-window account → single merged entry; exact key-set assertion. |

After fixes: **105 passed** (was 85; +20 regression tests), every fix re-proven on a live server.

### Firmware review (`03-firmware-review.md`) — Codex gpt-5-codex, fresh

The reviewer **confirmed the COMPILE claim without re-running** (inspected `firmware.bin`
timestamp + `strings` for the exact scene literals + verified every LovyanGFX/ArduinoJson
symbol exists in the installed libs) and **independently verified the panel config against the
espp BSP** (all 7 pins match). Found **2 P0 + several P1**. Notably, **two Codex findings were
proven FALSE POSITIVES** and the firmware was left unchanged with evidence.

| ID | What was caught | Verdict | Fix |
|----|-----------------|---------|-----|
| **P0-1** | Oversized-body guard **bypassed for chunked / no-Content-Length** responses: `getSize()` returns `-1`, `-1 > 2048` is false, so `getString()` streams the whole body into an unbounded heap `String` → could OOM the ESP32 before the post-read check. | **REAL** (traced through `HTTPClient.cpp`) | Replaced `getString()` with a bounded streamed read via `getStreamPtr()` into a fixed `static char buf[2049]`, bailing at > 2048 — RAM bounded regardless of Content-Length. |
| **P0-2** | Doc/firmware contradiction: firmware BL=**48**, docs (BUILD.md / contract) said **46**. Codex called 48 a "dark-screen bug". | **FALSE POSITIVE — firmware is correct at 48** (espp BSP lists `backlight_io = GPIO_NUM_48`). | Fixed the **stale docs** to 48; firmware untouched. |
| **P1-1** | SoftAP captive portal + NVS provisioning not implemented (creds are compile-time `secrets.h`). | REAL but **known-deferred** (devlog §8). | Tracked as an open acceptance gap; no code change. The scene/branch are in place so the portal drops in later. |
| **P1-2** | WiFiConfig / join-failed setup scene **immediately overwritten by Boot** on the first `loop()` (the "join AgentLamp-XXXX / 192.168.4.1" instructions vanish on a fresh device). | **REAL** (material first-boot UX bug) | Added a `provisioningHalt` latch; `loop()` early-returns while latched, keeps reconnecting, clears on join. |
| **P1-3** | `Retry-After` silently ignored on 429 (`http.header()` returns "" without `collectHeaders()`). | **REAL** | `http.collectHeaders({"Retry-After"}, 1)` before `GET()`. |
| **P1-4** | Offline/Stale "last seen Ns ago" + uptime clock **frozen** (repaint gate only fired on scene/seq change). | **REAL** | Coarse 1 s tick forces a repaint while parked in Offline/Stale only; Live scenes keep anti-flicker. |
| **P1-5** | `cfg.invert=true` (LGFX ref) vs espp BSP `invert_colors=false`. | **UNRESOLVABLE without hardware** | Left `true` (matches the proven LGFX reference). **Verify on first flash** — if colours are photo-negative, flip it. |
| **P1-6** | Column `offset_x=34` would be wrong at `setRotation(2)` (180°). | **REAL but DORMANT** (firmware only ever calls `setRotation(0)`). Codex's "wrong axis vs espp" is a **FALSE POSITIVE** (cross-library convention). | No change; comment/flag only. |
| **P1-7** | A relay-mode `https://` URL would fall into unverified TLS. | **REAL (latent)** — v1 is `http://`. | Guard rejects any `https://` base URL until `WiFiClientSecure` + pinned CA lands. |
| **P1-8** | `consecutiveFails` (`uint8_t`) wraps at 255 (~17 min) → momentary drop out of Offline. | **REAL (very low sev)** | Clamped: `if (consecutiveFails < 255) consecutiveFails++;`. |
| **P1-9** | Build not reproducible: floating `platform` + caret lib ranges. | **REAL (minor)** | Pinned `platform = espressif32@7.0.1` + exact lib versions (ArduinoJson 7.4.3, LovyanGFX 1.2.21, NeoPixel 1.15.5). |

After fixes: recompile **SUCCESS**, 0 errors / 0 warnings, RAM 8.5 % (the +2 KB is the new
fixed body buffer moved off the heap), Flash 10.5 % unchanged.

---

## Final state (bench, pre-flash)

| Thing | State |
|-------|-------|
| **Server runnable?** | **Yes.** `python -m agentlamp_server` serves schema-v1 frames; **105/105 tests pass**; live curl verified every endpoint. Default bind `0.0.0.0:8787`. |
| **Firmware compiles?** | **Yes.** `pio run` SUCCESS, 0/0 errors/warnings; `firmware/.pio/build/waveshare-s3-lcd-147/firmware.bin` = **689 KB** present. RAM 8.5 % / Flash 10.5 %. |
| **Board** | Waveshare ESP32-S3-LCD-1.47**B** — ESP32-S3R8, 8 MB OPI PSRAM, 16 MB flash, MAC `44:1b:f6:86:59:68`. |
| **Serial port** | `/dev/cu.usbmodem1101` (native USB CDC-on-boot). |
| **Laptop LAN IP** | `192.168.1.148` (en0) → device polls `http://192.168.1.148:8787`. |
| **Flashed?** | **No** — flash is the next step. |
| **Committed?** | **No** — `git status` shows `server/`, `firmware/`, `docs/devlog/` untracked + 3 modified docs (BUILD.md, device_frame_api.md, firmware_contract.md). |

### Known open gaps (deferred by scope, not bugs)

- **WiFi SoftAP captive portal + NVS provisioning** — stubbed; today's path is creds in
  `secrets.h` → reflash (contract acceptance gap, tracked).
- **NTP wall clock** — top-bar clock is uptime mm:ss (staleness uses `millis()`, so cosmetic).
- **Relay-mode TLS** (`WiFiClientSecure` + pinned root CA) — local mode is plain HTTP over
  LAN by design; `https://` is hard-rejected until it lands.
- **Persistence + 30-day retention purge** on the server — in-memory state is rebuildable
  from events, so the DB swap is a drop-in later step.
- **`cfg.invert` (firmware)** — `true` is a best-guess from the LGFX reference; confirm on
  first flash (flip if photo-negative).

### Next step

Flash + bring up on the real board. Exact inline commands and the physical verification
checklist are in **[`FLASH-RUNBOOK.md`](FLASH-RUNBOOK.md)**.
