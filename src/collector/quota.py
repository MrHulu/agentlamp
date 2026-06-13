"""Fetch real Claude Code subscription usage (5h + weekly) from Anthropic's OAuth usage endpoint.

This is the **same authoritative source** Claude Code's own ``/usage`` command reads — the real
plan-limit utilization, NOT a token-count estimate. We read the local OAuth access token (the one
Claude Code already manages) and ``GET /api/oauth/usage``; the response carries ``five_hour`` and
``seven_day`` utilization percentages (0-100) plus reset timestamps.

TOKEN HANDLING (deliberately read-only / non-rotating): we only READ the current access token from
the macOS login Keychain (item ``Claude Code-credentials``, written by Claude Code), or from an
``AGENTLAMP_OAUTH_TOKEN`` env override. We NEVER refresh or rewrite it — a refresh would rotate
Claude Code's own refresh token and could log the user out. If the token is missing / near-expiry /
rejected we simply skip the cycle (the gauge keeps its last value) until Claude Code refreshes the
token on its own cadence.

PRIVACY: the response is pure usage telemetry (percentages + reset times). No transcript content,
prompts, file paths, or commands are read. The emitted quota event carries only
``provider + account + window_type + used_ratio``.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_KEYCHAIN_SERVICE = "Claude Code-credentials"
_OAUTH_BETA = "oauth-2025-04-20"
_TIMEOUT_S = 15.0
_EXPIRY_SKEW_S = 60.0  # treat a token expiring within 60s as already gone (avoid mid-flight 401)

# --- Codex (OpenAI / ChatGPT subscription) ---------------------------------------------------- #
# OpenAI exposes NO live GET usage endpoint (all 403). Codex DOES persist the rate-limit snapshot
# it last saw from the backend into its session rollout files (~/.codex/sessions/**/rollout-*.jsonl)
# as a record carrying {primary, secondary} windows with `used_percent` + `resets_at`. We read the
# freshest such snapshot — real data, as fresh as the user's last Codex run (which is frequent).
_CODEX_SESSIONS_DIR = "~/.codex/sessions"
_CODEX_MAX_FILE_BYTES = 64 * 1024 * 1024
_CODEX_MAX_SCAN_FILES = 8          # only the few newest rollout files can hold the latest snapshot
_CODEX_MAX_AGE_S = 14 * 24 * 3600  # ignore snapshots older than 14d (well past the weekly window)
# (mtime, size) -> last rate_limit dict in a rollout file. A finished rollout never changes, so a
# long-running daemon parses each file at most once.
_CODEX_FILE_CACHE: dict[str, tuple[float, int, dict | None]] = {}


def _claude_plan(oauth: dict) -> str:
    """Derive the *precise* Claude subscription tier from the OAuth blob.

    ``subscriptionType`` is coarse ("max" covers both Max 5× and Max 20×). The finer variant lives in
    ``rateLimitTier`` (e.g. ``default_claude_max_20x`` → ``max_20x``, ``default_claude_max_5x`` →
    ``max_5x``, ``default_claude_pro`` → ``pro``). We strip the ``default_claude_`` / ``default_``
    prefix and use what remains; fall back to ``subscriptionType`` if the field is missing/unparseable.
    """
    tier = str(oauth.get("rateLimitTier") or "").strip().lower()
    if tier:
        for prefix in ("default_claude_", "default_"):
            if tier.startswith(prefix):
                tier = tier[len(prefix):]
                break
        if tier:
            return tier
    return str(oauth.get("subscriptionType") or "").strip().lower()


def _read_oauth_blob(now: float | None = None) -> dict | None:
    """Return ``{"token": <access token>, "plan": <tier>}`` for the Claude Code OAuth cred, or None.

    Lookup order: ``AGENTLAMP_OAUTH_TOKEN`` env override → macOS Keychain. Read-only; never refreshes
    or rewrites the credential. ``plan`` is the precise tier from ``rateLimitTier`` (e.g. "max_20x"),
    falling back to coarse ``subscriptionType`` ("max"), "" if absent.
    """
    env = os.environ.get("AGENTLAMP_OAUTH_TOKEN", "").strip()
    if env:
        return {"token": env, "plan": os.environ.get("AGENTLAMP_CLAUDE_PLAN", "").strip().lower()}
    if sys.platform != "darwin":
        return None
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        blob = json.loads(proc.stdout)
    except ValueError:
        return None
    oauth = blob.get("claudeAiOauth", blob)
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    if not token:
        return None
    exp = oauth.get("expiresAt")
    if isinstance(exp, (int, float)):
        now = time.time() if now is None else now
        if (exp / 1000.0) - now <= _EXPIRY_SKEW_S:
            return None
    return {"token": str(token), "plan": _claude_plan(oauth)}


def _read_oauth_token(now: float | None = None) -> str | None:
    """Back-compat thin wrapper: just the access token (used by ``fetch_usage`` default path)."""
    blob = _read_oauth_blob(now)
    return blob["token"] if blob else None


def _parse_reset_epoch(block: object) -> int | None:
    """Extract an epoch-seconds reset time from a usage window block's ``resets_at`` (ISO string)."""
    if not isinstance(block, dict):
        return None
    raw = block.get("resets_at")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return int(raw)
    if isinstance(raw, str) and raw:
        try:
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def fetch_usage(token: str | None = None, *, now: float | None = None) -> dict | None:
    """GET the OAuth usage endpoint. Returns the parsed dict, or None on any failure."""
    token = token or _read_oauth_token(now)
    if not token:
        return None
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": _OAUTH_BETA,
        "User-Agent": "agentlamp-collector",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except (urllib.error.URLError, ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _util_ratio(block: object) -> float | None:
    """Map an endpoint window block ``{"utilization": 0-100, ...}`` to a 0..1 ratio, or None."""
    if not isinstance(block, dict):
        return None
    u = block.get("utilization")
    if not isinstance(u, (int, float)) or isinstance(u, bool):
        return None
    return max(0.0, min(1.0, float(u) / 100.0))


def compute_claude_quota(now: float | None = None, *, account_alias: str = "main") -> list[dict]:
    """Return up to two REAL Claude usage windows (5h, week) for ``relaypost.push_quota``.

    Each dict::

        {"provider": "claude", "account_alias": <neutral>, "window_type": "5h"|"week",
         "used_ratio": <0..1>, "confidence": "high", "is_estimated": False}

    Returns ``[]`` when usage is unavailable (no / expired token, network error, malformed body) —
    the daemon then skips the cycle and the previously-pushed value persists in the frame.
    """
    blob = _read_oauth_blob(now)
    if not blob:
        return []
    usage = fetch_usage(blob["token"], now=now)
    if not usage:
        return []
    base = {"provider": "claude", "account_alias": account_alias,
            "confidence": "high", "is_estimated": False, "plan": blob.get("plan", "")}
    out: list[dict] = []
    fh = usage.get("five_hour")
    w5 = _util_ratio(fh)
    if w5 is not None:
        out.append({**base, "window_type": "5h", "used_ratio": w5, "reset_at": _parse_reset_epoch(fh)})
    sd = usage.get("seven_day")
    week = _util_ratio(sd)
    if week is not None:
        out.append({**base, "window_type": "week", "used_ratio": week, "reset_at": _parse_reset_epoch(sd)})
    return out


def _find_rate_limit(obj: object) -> dict | None:
    """Recursively locate a Codex rate-limit dict ({primary, secondary} with used_percent)."""
    if isinstance(obj, dict):
        rl = obj.get("rate_limits") or obj.get("rate_limit")
        if isinstance(rl, dict) and ("primary" in rl or "secondary" in rl):
            return rl
        if ("primary" in obj or "secondary" in obj) and any(
            isinstance(obj.get(k), dict) and "used_percent" in obj[k] for k in ("primary", "secondary")
        ):
            return obj
        for v in obj.values():
            found = _find_rate_limit(v)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_rate_limit(v)
            if found is not None:
                return found
    return None


def _file_last_rate_limit(path: pathlib.Path, mtime: float, size: int) -> dict | None:
    """Last rate-limit snapshot in a rollout file, cached by (mtime, size)."""
    key = str(path)
    c = _CODEX_FILE_CACHE.get(key)
    if c is not None and c[0] == mtime and c[1] == size:
        return c[2]
    last: dict | None = None
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if "rate_limit" not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                rl = _find_rate_limit(rec)
                if rl is not None:
                    last = rl
    except OSError:
        last = None
    _CODEX_FILE_CACHE[key] = (mtime, size, last)
    return last


def _read_codex_rate_limit(now: float) -> tuple[dict, float] | None:
    """Return ``(rate_limit_dict, snapshot_mtime)`` from the freshest rollout file, or None."""
    root = pathlib.Path(os.path.expanduser(
        os.environ.get("AGENTLAMP_CODEX_SESSIONS_DIR", _CODEX_SESSIONS_DIR)
    ))
    if not root.exists():
        return None
    candidates: list[tuple[float, int, pathlib.Path]] = []
    for p in root.rglob("rollout-*.jsonl"):
        try:
            stt = p.stat()
        except OSError:
            continue
        if stt.st_size > _CODEX_MAX_FILE_BYTES or stt.st_mtime < now - _CODEX_MAX_AGE_S:
            continue
        candidates.append((stt.st_mtime, stt.st_size, p))
    candidates.sort(reverse=True)  # newest mtime first
    for mtime, size, p in candidates[:_CODEX_MAX_SCAN_FILES]:
        rl = _file_last_rate_limit(p, mtime, size)
        if rl is not None:
            return rl, mtime
    return None


def _codex_window_ratio(block: object) -> float | None:
    if not isinstance(block, dict):
        return None
    u = block.get("used_percent")
    if not isinstance(u, (int, float)) or isinstance(u, bool):
        return None
    return max(0.0, min(1.0, float(u) / 100.0))


def _confidence_for_age(age_s: float) -> str:
    if age_s <= 2 * 3600:
        return "high"
    if age_s <= 24 * 3600:
        return "medium"
    return "low"


def compute_codex_quota(now: float | None = None, *, account_alias: str = "main") -> list[dict]:
    """Return up to two REAL Codex usage windows (5h, week) for ``relaypost.push_quota``.

    Read from Codex's freshest local session snapshot (OpenAI has no live endpoint). ``primary`` is
    the 5h window, ``secondary`` the weekly window (mapped by ``window_minutes``). ``confidence``
    degrades with snapshot age; ``is_estimated`` is False (it's Codex's own real reading, just aged).
    Returns ``[]`` when no recent snapshot exists.
    """
    now = time.time() if now is None else now
    found = _read_codex_rate_limit(now)
    if found is None:
        return []
    rl, mtime = found
    confidence = _confidence_for_age(max(0.0, now - mtime))
    plan = str(rl.get("plan_type") or "").strip().lower() if isinstance(rl, dict) else ""
    base = {"provider": "codex", "account_alias": account_alias,
            "confidence": confidence, "is_estimated": False, "plan": plan}
    out: list[dict] = []
    for key in ("primary", "secondary"):
        block = rl.get(key)
        ratio = _codex_window_ratio(block)
        if ratio is None:
            continue
        wm = block.get("window_minutes") if isinstance(block, dict) else None
        window = "5h" if (isinstance(wm, (int, float)) and wm <= 360) else "week"
        reset = block.get("resets_at") if isinstance(block, dict) else None
        reset_at = int(reset) if isinstance(reset, (int, float)) and not isinstance(reset, bool) and reset > 0 else None
        out.append({**base, "window_type": window, "used_ratio": ratio, "reset_at": reset_at})
    return out


def compute_quota(now: float | None = None, *, account_alias: str = "main") -> list[dict]:
    """Combined real quota across providers (Claude live endpoint + Codex session snapshot).

    Each provider contributes independently; a provider that is unavailable simply yields no events
    (its last value persists in the frame). Never raises — a provider failure can't break the daemon.
    """
    out: list[dict] = []
    for fn in (compute_claude_quota, compute_codex_quota):
        try:
            out.extend(fn(now, account_alias=account_alias))
        except Exception:  # noqa: BLE001 — one provider's failure must not drop the other / break daemon
            continue
    return out


if __name__ == "__main__":  # one-shot: print live usage windows
    print(json.dumps({
        "claude_usage": fetch_usage(),
        "quota": compute_quota(),
    }, indent=2))
