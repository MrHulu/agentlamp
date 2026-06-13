#!/usr/bin/env python3
"""USB-CDC frame bridge — push the local server's device frame DOWN the serial cable.

For a USB-tethered desk lamp this replaces WiFi entirely: the Mac that already powers the lamp
over USB also feeds it frames over the same cable, so the lamp works on ANY network (or none) —
no per-location WiFi setup. The firmware (``main.cpp::readUsbFrame``) reads one compact JSON
frame per ``\\n`` from its serial RX and renders it; USB-CDC is full-duplex so this Mac→device
direction never collides with the firmware's TX log output.

Transport relationship:
  collector.daemon : hook queue → POST /admin/event   (feeds the server, unchanged)
  agentlamp_server : builds the frame                  (unchanged)
  usb_bridge (this): GET /frame → write to /dev/cu.usbmodem*   (server → cable)
The firmware prefers USB when frames arrive; WiFi stays a dormant fallback.

Run:   cd <repo>/src && ../.venv/bin/python -m collector.usb_bridge
Env:   AGENTLAMP_SERIAL_PORT     (default: first /dev/cu.usbmodem* / ttyACM* / ttyUSB*)
       AGENTLAMP_SERVER_BASE     (default http://127.0.0.1:8787)
       AGENTLAMP_DEV_DEVICE_ID   (default orb-01)   AGENTLAMP_DEV_DEVICE_TOKEN (default dev-local-token)
       AGENTLAMP_USB_INTERVAL_S  (default 2)

NOTE: this process holds the serial port open. Stop it before flashing the firmware
(`launchctl bootout … com.hulu.agentlamp.usbbridge`, or kill it), then restart.
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time
import urllib.request

SERVER = os.environ.get("AGENTLAMP_SERVER_BASE", "http://127.0.0.1:8787").rstrip("/")
DEVICE = os.environ.get("AGENTLAMP_DEV_DEVICE_ID", "orb-01")
TOKEN = os.environ.get("AGENTLAMP_DEV_DEVICE_TOKEN", "dev-local-token")
INTERVAL = float(os.environ.get("AGENTLAMP_USB_INTERVAL_S", "2"))
BAUD = 115200

# Proxy-bypass opener — a system proxy (Clash etc.) must NEVER intercept loopback (same rule as
# the daemon's netpost). We never touch the system proxy; we just refuse to route through it.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _log(msg: str) -> None:
    print(f"[agentlamp-usb {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def _find_port() -> str | None:
    p = os.environ.get("AGENTLAMP_SERIAL_PORT")
    if p:
        return p
    cands = sorted(
        glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/tty.usbmodem*")
        + glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*")
    )
    return cands[0] if cands else None


def _fetch_frame_line() -> bytes | None:
    """GET the device frame and return it as a single compact ``\\n``-terminated JSON line."""
    req = urllib.request.Request(
        f"{SERVER}/api/v1/device/{DEVICE}/frame",
        headers={"Authorization": f"Bearer {TOKEN}", "X-Frame-Schema-Version": "1"},
    )
    try:
        with _OPENER.open(req, timeout=3) as resp:
            body = resp.read()
        # Re-serialize compact so it is exactly one line (firmware reads per-newline) and stays
        # well under the firmware's 2 KB frame cap; a parse failure here means don't send junk.
        return json.dumps(json.loads(body), separators=(",", ":")).encode("utf-8") + b"\n"
    except Exception:
        return None


def run() -> int:
    import serial  # pyserial

    ser = None
    last_seen_log = 0.0
    _log(f"start: server={SERVER} device={DEVICE} interval={INTERVAL}s")
    while True:
        try:
            if ser is None or not ser.is_open:
                port = _find_port()
                if not port:
                    _log("no serial port (device unplugged?) — retrying in 2s")
                    time.sleep(2)
                    continue
                ser = serial.Serial(port, BAUD, timeout=0.2, write_timeout=2)
                _log(f"opened {port}")

            # Drain + surface the firmware's TX (so its buffer never fills and we can confirm it
            # is rendering USB frames). Log only the confirmation lines, throttled.
            try:
                if ser.in_waiting:
                    chunk = ser.read(ser.in_waiting).decode("utf-8", "replace")
                    now = time.time()
                    if now - last_seen_log > 10:
                        for ln in chunk.splitlines():
                            if "via=usb" in ln or "transport" in ln:
                                _log(f"device: {ln.strip()}")
                                last_seen_log = now
                                break
            except OSError:
                pass

            line = _fetch_frame_line()
            if line:
                ser.write(line)
        except OSError as exc:
            _log(f"serial error: {exc!r} — reopening")
            try:
                if ser:
                    ser.close()
            except Exception:
                pass
            ser = None
            time.sleep(1)
            continue
        time.sleep(INTERVAL)


def main() -> int:
    try:
        import serial  # noqa: F401
    except ImportError:
        _log("pyserial not installed in this venv (pip install pyserial)")
        return 1
    try:
        return run()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
