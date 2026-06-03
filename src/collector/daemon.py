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
  5. heartbeat at least every HEARTBEAT_INTERVAL_S so the collector is never marked
     offline during idle periods — local mode hits ``/admin/heartbeat`` over loopback;
     RELAY mode pushes a SIGNED ``collector.heartbeat`` to the relay (P1, devlog/16),
     because the loopback heartbeat never reaches the cloud and an idle-but-present
     owner would otherwise flip the whole fleet offline.

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

from collector import config, netpost, relaypost  # noqa: E402
from collector.config import S  # noqa: E402
from collector.normalize import normalize_record  # noqa: E402

_STOP = False

# Relay-mode clock correction learned from a `stale_timestamp` 401 (server_time -
# local). Applied to the signed timestamp so a skewed local clock still signs an
# in-window request. Resynced AT MOST once per stale reject — never a tight loop.
_RELAY_CLOCK_OFFSET = 0.0


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

        if config.RELAY_MODE:
            _post_relay(path, result, counts, pepper=pepper, aliases=aliases)
        else:
            _post_local(path, result, counts)
    return counts


def _post_local(path: pathlib.Path, result, counts: dict) -> None:
    """Local mode (unchanged): POST the shorthand to /admin/event over loopback.

    200 → delete; 422 → dead-letter (never retry); transport/5xx → leave + retry."""
    try:
        status_code, body = netpost.post_json(f"{config.SERVER_BASE}/admin/event", result.event)
    except netpost.PostError:
        counts["requeued"] += 1   # server unreachable → leave + retry (reaper bounds)
        return

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


def _post_relay(path: pathlib.Path, result, counts: dict, *, pepper: bytes, aliases) -> None:
    """Relay mode: sign the shorthand into a 1-event batch + POST it to the relay.

    ``pepper`` + ``aliases`` drive the collector-side sanitizer (BUILD-SPEC I1): the
    relay push serializes the sanitizer OUTPUT (``display_title`` as ``title-<hmac>``,
    neutral aliases, canonical enums), so the cloud's VALIDATE-only gate accepts it.

    Failure policy (collector_ingest_api.md):
      * misconfig (no host/kid/secret) → leave + retry (a later enroll fixes it),
      * transport failure → leave + retry (reaper bounds the queue),
      * 401 stale_timestamp → resync clock ONCE from server_time, then leave + retry
        (the NEXT loop signs with the corrected offset — no tight loop here),
      * other request-level reject (bad_signature/revoked/...) → dead-letter (can't pass),
      * per-event ``rejected`` in results[] → dead-letter (reason + hash only), never retry,
      * accepted → delete.
    """
    global _RELAY_CLOCK_OFFSET
    secret = config.relay_secret()
    if not (config.RELAY_HOST and config.RELAY_KID and secret):
        counts["requeued"] += 1   # un-enrolled / partial config → wait for enroll
        return

    # Idempotency-Key: a retry of THIS record (same source filename) returns the prior
    # result without re-applying (the file name is the stable per-record anchor).
    idem = f"{config.COLLECTOR_ID}:{path.stem}"
    try:
        res = relaypost.push_batch(
            relay_host=config.RELAY_HOST, collector_id=config.COLLECTOR_ID,
            kid=config.RELAY_KID, secret=secret, shorthands=[result.event],
            pepper=pepper, aliases=aliases,
            clock_offset=_RELAY_CLOCK_OFFSET, idempotency_key=idem,
        )
    except netpost.PostError:
        counts["requeued"] += 1   # transport failure → leave + retry
        return

    if not res.ok:
        if res.http_status == 401 and res.reason == "stale_timestamp":
            # Resync ONCE from the server clock; do NOT loop — retry next drain.
            _RELAY_CLOCK_OFFSET = relaypost.resync_offset(res.server_time)
            counts["requeued"] += 1
            _log(f"relay stale_timestamp → resync offset={_RELAY_CLOCK_OFFSET:+.1f}s")
            return
        if res.http_status == 429:
            counts["requeued"] += 1   # rate limited → leave + retry (Retry-After)
            return
        # bad_signature / collector_revoked / payload_hash_mismatch / batch limits:
        # the record can never pass as-is → dead-letter (reason + hash only).
        digest = res.body.get("payload_hash", "") or _payload_hash(result.event)
        _quarantine(path, f"relay_{res.reason or res.http_status}", str(digest))
        counts["rejected"] += 1
        _log(f"relay reject (request {res.http_status} {res.reason}): {result.diag}")
        return

    # HTTP 200 — inspect per-event results (single-event batch).
    ev = (res.results or [{}])[0]
    status = str(ev.get("status", "accepted"))
    if status == "rejected":
        reason = str(ev.get("reason", "sanitization_failed"))
        _quarantine(path, f"relay_event_{reason}", _payload_hash(result.event))
        counts["rejected"] += 1
        _log(f"relay reject (event {reason}): {result.diag}")
        return
    # accepted (or duplicate) → drop the record.
    try:
        path.unlink(missing_ok=True)
    except OSError:
        _log(f"WARN: relay push ok but unlink failed ({path.name}) — will retry/reap")
    counts["posted"] += 1


