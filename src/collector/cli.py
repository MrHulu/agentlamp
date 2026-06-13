#!/usr/bin/env python3
"""``agentlamp`` — the collector control CLI (enroll / revoke / status / doctor).

The headline command is **enroll** (I5): one line on a NEW computer installs the
WHOLE stack, not just a ``kid``. An un-enrolled computer shows offline/stale on the
orb — it never "magically follows." Enroll is idempotent: re-running it never
double-installs hooks, never re-mints a secret you passed in, and updates the relay
config in place.

    # THE one-liner on a fresh machine — MINTS a fresh kid + high-entropy secret,
    # stores them, registers with the relay (admin token via env/stdin, not argv):
    AGENTLAMP_ADMIN_TOKEN=... agentlamp enroll \\
        --relay-host https://relay.example.com \\
        --collector-id laptop-2 \\
        --write-claude ~/.claude/settings.json \\
        --write-codex  ~/.codex/config.toml

    # explicit kid+secret still supported (rotation / pinning); argv-safe secret input:
    agentlamp enroll --relay-host https://relay.example.com --collector-id laptop-2 \\
        --kid k7 --secret-stdin   # secret read from stdin, never on argv

    # later, on switching away / losing a laptop:
    agentlamp revoke --kid k7            # local: forget the secret (relay revokes server-side)
    agentlamp status                     # what is configured here
    agentlamp doctor                     # are the pieces actually in place?

What enroll does (each step idempotent):
  1. install provider hooks   — reuses ``install_hooks.py`` (--print, or --write-* merge)
  2. init the local pepper     — ``config.load_pepper()`` creates a persisted 0600 key
  3. ensure the alias map file — touch ``aliases.toml`` so the user can edit neutral names
  4. store the collector secret + kid in the OS keyring (``secretstore``)
  5. enable relay push         — write the relay env to a sourced profile fragment
  6. REGISTER the kid+secret with the cloud — POST {relay_host}/admin/collectors/{kid}/enroll
     (Authorization: Bearer <admin token>). This is what makes "switch computer fast"
     REAL (I5): a brand-new computer self-enrolls — the cloud's Durable Object adds the
     kid+secret to its live registry at once, with NO ``wrangler deploy`` / redeploy.
     Idempotent server-side (re-enroll just re-puts the same kid). The admin token comes
     from ``--admin-token`` or ``AGENTLAMP_ADMIN_TOKEN`` — a CLEAR error if it is missing.

NO host/account/machine name is hardcoded (I3) — every identifier comes from a flag
or the environment. Revoke is documented + implemented (``revoke``): it forgets the
local secret AND hits the public ``/admin/collectors/{kid}/revoke`` route so a leaked
secret is rejected immediately everywhere (the Durable Object owns revocation, I4).
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

# Bootstrap import path (run as module or script).
_SRC = pathlib.Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from collector import config, install_hooks, netpost, secretstore  # noqa: E402

# The env fragment enroll writes; sourced from the shell profile so the daemon
# (and any agentlamp invocation) inherits the relay config. Under the config dir
# (overridable for tests via AGENTLAMP_CONFIG_DIR), never a hardcoded $HOME path.
ENV_FILENAME = "relay.env"


def _env_path() -> pathlib.Path:
    return pathlib.Path(config.CONFIG_DIR) / ENV_FILENAME


def _write_env(host: str, kid: str, collector_id: str) -> pathlib.Path:
    """Persist the relay config as a sourceable env fragment (0600). Idempotent —
    rewrites the same keys each time."""
    p = _env_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# AgentLamp relay config — written by `agentlamp enroll`. Source from your",
        "# shell profile:  [ -f ~/.config/agentlamp/relay.env ] && . ~/.config/agentlamp/relay.env",
        "export AGENTLAMP_MODE=relay",
        f"export AGENTLAMP_RELAY_HOST={_sh_quote(host)}",
        f"export AGENTLAMP_RELAY_KID={_sh_quote(kid)}",
        f"export AGENTLAMP_COLLECTOR_ID={_sh_quote(collector_id)}",
        # Relay = cloud viewer: force HMAC labels so no readable folder name leaks.
        "export AGENTLAMP_LOCAL_LABELS=0",
        "",
    ]
    fd = os.open(str(p), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, ("\n".join(lines)).encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def _sh_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)


# Portable, source-free relay config (devlog/16 MED #5). enroll writes this JSON
# next to the POSIX relay.env so config.py reads the relay settings DIRECTLY (no
# `source` of a POSIX file — works on Windows / cron / a bare daemon launch).
RELAY_JSON_FILENAME = "relay.json"


def _relay_json_path() -> pathlib.Path:
    return pathlib.Path(config.CONFIG_DIR) / RELAY_JSON_FILENAME


def _write_relay_json(host: str, kid: str, collector_id: str) -> pathlib.Path:
    """Persist the relay config as a portable JSON file (0600) config.py reads
    directly — no POSIX sourcing required (devlog/16 MED #5)."""
    import json
    p = _relay_json_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "mode": "relay",
        "relay_host": host,
        "kid": kid,
        "collector_id": collector_id,
        # Relay = cloud viewer: force HMAC labels so no readable folder name leaks.
        "local_labels": False,
    }
    fd = os.open(str(p), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(data, separators=(",", ":")).encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


# --------------------------------------------------------------------------- #
# Relay-host scheme guard (devlog/16 MED #3): enroll/revoke carry the admin bearer
# AND the collector secret, so the relay host MUST be https:// — a plaintext http://
# would leak both on the wire. http:// is rejected EXCEPT an explicit loopback host
# (localhost / 127.0.0.1 / [::1]) for local stub tests, or the --insecure-localhost
# escape hatch (also loopback-only). Never silently downgrades.
# --------------------------------------------------------------------------- #
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _is_loopback_host(host: str) -> bool:
    from urllib.parse import urlparse
    try:
        netloc_host = (urlparse(host).hostname or "").lower()
    except Exception:
        return False
    if netloc_host in _LOOPBACK_HOSTS:
        return True
    return netloc_host.startswith("127.")


def _require_https(host: str, *, insecure_localhost: bool) -> str | None:
    """Return an error string if ``host`` is not an acceptable secure relay URL, else
    None. https:// always OK; http:// only for loopback (always) or with the explicit
    --insecure-localhost flag (still loopback-only — never a public http:// host)."""
    from urllib.parse import urlparse
    try:
        scheme = (urlparse(host).scheme or "").lower()
    except Exception:
        scheme = ""
    if scheme == "https":
        return None
    if scheme != "http":
        return (f"--relay-host must be an https:// URL (got {host!r}); it carries the "
                "admin bearer + collector secret.")
    # scheme == http:
    loop = _is_loopback_host(host)
    if loop:
        # http://loopback (local stub / dev relay) never reaches a network — allowed
        # unconditionally; --insecure-localhost is the explicit, self-documenting form.
        return None
    if insecure_localhost:
        return ("--insecure-localhost only permits http:// for a LOOPBACK host "
                f"(localhost/127.0.0.1); {host!r} is not loopback.")
    return (f"--relay-host must use https:// (got plaintext http:// {host!r}); it "
            "carries the admin bearer + collector secret. Use https://, or "
            "--insecure-localhost for a loopback dev relay.")


# --------------------------------------------------------------------------- #
# Secret / admin-token input WITHOUT argv exposure (devlog/16 MED #4).
#
# A secret/token passed as --secret <v> / --admin-token <v> lands in the process
# argv, where any local user's `ps` can read it. Prefer (in order): an explicit
# --*-stdin flag (read one line from stdin), the matching env var, the legacy
# --secret/--admin-token flag (kept working, documented as less safe), or an
# interactive getpass prompt on a TTY. Returns the resolved value (may be "").
# --------------------------------------------------------------------------- #
def _read_one_line_stdin() -> str:
    """Read a single secret line from stdin (the --*-stdin form). Strips the trailing
    newline only — a secret may legitimately contain spaces."""
    line = sys.stdin.readline()
    return line.rstrip("\n").rstrip("\r")


def _resolve_secret(args: argparse.Namespace) -> str:
    """The collector signing secret, argv-safe (devlog/16 MED #4):
    --secret-stdin > AGENTLAMP_RELAY_SECRET env > --secret (argv, less safe) >
    getpass prompt on a TTY. Empty string if none supplied (caller decides to mint)."""
    if getattr(args, "secret_stdin", False):
        return _read_one_line_stdin()
    env_secret = os.environ.get("AGENTLAMP_RELAY_SECRET", "")
    if env_secret:
        return env_secret
    if getattr(args, "secret", ""):
        return args.secret
    if getattr(args, "secret_prompt", False) and sys.stdin.isatty():
        import getpass
        return getpass.getpass("Collector secret (leave blank to mint a fresh one): ").strip()
    return ""


def _mint_kid() -> str:
    """Mint a fresh collector key id matching the relay's KID charset
    ([A-Za-z0-9_-]{1,64}). 32 bits of hex (8 chars) prefixed ``k`` for legibility."""
    import secrets
    return "k" + secrets.token_hex(4)


def _mint_secret() -> str:
    """Mint a high-entropy collector signing secret: 256 bits (urandom-backed
    ``secrets.token_hex(32)`` → 64 hex chars). devlog/16 P0: this is what makes the
    one-liner real — enroll with no --kid/--secret self-provisions strong creds."""
    import secrets
    return secrets.token_hex(32)


# --------------------------------------------------------------------------- #
# Cloud admin registration (I5 enroll / I4 revoke).
#
# These hit the relay's authed /admin routes so a brand-new computer self-enrolls
# (the Durable Object adds the kid+secret to its live registry at once — no
# `wrangler deploy`), and a revoke is rejected everywhere immediately. The admin
# token NEVER goes in the URL or a hardcoded constant (I3): it rides in the
# Authorization header, sourced from --admin-token or AGENTLAMP_ADMIN_TOKEN.
# --------------------------------------------------------------------------- #
def _admin_token(args: argparse.Namespace) -> str:
    """The relay admin bearer, argv-safe (devlog/16 MED #4): --admin-token-stdin >
    AGENTLAMP_ADMIN_TOKEN env > --admin-token (argv, less safe). The token never has
    to ride on argv where a local `ps` could read it; --admin-token stays supported."""
    if getattr(args, "admin_token_stdin", False):
        return _read_one_line_stdin().strip()
    env_tok = os.environ.get("AGENTLAMP_ADMIN_TOKEN", "")
    if env_tok:
        return env_tok.strip()
    return (getattr(args, "admin_token", "") or "").strip()


def _admin_freshness_headers() -> dict:
    """The relay's /admin surface requires per-request FRESHNESS, not just a bearer (the DO's
    checkAdminReplay — docs/devlog/16 MED): a fresh ``X-ACO-Timestamp`` (±300s of server time) +
    a single-use ``X-ACO-Nonce`` (lowercase hex). A bearer alone proves authorization, NOT recency,
    so WITHOUT these the relay rejects with 401 ``admin_stale`` — and a replayed OLD enroll could
    otherwise undo a LATER revoke. Minted fresh per call (the Worker forwards them to the DO)."""
    import time as _time
    import secrets as _secrets
    return {
        "X-ACO-Timestamp": str(int(_time.time())),
        "X-ACO-Nonce": _secrets.token_hex(16),   # 32 lowercase-hex chars (within the [16,128] range)
    }


def _admin_post(host: str, path: str, body: dict, token: str) -> tuple[int, dict]:
    """POST to a relay /admin route with the bearer + admin freshness headers, bypassing any env
    proxy (reuses netpost's empty-ProxyHandler opener — never touches the system proxy)."""
    url = f"{host.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {token}", **_admin_freshness_headers()}
    return netpost.post_json(url, body, headers=headers)


