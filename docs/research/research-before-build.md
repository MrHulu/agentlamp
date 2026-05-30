# Research Before Build

> ⚠️ **Historical (pre-reframe).** Conclusions that assume a cloud-centric architecture are
> **superseded** by the 2026-05-30 local-mode-first reframe (see `../architecture/architecture.md`).
> Hardware/library findings below remain valid; treat any "cloud aggregation is required"
> framing as outdated.

Date: 2026-05-29

## Sources Checked

- Official hardware wiki: https://www.waveshare.net/wiki/ESP32-S3-LCD-1.47B
- Arduino ESP32 Wi-Fi docs: https://docs.espressif.com/projects/arduino-esp32/en/latest/api/wifi.html
- ESPP Waveshare ESP32-S3 LCD 1.47 board notes: https://esp-cpp.github.io/espp/ws_s3_lcd_1_47.html
- Python HMAC docs: https://docs.python.org/3/library/hmac.html
- FastAPI header parameter docs: https://fastapi.tiangolo.com/tutorial/header-params/
- OpenAI Codex hooks docs: https://developers.openai.com/codex/hooks
- OpenAI Codex config reference: https://developers.openai.com/codex/config-reference
- OpenAI Codex CLI command reference: https://developers.openai.com/codex/cli/reference
- Claude Code hooks docs: https://code.claude.com/docs/en/hooks
- Claude Code monitoring/OTel docs: https://code.claude.com/docs/en/monitoring-usage
- GitHub search:
  - `gh search repos 'ESP32-S3-LCD-1.47B' --limit 10`
  - `gh search repos 'Waveshare ESP32-S3-LCD-1.47' --limit 10`
  - `gh search repos 'Claude Code usage monitor ESP32' --limit 10`
  - `gh search repos 'Codex CLI hooks OpenTelemetry monitor' --limit 10`

## Hardware Findings

The target board is suitable for the spec:

- ESP32-S3 with Wi-Fi/BLE.
- 1.47 inch TFT display at 172x320.
- ST7789 display controller.
- RGB LED on GPIO38.
- LCD pins from official wiki: MOSI GPIO45, SCLK GPIO40, CS GPIO42, DC GPIO41, RST GPIO39, backlight GPIO46.
- Official docs mention Arduino and ESP-IDF workflows; Arduino ESP32 board package requirement is 3.0.2 or newer.

## Candidate References

| Candidate | Useful For | Decision |
|-----------|------------|----------|
| Waveshare official ESP32-S3-LCD-1.47B wiki | Pinout, official board resources, sample direction | Keep as hardware source of truth |
| ESPP `WsS3Lcd147` docs | Board abstraction examples, LVGL display/LED/button ideas | Use as reference only; do not switch MVP to ESP-IDF/espp yet |
| `rootedlab-code/claude-code-usage-monitor` | Similar physical Claude usage monitor concept | Do not fork; it is Claude-specific and local-usage-focused, while this project needs multi-provider cloud aggregation |
| `StanleyChanH/ESP32-S3-LCD-1.47B-MicroPython-Implementation` | Board bring-up clues and MicroPython display approach | Reject for MVP runtime; MicroPython adds TLS/UI uncertainty for this product |
| `mylesdebastion/waveshare-esp32-s3-lcd-1.47` | RGB/Wi-Fi/ESP-IDF compatibility notes | Reference for firmware risks only; not the architecture |
| `ahmadrezarazian/Waveshare-ESP32-S3-LCD1.47-TinyCryptoFlow` | LovyanGFX-style polished data display | Reference visual/animation patterns only; domain and API model differ |
| `xmedgcop/Clawdmeter` | Claude Code usage monitor with ESP32 direction | Do not fork; use only as prior-art signal because this project needs Codex + Claude + cloud aggregation |
| `yagooluz-sudo/claudiometro` | Claude Code usage monitor with desktop/ESP32 direction | Do not fork; same single-provider limitation |
| Codex hooks/config docs | Official Codex local lifecycle hooks and history settings | Use hooks as primary source; avoid `history.jsonl` parsing |
| Claude Code hooks/OTel docs | Official Claude lifecycle hooks and redacted telemetry controls | Use hooks first; OTel only with prompt/tool detail gates disabled |

## Provider Findings

- Codex now has lifecycle hooks including `UserPromptSubmit`, `PreToolUse`, `PermissionRequest`, `PostToolUse`, and `Stop`; these are a better integration surface than local transcript parsing.
- Codex config exposes `history.persistence`, which confirms local history is a transcript store. It is not a safe default source for this product.
- Codex CLI can emit JSON for some commands, including non-interactive runs and cloud task listing; this is useful for future wrappers, not required for MVP.
- Claude Code hooks provide JSON input with session, `transcript_path`, `cwd`, and event-specific fields. The adapter must redact `transcript_path`, `cwd`, prompt text, file paths, and tool payloads.
- Claude Code OTel can export metrics/events, but prompt and tool detail/content logging are opt-in. This project must keep those gates disabled by default.

## Selected Direction

Use the original spec's stack:

- Cloud: FastAPI + PostgreSQL + Redis + Caddy.
- Collector: Python + httpx + pydantic + keyring/platformdirs.
- Firmware: PlatformIO + Arduino-ESP32 3.x + LovyanGFX or LVGL + ArduinoJson + WiFiClientSecure.

The first implementation should not depend on provider quota scraping. Use manual/mock quota until signing, sanitization, and frame rendering are proven.

Provider implementation should start with Codex and Claude hook sinks, not transcript scrapers.
