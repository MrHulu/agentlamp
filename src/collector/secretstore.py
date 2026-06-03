"""OS-keyring-backed secret store for the collector (stdlib-first, zero hard deps).

The enroll flow (I5) must persist the collector signing secret + ``kid`` so a new
computer is a one-line setup, and a revoke must be a one-line removal. The whole
collector is stdlib-only by design (every sibling module's docstring), and the repo
ships no dependency manifest — so this module does NOT hard-require ``keyring``.

Backend ladder (first that works wins), so the secret lands in the *real* OS
keyring on every owner platform:

  1. the ``keyring`` package, if it is importable (the canonical lib). This is
     the ONLY OS-keyring path on Windows: ``keyring`` ships a native Windows
     Credential Manager backend (``keyring.backends.Windows``, via ``pywin32``).
     There is NO stdlib ``security``/``secret-tool`` equivalent on Windows, so
     ``pip install keyring`` is REQUIRED there for an ACL-protected store —
     without it Windows drops to the file fallback below (see the warning).
  2. on POSIX only, the OS-native credential CLI — macOS ``security``, Linux
     ``secret-tool`` (libsecret) — invoked as a subprocess (no pip install).
  3. a fallback 0600 file under the config dir (same posture as the pepper file),
     used only when no OS keyring is reachable (headless CI / locked-down boxes).
     ⚠️ On Windows, ``os.chmod(..., 0o600)`` is a NO-OP — the file inherits the
     directory ACL and is NOT owner-restricted. ``set_secret`` emits a clear
     WARNING in that case so the secret is never silently left world-readable;
     install ``keyring`` to get a real protected store.

No host / account / machine name is ever hardcoded (I3): the service + account
labels are passed in by the caller. Secrets are returned/stored as UTF-8 strings.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

SERVICE = "agentlamp-collector"


class SecretStoreError(Exception):
    """A secret could NOT be stored in a protected backend. Raised (fail-closed) on
    Windows when the only reachable backend is the plaintext file fallback — the
    collector secret must never be silently written world-readable there (devlog/16
    MED #5). The fix is to ``pip install keyring`` (real Windows Credential Manager).
    """


def _try_keyring():
    try:
        import keyring  # type: ignore
        return keyring
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# macOS Keychain via the built-in `security` CLI (no dep).
# --------------------------------------------------------------------------- #
def _macos_set(service: str, account: str, secret: str) -> bool:
    """Store via the macOS ``security`` CLI WITHOUT the secret on argv.

    docs/devlog/16 MED #4: ``security add-generic-password -w <secret>`` puts the
    collector secret in the process argv, where any local user's ``ps``/proc listing
    can read it. The man page says ``-w`` placed LAST (no value) prompts for the
    password on STDIN — so we pass ``-w`` last and feed the secret via ``input=``.
    The secret never appears in argv; only ``-w`` (the flag) does.
    """
    try:
        # -U updates if present (idempotent); trailing -w (no value) → read from stdin.
        subprocess.run(
            ["security", "add-generic-password", "-U", "-s", service, "-a", account, "-w"],
            input=(secret + "\n").encode("utf-8"), check=True, capture_output=True,
        )
        return True
    except Exception:
        return False


def _macos_get(service: str, account: str) -> str | None:
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            check=True, capture_output=True, text=True,
        )
        v = out.stdout.strip()
        return v or None
    except Exception:
        return None


def _macos_del(service: str, account: str) -> bool:
    try:
        subprocess.run(
            ["security", "delete-generic-password", "-s", service, "-a", account],
            check=True, capture_output=True,
        )
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Linux libsecret via `secret-tool` (no dep).
# --------------------------------------------------------------------------- #
def _linux_set(service: str, account: str, secret: str) -> bool:
    try:
        subprocess.run(
            ["secret-tool", "store", "--label", f"{service}:{account}",
             "service", service, "account", account],
            input=secret.encode("utf-8"), check=True, capture_output=True,
        )
        return True
    except Exception:
        return False


def _linux_get(service: str, account: str) -> str | None:
    try:
        out = subprocess.run(
            ["secret-tool", "lookup", "service", service, "account", account],
            check=True, capture_output=True, text=True,
        )
        v = out.stdout.strip()
        return v or None
    except Exception:
        return None


def _linux_del(service: str, account: str) -> bool:
    try:
        subprocess.run(
            ["secret-tool", "clear", "service", service, "account", account],
            check=True, capture_output=True,
        )
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Fallback: a 0600 JSON file under the config dir (same posture as the pepper).
# --------------------------------------------------------------------------- #
def _fallback_path() -> pathlib.Path:
    # Imported lazily to honor the test env overrides config reads at import time.
    from . import config
    return pathlib.Path(config.CONFIG_DIR) / "secrets.json"


def _fallback_load() -> dict:
    p = _fallback_path()
    try:
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        pass
    return {}


def _fallback_save(data: dict) -> bool:
    p = _fallback_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(p), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(data, separators=(",", ":")).encode("utf-8"))
        finally:
            os.close(fd)
        try:
            # POSIX: enforces owner-only (0600). Windows IGNORES the mode bits
            # (os.chmod can only toggle the read-only flag), so this is a NO-OP
            # there — _warn_if_unprotected() surfaces that to the operator.
            os.chmod(p, 0o600)
        except OSError:
            pass
        return True
    except OSError:
        return False


def _is_windows() -> bool:
    return sys.platform.startswith("win") or os.name == "nt"


def _warn_if_unprotected() -> None:
    """On Windows the file fallback is NOT ACL-protected (chmod 0600 is a no-op),
    so the collector signing secret would sit world-readable. Tell the operator
    plainly + how to fix it. Never raises — a warning must not break enroll."""
    if not _is_windows():
        return
    try:
        p = _fallback_path()
    except Exception:  # config not importable for some reason; still warn generically
        p = None
    loc = f" at {p}" if p else ""
    print(
        "WARNING [agentlamp]: no OS keyring backend available on Windows; the "
        f"collector secret was written to a plaintext file{loc} that is NOT "
        "ACL-protected (chmod 0600 is a no-op on Windows). Install the keyring "
        "package for a real Windows Credential Manager store:  pip install keyring",
        file=sys.stderr,
    )


# --------------------------------------------------------------------------- #
# Public API (service/account passed in; nothing machine-specific hardcoded).
# --------------------------------------------------------------------------- #
def set_secret(account: str, secret: str, *, service: str = SERVICE,
               allow_insecure_file: bool = False) -> str:
    """Store ``secret`` under (service, account). Returns the backend used
    ('keyring' | 'os-keychain' | 'file'). Idempotent (overwrites).

    🚨 FAIL-CLOSED on Windows (devlog/16 MED #5): if NO OS keyring backend is
    reachable on Windows, the only fallback is a plaintext file that ``chmod 0600``
    cannot protect (Windows ignores POSIX mode bits), so the collector secret would
    sit world-readable. Rather than silently write it there, raise
    ``SecretStoreError`` — the operator must ``pip install keyring`` for a real
    Windows Credential Manager store. Pass ``allow_insecure_file=True`` ONLY to opt
    into the plaintext file knowingly (tests / a locked-down single-user box)."""
    kr = _try_keyring()
    if kr is not None:
        try:
            kr.set_password(service, account, secret)
            return "keyring"
        except Exception:
            pass
    if sys.platform == "darwin" and _macos_set(service, account, secret):
        return "os-keychain"
    if sys.platform.startswith("linux") and _linux_set(service, account, secret):
        return "os-keychain"
    # File fallback: no OS keyring reachable.
    if _is_windows() and not allow_insecure_file:
        # Fail closed — the file is NOT ACL-protected on Windows; never write the
        # secret world-readable behind the operator's back.
        raise SecretStoreError(
            "no OS keyring backend available on Windows; refusing to write the "
            "collector secret to an UNPROTECTED plaintext file (chmod 0600 is a no-op "
            "on Windows). Install the keyring package for a real Windows Credential "
            "Manager store:  pip install keyring  (or, on a locked-down single-user "
            "box, opt into the plaintext file with allow_insecure_file=True)."
        )
    # On Windows with the explicit opt-in, still warn the plaintext file is unprotected.
    _warn_if_unprotected()
    data = _fallback_load()
    data[f"{service}:{account}"] = secret
    _fallback_save(data)
    return "file"


def get_secret(account: str, *, service: str = SERVICE) -> str | None:
    """Fetch a secret; checks every backend in order so a value stored by any of
    them (across enroll runs on the same box) is found."""
    kr = _try_keyring()
    if kr is not None:
        try:
            v = kr.get_password(service, account)
            if v:
                return v
        except Exception:
            pass
    if sys.platform == "darwin":
        v = _macos_get(service, account)
        if v:
            return v
    if sys.platform.startswith("linux"):
        v = _linux_get(service, account)
        if v:
            return v
    return _fallback_load().get(f"{service}:{account}")


def delete_secret(account: str, *, service: str = SERVICE) -> bool:
    """Remove a secret from EVERY backend (revoke must be thorough). Returns True
    if at least one backend held + removed it."""
    removed = False
    kr = _try_keyring()
    if kr is not None:
        try:
            kr.delete_password(service, account)
            removed = True
        except Exception:
            pass
    if sys.platform == "darwin" and _macos_del(service, account):
        removed = True
    if sys.platform.startswith("linux") and _linux_del(service, account):
        removed = True
    data = _fallback_load()
    key = f"{service}:{account}"
    if key in data:
        data.pop(key, None)
        _fallback_save(data)
        removed = True
    return removed