class CloudRegisterError(Exception):
    """A cloud /admin call could not be delivered or was rejected. ``transport`` is
    True when the relay was unreachable (vs. an HTTP-level rejection)."""

    def __init__(self, msg: str, *, transport: bool = False):
        super().__init__(msg)
        self.transport = transport


def _register_collector_with_cloud(host: str, kid: str, secret: str, token: str) -> dict:
    """REGISTER kid+secret with the cloud (I5). POSTs the secret in the body + the
    admin token in the Authorization header to /admin/collectors/{kid}/enroll.

    Idempotent: the DO just re-puts the same kid (re-running enroll is safe). Raises
    ``CloudRegisterError`` with a CLEAR message on a missing token (caught earlier),
    an HTTP rejection (401/403/400/...), or a transport failure.
    """
    try:
        status, resp = _admin_post(host, f"/admin/collectors/{kid}/enroll", {"secret": secret}, token)
    except netpost.PostError as e:
        raise CloudRegisterError(
            f"could not reach the relay at {host} to register kid={kid}: {e}", transport=True
        ) from e
    if status == 200 and resp.get("ok"):
        return resp
    raise CloudRegisterError(_admin_error_hint(status, resp, kid))


def _revoke_collector_in_cloud(host: str, kid: str, token: str) -> dict:
    """Hit the public /admin/collectors/{kid}/revoke route so a leaked secret is
    rejected immediately everywhere (I4 — the Durable Object owns revocation)."""
    try:
        status, resp = _admin_post(host, f"/admin/collectors/{kid}/revoke", {}, token)
    except netpost.PostError as e:
        raise CloudRegisterError(
            f"could not reach the relay at {host} to revoke kid={kid}: {e}", transport=True
        ) from e
    if status == 200 and resp.get("ok"):
        return resp
    raise CloudRegisterError(_admin_error_hint(status, resp, kid))


