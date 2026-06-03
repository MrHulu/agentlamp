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

# --------------------------------------------------------------------------- #
# Relay mode (I3: host/kid/secret are NEVER hardcoded — env/keyring only).
#
# Local mode (default): the daemon POSTs shorthand bodies to SERVER_BASE/admin/event
# over loopback. RELAY mode instead signs each batch and POSTs it to a remote
# Cloudflare relay at RELAY_HOST/api/v1/collectors/{kid}/events.
#
#   AGENTLAMP_RELAY_HOST   — e.g. https://relay.example.com   (presence enables relay)
#   AGENTLAMP_RELAY_KID    — the active collector key id (selects the signing secret)
#   AGENTLAMP_COLLECTOR_ID — the neutral collector id in the URL ([A-Za-z0-9_-]{1,64})
#   AGENTLAMP_RELAY_SECRET — the signing secret (TEST/CI override; prod reads the keyring)
#
# A relay deployment forces HMAC labels (no readable folder names leak to the cloud);
# enroll sets AGENTLAMP_LOCAL_LABELS=0 accordingly.
# --------------------------------------------------------------------------- #
# Relay config file (enroll writes it; read DIRECTLY here, no POSIX `source` needed).
# devlog/16 MED #5: the POSIX `relay.env` only works on a shell that sources it —
# useless on Windows / cron / a bare daemon launch. So enroll ALSO writes a portable
# ``relay.json`` and config reads it here at import. ENV ALWAYS WINS (tests/CI + an
# explicit override stay authoritative); the file only fills the gaps so a fresh
# daemon launch picks up the enrolled relay config with nothing sourced.
RELAY_CONFIG_FILE = _expand(
    os.environ.get("AGENTLAMP_RELAY_CONFIG_FILE", str(CONFIG_DIR / "relay.json"))
)


def _load_relay_config_file() -> dict:
    """Read ``relay.json`` (enroll's portable, source-free relay config). Missing /
    malformed → empty dict (env-only operation is still fine). Never raises."""
    import json
    try:
        if RELAY_CONFIG_FILE.is_file():
            data = json.loads(RELAY_CONFIG_FILE.read_text(encoding="utf-8") or "{}")
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        pass
    return {}


_RELAY_FILE = _load_relay_config_file()


def _relay_setting(env_key: str, file_key: str, default: str = "") -> str:
    """Resolve a relay setting: env var WINS, else the relay.json file, else default."""
    v = os.environ.get(env_key, "")
    if v:
        return v
    fv = _RELAY_FILE.get(file_key, "")
    return str(fv) if fv else default


RELAY_HOST = _relay_setting("AGENTLAMP_RELAY_HOST", "relay_host").rstrip("/")
RELAY_KID = _relay_setting("AGENTLAMP_RELAY_KID", "kid")
COLLECTOR_ID = _relay_setting("AGENTLAMP_COLLECTOR_ID", "collector_id") or ACCOUNT
# Explicit mode override (env or file); otherwise relay is on iff a host is configured.
_mode = (os.environ.get("AGENTLAMP_MODE", "") or str(_RELAY_FILE.get("mode", ""))).strip().lower()
RELAY_MODE = (_mode == "relay") or (_mode != "local" and bool(RELAY_HOST))
# Test/CI secret override (prod loads from the OS keyring via secretstore).
RELAY_SECRET_ENV = os.environ.get("AGENTLAMP_RELAY_SECRET", "")


def relay_secret() -> bytes | None:
    """The collector signing secret for relay mode: env override (tests/CI) else the
    OS keyring entry keyed by the active kid. None if neither is present."""
    if RELAY_SECRET_ENV:
        return RELAY_SECRET_ENV.encode("utf-8")
    if not RELAY_KID:
        return None
    try:
        from . import secretstore
        v = secretstore.get_secret(RELAY_KID)
        return v.encode("utf-8") if v else None
    except Exception:
        return None

# Local single-owner lamp (default): show the REAL, readable project name (the cwd
# basename) on the orb instead of an opaque HMAC hash. The HMAC aliasing exists to
# protect a cwd from a *cloud relay operator* — in local mode the only viewer is the
# owner at their own desk, so hashing their own folder names just makes the lamp
# unreadable. Set AGENTLAMP_LOCAL_LABELS=0 to force HMAC labels (e.g. relay mode).
#
# Resolution: explicit env wins; else in RELAY mode default to HMAC labels (0) so a
# source-free relay launch (config driven by relay.json, devlog/16 MED #5) never
# leaks readable folder names to the cloud even when nothing sourced the env fragment.
if "AGENTLAMP_LOCAL_LABELS" in os.environ:
    LOCAL_LABELS = os.environ.get("AGENTLAMP_LOCAL_LABELS", "1") == "1"
else:
    LOCAL_LABELS = not RELAY_MODE

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
