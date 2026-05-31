# Build & Quickstart

Goal: take a stranger from zero to a working AgentLamp orb in **local mode** (no cloud, no
domain, no certificate). Relay mode is the optional advanced step at the end.

## Bill of Materials

| Item | Notes |
|------|-------|
| Waveshare ESP32-S3-LCD-1.47B | 1.47" rectangular 172×320 LCD (rounded corners) + RGB LED. **Confirm the revision has PSRAM** (the framebuffer needs it — see `firmware/firmware_contract.md` → Memory Budget). Record the exact revision you used here. |
| USB-C cable | Data-capable (not charge-only) for flashing |
| A computer running Codex and/or Claude CLI | The collector reads their lifecycle hooks |
| (optional) 3D-printed or off-the-shelf enclosure | Cosmetic |

> Buy by searching the exact board name above (Waveshare store or your usual retailer).
> Confirm PSRAM + the exact revision **when you flash the firmware**, not before — the
> framebuffer needs PSRAM (see `firmware/firmware_contract.md` → Memory Budget).

## Wiring / Pins

The LCD + RGB are on-board on this Waveshare module; no hand-wiring is required. The pins the
firmware drives (verify against the official Waveshare wiki for your revision):

| Signal | GPIO |
|--------|------|
| LCD MOSI | 45 |
| LCD SCLK | 40 |
| LCD CS | 42 |
| LCD DC | 41 |
| LCD RST | 39 |
| LCD backlight | 48 |
| RGB LED | 38 |

## Local-mode quickstart (cloud-free)

1. **Install the collector** (Python 3.11+):
   ```bash
   git clone <repo-url> && cd agentlamp
   python3 -m venv .venv && . .venv/bin/activate
   pip install -e ./src/collector            # (package name finalized during implementation)
   cp .env.example .env                       # then edit — see below
   ```
2. **Run the local frame server + browser simulator** (no device needed yet):
   ```bash
   agentlamp serve --local --bind 0.0.0.0:8787
   # open http://localhost:8787/preview to see the 172x320 simulator
   ```
3. **Feed it a manual event** to confirm the pipeline:
   ```bash
   agentlamp emit --status CODING --project project-a --account main
   # the simulator should update within a couple seconds
   ```
4. **Flash the firmware** (PlatformIO):
   ```bash
   cd firmware && pio run -t upload
   ```
   On first boot the device starts a **SoftAP captive portal** (see
   `firmware/firmware_contract.md` → WiFi Provisioning). Connect to it, enter your WiFi SSID
   + password, and the **base URL** `http://<your-laptop-LAN-ip>:8787`.
5. **Pair the device**: create a device in the local admin / CLI, get the one-time pairing
   code, enter it in the portal. The device exchanges it for a read-only token and starts
   polling. The orb now mirrors your agents.
6. **Wire up the providers**: add the Codex/Claude hook entries (see
   `providers/codex_adapter.md` / `providers/claude_adapter.md`). The hook is fire-and-forget;
   it writes to a local queue and returns instantly.

## Relay mode (optional, for remote viewing)

Only if you want to see the orb when it's not on the same LAN as your laptop. You will need a
small public host (a $5 VPS is enough), a domain, and TLS. Then:
`agentlamp serve --relay …` on the host, point the collector at it with a signed collector
key, and re-pair the device with the relay base URL. Everything the relay sees is the
sanitized metadata listed in `security/threat_model.md`. **Do not host a relay for other
people** — v1 is single-owner.

## Verify before you trust it

Run the sanitizer fixtures (see `security/sanitization_policy.md` → Required Fixtures) and
confirm an unmapped project never emits a basename, before pointing the collector at real work.