def _payload_hash(event: dict) -> str:
    """sha256 of the shorthand body — for dead-letter accounting ONLY (never the raw value)."""
    import hashlib
    raw = json.dumps(event, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


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
    """Refresh the cloud's collector-liveness clock so an idle-but-present owner is
    never marked offline (P1, docs/devlog/16).

    Relay mode → push a SIGNED ``collector.heartbeat`` to the relay (the local
    ``/admin/heartbeat`` does not reach the cloud, so without this the relay's
    ``last_collector_heartbeat`` would go stale during idle and flip the fleet
    offline). Local mode → the loopback ``/admin/heartbeat`` (unchanged).

    A stale_timestamp on the relay heartbeat resyncs the SAME ``_RELAY_CLOCK_OFFSET``
    the drain path uses (once, no loop). Returns True iff the heartbeat landed."""
    if config.RELAY_MODE:
        return _relay_heartbeat()
    try:
        code, _ = netpost.post_empty(f"{config.SERVER_BASE}/admin/heartbeat")
        return code == 200
    except netpost.PostError:
        return False


def _relay_heartbeat() -> bool:
    """Sign + push a ``collector.heartbeat`` to the relay. Misconfig (no host/kid/
    secret) or transport failure → False (the next loop retries). A stale_timestamp
    resyncs the offset ONCE (shared with the drain path), no tight loop."""
    global _RELAY_CLOCK_OFFSET
    secret = config.relay_secret()
    if not (config.RELAY_HOST and config.RELAY_KID and secret):
        return False
    try:
        res = relaypost.push_heartbeat(
            relay_host=config.RELAY_HOST, collector_id=config.COLLECTOR_ID,
            kid=config.RELAY_KID, secret=secret, clock_offset=_RELAY_CLOCK_OFFSET,
        )
    except netpost.PostError:
        return False
    if res.ok:
        return True
    if res.http_status == 401 and res.reason == "stale_timestamp":
        _RELAY_CLOCK_OFFSET = relaypost.resync_offset(res.server_time)
        _log(f"relay heartbeat stale_timestamp → resync offset={_RELAY_CLOCK_OFFSET:+.1f}s")
    return False


def run() -> int:
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    config.ensure_dirs()
    pepper = config.load_pepper()
    aliases = config.load_aliases()

    if config.RELAY_MODE:
        _log(f"start: mode=relay queue={config.QUEUE_DIR} relay={config.RELAY_HOST} "
             f"collector_id={config.COLLECTOR_ID} kid={config.RELAY_KID or '(unset)'}")
    else:
        _log(f"start: mode=local queue={config.QUEUE_DIR} server={config.SERVER_BASE} "
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
