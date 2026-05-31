#!/usr/bin/env python3
"""AgentLamp collector daemon — drains the hook queue, drives the orb.

Long-running loop:
  1. scan ``~/.agentlamp/queue`` for completed ``*.json`` records (skip ``*.tmp``),
     oldest first,
  2. normalize each into the server's neutral shorthand (REUSING the server
     sanitizer for cwd -> alias),
  3. POST it to ``/admin/event`` over loopback, BYPASSING any system proxy,
  4. on success delete the file; on a server rejection (422) quarantine it to
     ``dead_letter/`` (reason + payload hash only, never raw); on a transport
     failure (server down/restarting) LEAVE the record and retry on the next loop
     forever — the reaper, not a retry cap, bounds the queue, so a server restart
     loses nothing,
  5. heartbeat ``/admin/heartbeat`` at least every HEARTBEAT_INTERVAL_S so the
     collector is never marked offline during idle periods.

Run:
    cd <repo>/src && ../.venv/bin/python -m collector.daemon
    # or one-shot drain (tests / cron):  ... -m collector.daemon --once
"""
from __future__ import annotations

import json
import os
import pathlib
import signal
import sys
import time

# Bootstrap: make ``collector`` importable whether run as a module or a script.
_SRC = pathlib.Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from collector import config, netpost  # noqa: E402
from collector.config import S  # noqa: E402
from collector.normalize import normalize_record  # noqa: E402

_STOP = False


def _handle_signal(signum, frame):  # noqa: ARG001
    global _STOP
    _STOP = True


def _log(msg: str) -> None:
    """Stderr log. Diagnostics only — callers pass clean labels, never raw
    paths/commands/prompts."""
    ts = time.strftime("%H:%M:%S")
    print(f"[agentlamp-daemon {ts}] {msg}", file=sys.stderr, flush=True)


def _quarantine(path: pathlib.Path, reason: str, payload_hash: str = "") -> None:
    """Move a poison record to dead_letter with reason + hash ONLY (never raw)."""
    try:
        config.DEAD_LETTER_DIR.mkdir(parents=True, exist_ok=True)
        meta = {"reason": reason, "payload_hash": payload_hash, "at": time.time(), "src": path.name}
        dest = config.DEAD_LETTER_DIR / f"{path.stem}.reason.json"
        dest.write_text(json.dumps(meta, separators=(",", ":")), encoding="utf-8")
    except OSError:
        pass
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _queue_files() -> list[pathlib.Path]:
    if not config.QUEUE_DIR.exists():
        return []
    # Only completed records; skip in-flight *.tmp. Oldest first (ts-prefixed name).
    return sorted(p for p in config.QUEUE_DIR.glob("*.json") if p.is_file())


def drain_once(pepper: bytes, aliases) -> dict:
    """Process all currently-completed queue files once. Returns counts.

    Failure policy:
      * unreadable / normalize-crashing (poison) record → dead-letter + drop (a
        single poison record must NEVER stall the loop),
      * server 422 (sanitization reject) → dead-letter (never retry — it can't pass),
      * transport failure / unexpected status (server down/restarting) → LEAVE the
        record and retry on the next loop; the reaper bounds the queue so it can
        never grow without limit (so a 5 s or 30 s server restart loses nothing).
    """
    counts = {"posted": 0, "heartbeat": 0, "rejected": 0, "requeued": 0, "dropped": 0}
    for path in _queue_files():
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _quarantine(path, "unreadable_record")
            counts["dropped"] += 1
            continue

        # Guard normalize: a poison record (e.g. a non-string tool_name) must be
        # quarantined, never raised into the loop where it would stall the queue.
        try:
            result = normalize_record(record, pepper=pepper, aliases=aliases)
        except Exception as exc:  # noqa: BLE001 — defense in depth
            _quarantine(path, f"normalize_error:{type(exc).__name__}")
            counts["dropped"] += 1
            _log(f"drop (normalize error {type(exc).__name__})")
            continue

        if result.action != "post":
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            counts["heartbeat" if result.action == "heartbeat" else "dropped"] += 1
            continue

        try:
            status_code, body = netpost.post_json(
                f"{config.SERVER_BASE}/admin/event", result.event
            )
        except netpost.PostError:
            counts["requeued"] += 1   # server unreachable → leave + retry (reaper bounds)
            continue

        if status_code == 200:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                _log(f"WARN: post ok but unlink failed ({path.name}) — will retry/reap")
            counts["posted"] += 1
        elif status_code == 422:
            reason = (body or {}).get("reason", "sanitization_failed")
            ph = (body or {}).get("payload_hash", "")
            _quarantine(path, str(reason), str(ph))
            counts["rejected"] += 1
            _log(f"reject (server 422 {reason}): {result.diag}")
        else:
            counts["requeued"] += 1   # transient 5xx → leave + retry
    return counts