def _admin_error_hint(status: int, resp: dict, kid: str) -> str:
    """Map a relay /admin rejection to a CLEAR, actionable message."""
    err = str(resp.get("error", "")) or "(no error field)"
    if status == 401:
        return (f"relay rejected the admin token (401 {err}) for kid={kid} — the token in "
                "--admin-token / AGENTLAMP_ADMIN_TOKEN does not match the relay's "
                "AGENTLAMP_ADMIN_TOKEN secret.")
    if status == 403:
        return (f"relay admin route is disabled (403 {err}) — the relay has no "
                "AGENTLAMP_ADMIN_TOKEN set, so enroll/revoke is fail-closed. Set it on the "
                "relay (wrangler secret put AGENTLAMP_ADMIN_TOKEN) and redeploy once.")
    if status == 400:
        return f"relay rejected the request (400 {err}) for kid={kid} — bad kid charset or empty secret."
    return f"relay returned HTTP {status} ({err}) for kid={kid}."


def cmd_enroll(args: argparse.Namespace) -> int:
    print("agentlamp enroll — installing the whole stack (idempotent)\n")

    # 0. relay-host scheme guard (MED #3) — enroll carries the admin bearer + the
    #    collector secret, so the host MUST be https:// (loopback http:// allowed for
    #    a dev relay / local stub). Reject BEFORE we touch hooks/secret/cloud.
    if args.relay_host:
        err = _require_https(args.relay_host, insecure_localhost=args.insecure_localhost)
        if err:
            print(f"[0/6] relay-host: ERROR — {err}", file=sys.stderr)
            return 2

    # 1. hooks ------------------------------------------------------------- #
    did_write = False
    if args.write_claude:
        install_hooks.write_claude(args.write_claude)
        did_write = True
    if args.write_codex:
        install_hooks.write_codex(args.write_codex)
        did_write = True
    if not did_write:
        print("[1/6] hooks: (no --write-claude/--write-codex given) — paste this config:\n")
        install_hooks._print("all")
        print()
    else:
        print("[1/6] hooks: installed (additive merge, .bak kept).")

    # 2. pepper ------------------------------------------------------------ #
    pepper = config.load_pepper()
    print(f"[2/6] pepper: ready ({len(pepper)*8}-bit local HMAC key at {config.PEPPER_FILE}).")

    # 3. alias map --------------------------------------------------------- #
    alias_path = pathlib.Path(config.ALIAS_FILE)
    alias_path.parent.mkdir(parents=True, exist_ok=True)
    if not alias_path.exists():
        alias_path.write_text(
            "# AgentLamp neutral aliases — map a cwd/account to a neutral label.\n"
            "# [projects]\n#   \"/abs/path/to/repo\" = \"project-a\"\n"
            "# [accounts]\n#   default = \"main\"\n",
            encoding="utf-8",
        )
        print(f"[3/6] aliases: created editable map at {alias_path}.")
    else:
        print(f"[3/6] aliases: present at {alias_path} (left as-is).")

    # 4. collector secret + kid. THE HEADLINE (MED #1 / P0): with no --kid/--secret,
    #    enroll MINTS a fresh kid + a high-entropy secret (urandom-backed) and stores
    #    them — that is what makes the one-liner real. Supplying --kid/--secret stays
    #    supported (explicit rotation / pinning). Secret input is argv-safe (MED #4):
    #    --secret-stdin / AGENTLAMP_RELAY_SECRET / --secret / getpass, never on argv.
    kid = args.kid or _mint_kid()
    minted_kid = not args.kid
    supplied_secret = _resolve_secret(args)
    secret = ""
    try:
        if supplied_secret:
            backend = secretstore.set_secret(kid, supplied_secret)
            secret = supplied_secret
            src = "from stdin/env/flag"
            print(f"[4/6] secret: stored kid={kid} in {backend} ({src}).")
        else:
            existing = secretstore.get_secret(kid)
            if existing:
                secret = existing
                print(f"[4/6] secret: kid={kid} already stored (no secret supplied, kept).")
            else:
                # MINT a fresh high-entropy secret (P0 one-liner). 256-bit, urandom.
                secret = _mint_secret()
                backend = secretstore.set_secret(kid, secret)
                print(f"[4/6] secret: MINTED a fresh 256-bit secret for "
                      f"{'minted ' if minted_kid else ''}kid={kid}, stored in {backend}.")
    except secretstore.SecretStoreError as e:
        # Windows fail-closed (MED #5): never silently write a plaintext secret.
        print(f"[4/6] secret: ERROR — {e}", file=sys.stderr)
        return 2

    # 5. enable relay push ------------------------------------------------- #
    if not args.relay_host:
        print("[5/6] relay: --relay-host required.", file=sys.stderr)
        return 2
    collector_id = args.collector_id or config.ACCOUNT
    # Write BOTH: the POSIX env fragment (legacy, shell-sourced) AND a portable
    # relay.json config.py reads DIRECTLY — so a Windows / cron / bare daemon launch
    # picks up the relay config with NOTHING sourced (MED #5).
    env_path = _write_env(args.relay_host, kid, collector_id)
    json_path = _write_relay_json(args.relay_host, kid, collector_id)
    print(f"[5/6] relay: enabled → {args.relay_host} (collector_id={collector_id}).")
    print(f"        wrote {json_path} (read directly — no sourcing needed)")
    print(f"        wrote {env_path} (POSIX shell profile fragment)")

    # 6. REGISTER the kid+secret with the cloud (I5) so a brand-new computer
    #    self-enrolls — the relay's Durable Object adds it to the live registry at
    #    once, NO `wrangler deploy`. Skippable for an offline/local-only setup.
    if args.no_cloud_register:
        print("[6/6] cloud: skipped (--no-cloud-register) — kid is NOT registered with the relay; "
              "this computer will be rejected until you enroll it server-side.")
    else:
        token = _admin_token(args)
        if not token:
            print("[6/6] cloud: ERROR — no admin token. Registering a new computer with the relay "
                  "needs the relay's admin token; pass --admin-token or set AGENTLAMP_ADMIN_TOKEN "
                  "(or re-run with --no-cloud-register for a local-only setup).", file=sys.stderr)
            return 2
        try:
            _register_collector_with_cloud(args.relay_host, kid, secret, token)
            print(f"[6/6] cloud: registered kid={kid} with the relay "
                  "(Durable Object live registry updated — no redeploy needed).")
        except CloudRegisterError as e:
            print(f"[6/6] cloud: ERROR — {e}", file=sys.stderr)
            return 3

    print("\n  Config is read directly from relay.json — NO sourcing needed (Windows/cron OK).")
    print("  POSIX shells may also source the env fragment:")
    print(f"    [ -f {env_path} ] && . {env_path}")
    print("\n  Then start the daemon:")
    print(f"    cd {config.SRC_DIR} && {sys.executable} -m collector.daemon\n")
    print("Done. This computer now signs + pushes to the relay. To stop following from")
    print(f"here later:  agentlamp revoke --kid {kid}   (and delete the kid in the relay).")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    """Revoke a kid: forget the local secret + disable relay push here, AND hit the
    public relay revoke route so a leaked secret stops being accepted at once.

    The authoritative revoke is SERVER-SIDE — the relay's Durable Object owns
    revocation (I4) and rejects the kid immediately everywhere. Local revoke alone
    does not invalidate a secret that already leaked, so this does BOTH: it POSTs to
    /admin/collectors/{kid}/revoke (admin token from --admin-token / env) and then
    forgets the local copy. The cloud hit is skippable with --no-cloud-revoke (and
    is auto-skipped if no relay host / admin token is available, with a clear note).
    """
    # 1. Server-side revoke first — the leaked secret must stop being accepted now.
    host = args.relay_host or config.RELAY_HOST
    cloud_msg = ""
    if args.no_cloud_revoke:
        cloud_msg = "cloud: skipped (--no-cloud-revoke)"
    elif not host:
        cloud_msg = "cloud: SKIPPED (no --relay-host / AGENTLAMP_RELAY_HOST) — revoke it server-side"
    elif (scheme_err := _require_https(host, insecure_localhost=args.insecure_localhost)):
        # MED #3: the revoke route carries the admin bearer too — refuse plaintext http://.
        cloud_msg = f"cloud: ERROR — {scheme_err}"
    else:
        token = _admin_token(args)
        if not token:
            cloud_msg = ("cloud: ERROR — no admin token (pass --admin-token or set "
                         "AGENTLAMP_ADMIN_TOKEN) — revoke NOT applied at the relay")
        else:
            try:
                _revoke_collector_in_cloud(host, args.kid, token)
                cloud_msg = f"cloud: revoked kid={args.kid} at the relay (rejected everywhere at once)"
            except CloudRegisterError as e:
                cloud_msg = f"cloud: ERROR — {e}"

    # 2. Local: forget the secret + disable relay push (idempotent). Remove BOTH the
    #    POSIX env fragment AND the portable relay.json so a source-free daemon launch
    #    also stops following from here (MED #5).
    removed = secretstore.delete_secret(args.kid) if args.kid else False
    env_removed = False
    for p in (_env_path(), _relay_json_path()):
        try:
            if p.exists():
                p.unlink()
                env_removed = True
        except OSError:
            pass
    print(f"revoke: kid={args.kid} secret {'removed' if removed else 'was not stored'}; "
          f"relay config {'removed' if env_removed else 'absent'}.")
    print(f"        {cloud_msg}.")
    if "ERROR" in cloud_msg or "SKIPPED" in cloud_msg.upper():
        print("IMPORTANT: also delete this kid from the relay (server-side) so a leaked")
        print("secret is rejected at once — the Durable Object owns revocation (I4):")
        print(f"    # see docs/runbook/switch-fast.md → Revoke; e.g. an /admin call removing kid={args.kid}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:  # noqa: ARG001
    mode = "relay" if config.RELAY_MODE else "local"
    print(f"mode:         {mode}")
    if config.RELAY_MODE:
        print(f"relay_host:   {config.RELAY_HOST or '(unset)'}")
        print(f"collector_id: {config.COLLECTOR_ID}")
        print(f"kid:          {config.RELAY_KID or '(unset)'}")
        secret = config.relay_secret()
        print(f"secret:       {'present' if secret else 'MISSING (run enroll)'}")
    else:
        print(f"server_base:  {config.SERVER_BASE}")
    print(f"queue:        {config.QUEUE_DIR}")
    print(f"pepper:       {'present' if config.PEPPER_FILE.is_file() else 'will-create-on-first-run'}")
    print(f"aliases:      {config.ALIAS_FILE}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:  # noqa: ARG001
    """Read-only health check: are the enroll outputs actually in place?"""
    ok = True
    checks = []
    checks.append(("pepper file", config.PEPPER_FILE.is_file()))
    checks.append(("alias map", pathlib.Path(config.ALIAS_FILE).exists()))
    if config.RELAY_MODE:
        checks.append(("relay host set", bool(config.RELAY_HOST)))
        checks.append(("kid set", bool(config.RELAY_KID)))
        checks.append(("secret reachable", config.relay_secret() is not None))
        checks.append(("env fragment", _env_path().exists()))
    else:
        checks.append(("server base set", bool(config.SERVER_BASE)))
    for name, passed in checks:
        print(f"  [{'OK' if passed else 'XX'}] {name}")
        ok = ok and passed
    print("doctor:", "all checks passed" if ok else "SOME CHECKS FAILED — re-run enroll")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentlamp", description="AgentLamp collector control CLI.")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("enroll", help="install the whole stack on this computer (idempotent)")
    e.add_argument("--relay-host", default=os.environ.get("AGENTLAMP_RELAY_HOST", ""),
                   help="relay base URL, e.g. https://relay.example.com (https:// REQUIRED — "
                        "it carries the admin bearer + collector secret; loopback http:// OK)")
    e.add_argument("--collector-id", default=os.environ.get("AGENTLAMP_COLLECTOR_ID", ""),
                   help="neutral collector id in the URL ([A-Za-z0-9_-]{1,64}); default=account")
    e.add_argument("--kid", default=os.environ.get("AGENTLAMP_RELAY_KID", ""),
                   help="active collector key id; OMIT to MINT a fresh one (the one-liner)")
    # Secret input: argv-safe forms preferred (MED #4). --secret stays supported but is
    # the LEAST safe (lands in argv). OMIT all of them to MINT a high-entropy secret.
    e.add_argument("--secret", default="",
                   help="collector signing secret (LESS SAFE: lands in argv; prefer "
                        "--secret-stdin / $AGENTLAMP_RELAY_SECRET). Omit to MINT a fresh one.")
    e.add_argument("--secret-stdin", action="store_true",
                   help="read the collector secret from stdin (argv-safe; MED #4)")
    e.add_argument("--secret-prompt", action="store_true",
                   help="prompt for the collector secret via getpass on a TTY (argv-safe)")
    e.add_argument("--admin-token", default="",
                   help="relay admin bearer to register the kid+secret with the cloud (I5). "
                        "LESS SAFE (argv); prefer --admin-token-stdin / $AGENTLAMP_ADMIN_TOKEN.")
    e.add_argument("--admin-token-stdin", action="store_true",
                   help="read the admin bearer from stdin (argv-safe; MED #4)")
    e.add_argument("--insecure-localhost", action="store_true",
                   help="permit a plaintext http:// relay host ONLY for a loopback dev relay")
    e.add_argument("--no-cloud-register", action="store_true",
                   help="do NOT POST the kid+secret to the relay (local-only setup); the cloud "
                        "will reject this computer until you enroll it server-side")
    e.add_argument("--write-claude", nargs="?", const="~/.claude/settings.json", default=None,
                   help="additively merge Claude hooks into this settings.json")
    e.add_argument("--write-codex", nargs="?", const="~/.codex/config.toml", default=None,
                   help="additively append Codex hooks to this config.toml")
    e.set_defaults(func=cmd_enroll)

    r = sub.add_parser("revoke", help="revoke a kid: forget the local secret + revoke it at the relay")
    r.add_argument("--kid", required=True, help="key id to revoke (local + server-side)")
    r.add_argument("--relay-host", default=os.environ.get("AGENTLAMP_RELAY_HOST", ""),
                   help="relay base URL for the server-side revoke (https:// REQUIRED); "
                        "default=$AGENTLAMP_RELAY_HOST")
    r.add_argument("--admin-token", default="",
                   help="relay admin bearer to hit /admin/collectors/{kid}/revoke (I4). LESS "
                        "SAFE (argv); prefer --admin-token-stdin / $AGENTLAMP_ADMIN_TOKEN.")
    r.add_argument("--admin-token-stdin", action="store_true",
                   help="read the admin bearer from stdin (argv-safe; MED #4)")
    r.add_argument("--insecure-localhost", action="store_true",
                   help="permit a plaintext http:// relay host ONLY for a loopback dev relay")
    r.add_argument("--no-cloud-revoke", action="store_true",
                   help="forget the local secret only; do NOT hit the relay revoke route")
    r.set_defaults(func=cmd_revoke)

    s = sub.add_parser("status", help="show what is configured here")
    s.set_defaults(func=cmd_status)

    d = sub.add_parser("doctor", help="read-only health check of the enroll outputs")
    d.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
