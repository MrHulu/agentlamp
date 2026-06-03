# 14 — USB-cable transport: the lamp no longer needs WiFi

> 2026-06-02. Boss took the laptop+lamp out, the lamp couldn't connect, and asked "did you
> hardcode the WiFi?". No — but it exposed the real design flaw: a desk lamp **tethered to the
> laptop by USB** that talks over **WiFi** needs that location's WiFi every time you move
> (home → office → out), and the Mac was now on ethernet (`192.168.202.x`, WiFi off) with no
> WiFi for the lamp to join at all. The fix: feed the lamp over the cable it's already plugged
> into. **No WiFi, works anywhere the laptop goes.**

## Not a hardcode (the accusation, settled)

WiFi SSID/password are NEVER in source (`config.h` says so; `provisioning.h` reads them from
the SoftAP setup form → NVS). The boot log proved it: `wifi: joining moza-office` — the office
network Boss had provisioned, not present at the new location. The mDNS change (devlog 12) is
the *server's* name and only runs after WiFi connects, so it can't cause a join failure.

## The transport

```
collector.daemon : hook queue → POST /admin/event   (unchanged)
agentlamp_server : builds the frame                  (unchanged)
usb_bridge (NEW) : GET /frame → write a compact JSON line to /dev/cu.usbmodem*   (server → cable)
firmware (NEW)   : readUsbFrame() reads one frame per line off USB-CDC RX, renders it
```
USB-CDC is full-duplex, so the Mac→device frame stream is independent of the firmware's TX log
output. The firmware **prefers USB**: a 3 s `probeUsbFrame` at boot — if the Mac is feeding, it
logs `transport: USB-CDC -> skipping WiFi` and never touches WiFi; `usbFresh()` keeps WiFi
dormant (no join attempts, no offline, no self-heal reboot) while USB frames arrive. WiFi (+ the
mDNS server discovery) remains the automatic fallback if USB stops for >12 s.

`src/collector/usb_bridge.py` runs as **launchd `com.hulu.agentlamp.usbbridge`** (RunAtLoad +
KeepAlive), so it's feeding before the device boots → the device comes straight up on USB.

## The bug that cost an hour (RX FIFO < frame size)

First attempt: device went silent on USB — `readUsbFrame` never caught a frame. Root cause: the
ESP32-S3 hardware USB-CDC **RX FIFO defaults to 256 bytes, but a frame line is ~464 bytes**, so
every frame was truncated before a complete `\n`-line could form. One-line fix:
`Serial.setRxBufferSize(FRAME_MAX_BYTES + 256)` **before** `Serial.begin()`. (Lesson: TX working
≠ RX working; size the RX buffer to the largest message.)

## Verified

- Boot log with the bridge feeding: `frame ok : via=usb scene=… seq=…` then
  `transport: USB-CDC (frames over the cable) -> skipping WiFi`. No WiFi join attempted.
- Bridge launchd service feeds continuously; the device confirms `via=usb` with a climbing seq.
- 50 collector + 147 server tests green (unchanged — this is firmware + a standalone bridge).

## Runbook

- The bridge **holds the serial port**. Before flashing the firmware:
  `launchctl bootout gui/$(id -u)/com.hulu.agentlamp.usbbridge`, flash, then
  `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hulu.agentlamp.usbbridge.plist`.
- Env knobs: `AGENTLAMP_SERIAL_PORT`, `AGENTLAMP_USB_INTERVAL_S` (default 2 s),
  `AGENTLAMP_SERVER_BASE`/`_DEV_DEVICE_ID`/`_DEV_DEVICE_TOKEN`.
- WiFi still works as fallback (provision via BOOT-hold → `AgentLamp-Setup` portal) — but for a
  USB-tethered lamp it's no longer needed.

## Residual / honest

- The lamp must be plugged into the Mac running the server (it is — that's its power). If
  unplugged, it falls back to WiFi (and the home/last-provisioned network).
- The bridge plist lives in `~/Library/LaunchAgents/` (not yet committed in-repo — see R6 ops,
  which should commit all three plists + a runbook).