def reap(now: float) -> dict:
    """Bound the queue + dead-letter AT REST (collector_contract.md → bounded cache):
    delete orphaned ``*.tmp`` (a SIGKILL'd hook), drop ``*.json`` older than the TTL
    or beyond the count cap (oldest first, logged), and cap the dead-letter store."""
    reaped = {"tmp": 0, "aged": 0, "overflow": 0, "dead_letter": 0}

    for tmp in config.QUEUE_DIR.glob("*.tmp"):
        try:
            if now - tmp.stat().st_mtime > config.TMP_TTL_S:
                tmp.unlink(missing_ok=True)
                reaped["tmp"] += 1
        except OSError:
            pass

    jsons: list[tuple[pathlib.Path, float]] = []
    for p in config.QUEUE_DIR.glob("*.json"):
        try:
            jsons.append((p, p.stat().st_mtime))
        except OSError:
            pass
    for p, mt in jsons:
        if now - mt > config.QUEUE_TTL_S:
            try:
                p.unlink(missing_ok=True)
                reaped["aged"] += 1
            except OSError:
                pass
    alive = [(p, mt) for (p, mt) in jsons if p.exists()]
    if len(alive) > config.MAX_QUEUE_FILES:
        alive.sort(key=lambda x: x[1])  # oldest first
        for p, _mt in alive[: len(alive) - config.MAX_QUEUE_FILES]:
            try:
                p.unlink(missing_ok=True)
                reaped["overflow"] += 1
            except OSError:
                pass

    dls: list[tuple[pathlib.Path, float]] = []
    for p in config.DEAD_LETTER_DIR.glob("*.reason.json"):
        try:
            dls.append((p, p.stat().st_mtime))
        except OSError:
            pass
    if len(dls) > config.MAX_DEAD_LETTER_FILES:
        dls.sort(key=lambda x: x[1])
        for p, _mt in dls[: len(dls) - config.MAX_DEAD_LETTER_FILES]:
            try:
                p.unlink(missing_ok=True)
                reaped["dead_letter"] += 1
            except OSError:
                pass
    return reaped


def _heartbeat() -> bool:
    try:
        code, _ = netpost.post_empty(f"{config.SERVER_BASE}/admin/heartbeat")
        return code == 200
    except netpost.PostError:
        return False


def run() -> int:
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    config.ensure_dirs()
    pepper = config.load_pepper()
    aliases = config.load_aliases()

    _log(f"start: queue={config.QUEUE_DIR} server={config.SERVER_BASE} "
         f"aliases={config.ALIAS_FILE}")
    last_heartbeat = 0.0
    last_reap = 0.0
    while not _STOP:
        counts = drain_once(pepper, aliases)
        now = time.time()
        # Reap the at-rest queue/dead-letter periodically (not every fast loop).
        if now - last_reap >= 10:
            reaped = reap(now)
            if any(reaped.values()):
                _log("reap " + " ".join(f"{k}={v}" for k, v in reaped.items() if v))
            last_reap = now
        # Posting an event already refreshes the server's collector heartbeat;
        # send an explicit heartbeat only when idle past the interval.
        if counts["posted"] == 0 and (now - last_heartbeat) >= config.HEARTBEAT_INTERVAL_S:
            if _heartbeat():
                last_heartbeat = now
        elif counts["posted"] > 0:
            last_heartbeat = now
        if any(v for k, v in counts.items() if k in ("posted", "rejected", "dropped")):
            _log("drain " + " ".join(f"{k}={v}" for k, v in counts.items() if v))
        time.sleep(config.DRAIN_INTERVAL_S)
    _log("stopped")
    return 0


def run_once() -> int:
    config.ensure_dirs()
    pepper = config.load_pepper()
    aliases = config.load_aliases()
    counts = drain_once(pepper, aliases)
    reap(time.time())
    _heartbeat()
    _log("drain-once " + " ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--once" in argv:
        return run_once()
    return run()


if __name__ == "__main__":
    sys.exit(main())
