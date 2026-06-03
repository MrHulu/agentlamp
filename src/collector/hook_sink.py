#!/usr/bin/env python3
"""Fire-and-forget hook sink — the ONLY thing a provider hook runs.

Contract (collector_contract.md → Hook Ingestion + kickoff GOTCHA #3):
  * read the raw hook JSON from stdin,
  * append it atomically to ``~/.agentlamp/queue/<ts>.json``,
  * exit 0 in WELL under 1 second, with ZERO network I/O,
  * NEVER fail the host agent — any error is swallowed (exit 0).

The background daemon does all normalize / sanitize / POST work. This script is
self-contained (stdlib only, no package imports) so it stays fast and has no
import surface that could slow a tool call.

Usage (Claude settings.json / Codex config.toml hook command):
    python3 /abs/path/src/collector/hook_sink.py --provider claude
    python3 /abs/path/src/collector/hook_sink.py --provider codex

We print NOTHING to stdout and exit 0 — the passive-observer contract. For
PreToolUse / PermissionRequest that means "no decision, proceed normally"; we
never accidentally allow or deny a tool.
"""
from __future__ import annotations

import os
import pathlib
import sys
import time
import uuid


def _queue_dir() -> pathlib.Path:
    base = os.environ.get("AGENTLAMP_QUEUE_DIR")
    if base:
        return pathlib.Path(base)
    home = os.environ.get("AGENTLAMP_HOME", os.path.expanduser("~/.agentlamp"))
    return pathlib.Path(home) / "queue"


def _provider() -> str:
    # --provider X  (preferred) ; else env ; else "claude".
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--provider" and i + 1 < len(argv):
            return argv[i + 1].strip().lower()
        if a.startswith("--provider="):
            return a.split("=", 1)[1].strip().lower()
    return os.environ.get("AGENTLAMP_PROVIDER", "claude").strip().lower()


def _read_all_thread_deadline(seconds: float) -> bytes:
    """Cross-platform deadline read for when POSIX itimer/SIGALRM is unavailable (Windows,
    or not the main thread): read stdin in a daemon thread and ABANDON it if it blocks past
    the deadline. The daemon thread is reaped at process exit, so the <1s fire-and-forget
    guarantee holds even if the host keeps the stdin pipe open."""
    import threading

    box: dict[str, bytes] = {}

    def _reader() -> None:
        try:
            box["data"] = sys.stdin.buffer.read()
        except Exception:
            box["data"] = b""

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    t.join(seconds)
    return box.get("data", b"")


def _read_stdin_deadline(seconds: float = 0.8) -> bytes:
    """Read all of stdin, but never block past ``seconds`` — the <1s fire-and-forget
    guarantee must hold even if the host keeps the stdin pipe open."""
    if sys.stdin.isatty():
        return b""
    import signal

    def _on_alarm(signum, frame):  # noqa: ARG001
        raise TimeoutError

    try:
        signal.signal(signal.SIGALRM, _on_alarm)
        signal.setitimer(signal.ITIMER_REAL, seconds)
    except (ValueError, OSError, AttributeError):
        # Windows / non-main-thread: SIGALRM+itimer unavailable — use a real threaded
        # deadline instead of a plain blocking read (which could hang to the host's hook
        # timeout). Claude Code + Codex both run on Windows.
        return _read_all_thread_deadline(seconds)
    try:
        return sys.stdin.buffer.read()
    except (TimeoutError, Exception):
        return b""
    finally:
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
        except Exception:
            pass


def main() -> int:
    raw = _read_stdin_deadline()

    # Wrap the raw hook bytes into a record WITHOUT parsing/inspecting them — the
    # daemon parses + sanitizes. We only need the provider tag + a timestamp.
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:
        text = ""

    provider = _provider()
    # Build the queue line as a tiny JSON object whose "hook" value is the raw
    # JSON text embedded verbatim. We avoid importing json by hand-wrapping; but
    # json is stdlib and fast, so just use it for correctness.
    import json

    try:
        hook_obj = json.loads(text) if text.strip() else {}
    except Exception:
        hook_obj = {"_unparsed": True}
    rec = {"provider": provider, "received_at": time.time(), "hook": hook_obj}

    try:
        qd = _queue_dir()
        qd.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(qd, 0o700)  # owner-only: the raw hook JSON rests here briefly
        except OSError:
            pass
        ts_us = int(time.time() * 1_000_000)
        stem = f"{ts_us:016d}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        tmp = qd / f"{stem}.json.tmp"
        final = qd / f"{stem}.json"
        line = json.dumps(rec, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        # Atomic: write a unique .tmp in the SAME dir, then os.replace (atomic
        # rename on the same filesystem). The draining daemon only ever sees the
        # .tmp name or the complete final name — never a half-written file.
        fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(final))
    except Exception:
        # Never block / never raise into the host agent.
        try:
            tmp.unlink(missing_ok=True)  # type: ignore[name-defined]
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    # Always exit 0 — passive observer must never fail the agent.
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
