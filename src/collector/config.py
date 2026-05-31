"""Collector configuration: local paths, server base URL, pepper, alias map.

All collector state lives under ``$HOME`` (never in the repo): a per-machine
queue, dead-letter store, a keyed pepper, and the user's alias map. Nothing here
is a credential — the pepper is a *local* HMAC key that never leaves the machine
(it only makes opaque labels un-brute-forceable by a relay operator).

This module also bridges to the server's sanitizer (``agentlamp_server.sanitize``)
by putting ``<repo>/server`` on ``sys.path`` — the collector REUSES that module
rather than reinventing redaction (kickoff GOTCHA #2).
"""
from __future__ import annotations

import os
import pathlib
import sys

# --------------------------------------------------------------------------- #
# Repo / package locations.
# --------------------------------------------------------------------------- #
# config.py lives at <repo>/src/collector/config.py
_THIS = pathlib.Path(__file__).resolve()
SRC_DIR = _THIS.parents[1]            # <repo>/src
REPO_ROOT = _THIS.parents[2]          # <repo>
SERVER_DIR = REPO_ROOT / "server"     # <repo>/server (holds agentlamp_server)

# Make the server package importable so we can REUSE its sanitizer.
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from agentlamp_server import sanitize as S  # noqa: E402  (after sys.path tweak)

# --------------------------------------------------------------------------- #
# Local state paths (all under $HOME, overridable by env for tests).
# --------------------------------------------------------------------------- #
def _expand(p: str) -> pathlib.Path:
    return pathlib.Path(os.path.expanduser(p))


HOME_STATE = _expand(os.environ.get("AGENTLAMP_HOME", "~/.agentlamp"))
QUEUE_DIR = _expand(os.environ.get("AGENTLAMP_QUEUE_DIR", str(HOME_STATE / "queue")))
DEAD_LETTER_DIR = _expand(
    os.environ.get("AGENTLAMP_DEAD_LETTER_DIR", str(HOME_STATE / "dead_letter"))
)
CONFIG_DIR = _expand(os.environ.get("AGENTLAMP_CONFIG_DIR", "~/.config/agentlamp"))
PEPPER_FILE = _expand(os.environ.get("AGENTLAMP_PEPPER_FILE", str(CONFIG_DIR / "pepper")))
ALIAS_FILE = os.environ.get("AGENTLAMP_ALIAS_FILE", str(CONFIG_DIR / "aliases.toml"))

# --------------------------------------------------------------------------- #
# Server target. The daemon and the server are the SAME machine — POST over
# loopback (127.0.0.1), never the LAN IP (proxy-bypass research: loopback is
# immune to LAN/router/Clash-route quirks and lowest latency).
# --------------------------------------------------------------------------- #
SERVER_BASE = os.environ.get("AGENTLAMP_SERVER_BASE", "http://127.0.0.1:8787").rstrip("/")

# Neutral account label (GOTCHA #5: never an email or plan tier).
ACCOUNT = os.environ.get("AGENTLAMP_ACCOUNT", "main")

# Local single-owner lamp (default): show the REAL, readable project name (the cwd
# basename) on the orb instead of an opaque HMAC hash. The HMAC aliasing exists to
# protect a cwd from a *cloud relay operator* — in local mode the only viewer is the
# owner at their own desk, so hashing their own folder names just makes the lamp
# unreadable. Set AGENTLAMP_LOCAL_LABELS=0 to force HMAC labels (e.g. relay mode).
LOCAL_LABELS = os.environ.get("AGENTLAMP_LOCAL_LABELS", "1") == "1"

# Daemon timing.
DRAIN_INTERVAL_S = float(os.environ.get("AGENTLAMP_DRAIN_INTERVAL_S", "0.5"))
HEARTBEAT_INTERVAL_S = float(os.environ.get("AGENTLAMP_HEARTBEAT_INTERVAL_S", "30"))

# Bounded queue (collector_contract.md → Offline Cache: bounded, drop-oldest+log).
# A transport failure (server down) does NOT dead-letter — the record is retried
# indefinitely; the queue stays bounded by these caps so it can never grow without
# limit while holding raw hook JSON at rest.
MAX_QUEUE_FILES = int(os.environ.get("AGENTLAMP_MAX_QUEUE_FILES", "5000"))
QUEUE_TTL_S = float(os.environ.get("AGENTLAMP_QUEUE_TTL_S", "3600"))   # drop undrained records older than 1h
TMP_TTL_S = float(os.environ.get("AGENTLAMP_TMP_TTL_S", "60"))         # reap orphaned *.tmp (SIGKILL'd hook)
MAX_DEAD_LETTER_FILES = int(os.environ.get("AGENTLAMP_MAX_DEAD_LETTER_FILES", "1000"))


# --------------------------------------------------------------------------- #
# Pepper: env override, else a persisted per-machine key (created 0600), else
# ephemeral. The DAEMON's pepper only needs to be stable across its OWN restarts
# (so the same cwd maps to the same opaque label) — the server passes neutral
# labels through unchanged regardless of its pepper.
# --------------------------------------------------------------------------- #
def load_pepper() -> bytes:
    env = os.environ.get("AGENTLAMP_PEPPER_HEX")
    if env:
        try:
            b = bytes.fromhex(env)
            if b:
                return b
        except ValueError:
            pass
    try:
        if PEPPER_FILE.is_file():
            data = PEPPER_FILE.read_text().strip()
            b = bytes.fromhex(data)
            if b:
                return b
    except (ValueError, OSError):
        pass
    # Generate + persist a fresh pepper (0600).
    import secrets

    b = secrets.token_bytes(32)
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        # Write with restrictive perms from the start.
        fd = os.open(str(PEPPER_FILE), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(fd, b.hex().encode("ascii"))
        finally:
            os.close(fd)
    except OSError:
        pass  # ephemeral pepper for this run if we cannot persist
    return b


def load_aliases() -> "S.AliasMap":
    """Load the user's alias map (cwd/account -> neutral label). Missing -> empty
    (unmapped cwd then becomes ``project-<hmac6>``, never a basename)."""
    try:
        return S.load_alias_map(ALIAS_FILE)
    except Exception:
        return S.AliasMap()


def ensure_dirs() -> None:
    # 0700: the queue holds raw hook JSON transiently — keep it owner-only, and
    # tighten an already-existing dir (mkdir won't narrow an existing one).
    for d in (HOME_STATE, QUEUE_DIR, DEAD_LETTER_DIR):
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
