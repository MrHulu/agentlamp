"""Collector tests: fire-and-forget sink, normalization, privacy, proxy bypass,
daemon drain, and server-compatibility of the emitted shorthand.

Run:  cd <repo> && ./.venv/bin/python -m pytest src/collector/tests -q
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from collector import config, daemon, netpost
from collector.config import S
from collector.normalize import normalize_record

# --------------------------------------------------------------------------- #
# Hook payload builders (shapes verified against the 2026 official docs).
# --------------------------------------------------------------------------- #
def claude_hook(event, **fields):
    base = {
        "session_id": "claude-sess-abc123",
        "transcript_path": "/Users/hulu/.claude/projects/x/transcript.jsonl",
        "cwd": "/Users/hulu/secret/client-acme-prod",
        "hook_event_name": event,
    }
    base.update(fields)
    return {"provider": "claude", "received_at": 1.0, "hook": base}


def codex_hook(event, **fields):
    base = {
        "session_id": "codex-sess-xyz789",
        "turn_id": "turn-1",
        "cwd": "/Users/hulu/secret/client-acme-prod",
        "hook_event_name": event,
        "model": "gpt-5-codex",
        "permission_mode": "default",
    }
    base.update(fields)
    return {"provider": "codex", "received_at": 1.0, "hook": base}


def norm(rec, pepper, aliases):
    return normalize_record(rec, pepper=pepper, aliases=aliases)


# --------------------------------------------------------------------------- #
# 1. hook_sink — fire-and-forget queue writer (real subprocess).
# --------------------------------------------------------------------------- #
def test_hook_sink_writes_queue_file_and_is_silent(_isolated_state):
    qd = _isolated_state / "queue"
    hook_sink = config.SRC_DIR / "collector" / "hook_sink.py"
    payload = json.dumps({"session_id": "s1", "hook_event_name": "PreToolUse",
                          "tool_name": "Read", "cwd": "/Users/hulu/x"})
    env = dict(os.environ, AGENTLAMP_QUEUE_DIR=str(qd))
    p = subprocess.run([sys.executable, str(hook_sink), "--provider", "claude"],
                       input=payload, capture_output=True, text=True, env=env, timeout=10)
    assert p.returncode == 0
    assert p.stdout == ""  # passive observer: nothing on stdout
    files = list(qd.glob("*.json"))
    assert len(files) == 1
    assert not list(qd.glob("*.tmp"))  # no leftover temp
    rec = json.loads(files[0].read_text())
    assert rec["provider"] == "claude"
    assert rec["hook"]["tool_name"] == "Read"


def test_hook_sink_never_fails_on_garbage(_isolated_state):
    qd = _isolated_state / "queue"
    hook_sink = config.SRC_DIR / "collector" / "hook_sink.py"
    env = dict(os.environ, AGENTLAMP_QUEUE_DIR=str(qd))
    p = subprocess.run([sys.executable, str(hook_sink), "--provider", "codex"],
                       input="not json at all {{{", capture_output=True, text=True,
                       env=env, timeout=10)
    assert p.returncode == 0  # MUST never fail the host agent
    files = list(qd.glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["hook"] == {"_unparsed": True}


# --------------------------------------------------------------------------- #
# 2. normalization — event -> status for BOTH providers.
# --------------------------------------------------------------------------- #
def test_session_start_idle(pepper, aliases):
    r = norm(claude_hook("SessionStart", source="startup", model="claude-opus-4"), pepper, aliases)
    assert r.action == "post" and r.event["status"] == "IDLE"


def test_prompt_submit_thinking_no_prompt(pepper, aliases):
    r = norm(claude_hook("UserPromptSubmit", prompt="implement the secret auth flow"), pepper, aliases)
    assert r.event["status"] == "THINKING"
    assert "secret" not in json.dumps(r.event)


@pytest.mark.parametrize("tool,expected", [
    ("Read", "READING"), ("Grep", "READING"), ("Glob", "READING"),
    ("WebSearch", "READING"),
    ("Write", "CODING"), ("Edit", "CODING"), ("MultiEdit", "CODING"),
    ("Task", "THINKING"),
])
def test_claude_pretooluse_status(tool, expected, pepper, aliases):
    r = norm(claude_hook("PreToolUse", tool_name=tool, tool_input={"x": 1}), pepper, aliases)
    assert r.event["status"] == expected


def test_bash_test_command_is_testing(pepper, aliases):
    r = norm(claude_hook("PreToolUse", tool_name="Bash",
                         tool_input={"command": "npm test"}), pepper, aliases)
    assert r.event["status"] == "TESTING"
    assert r.event["tool_category"] == "test"
    assert "npm" not in json.dumps(r.event)


def test_bash_read_command_is_reading(pepper, aliases):
    r = norm(claude_hook("PreToolUse", tool_name="Bash",
                         tool_input={"command": "cat /Users/hulu/.ssh/id_rsa"}), pepper, aliases)
    assert r.event["status"] == "READING"
    assert "id_rsa" not in json.dumps(r.event)


def test_bash_other_command_is_coding(pepper, aliases):
    r = norm(claude_hook("PreToolUse", tool_name="Bash",
                         tool_input={"command": "python deploy.py"}), pepper, aliases)
    assert r.event["status"] == "CODING"


def test_codex_apply_patch_is_coding(pepper, aliases):
    r = norm(codex_hook("PreToolUse", tool_name="apply_patch",
                        tool_input={"input": "*** Begin Patch"}), pepper, aliases)
    assert r.event["status"] == "CODING"
    assert r.event["tool_category"] == "edit"


def test_codex_shell_list_command(pepper, aliases):
    r = norm(codex_hook("PreToolUse", tool_name="shell",
                        tool_input={"command": ["bash", "-lc", "pytest -q"]}), pepper, aliases)
    assert r.event["status"] == "TESTING"
    assert "pytest" not in json.dumps(r.event)


def test_permission_request_waiting(pepper, aliases):
    for rec in (claude_hook("PermissionRequest", tool_name="Bash", tool_input={"command": "rm x"}),
                codex_hook("PermissionRequest", tool_name="shell", tool_input={"command": "rm x"})):
        r = norm(rec, pepper, aliases)
        assert r.event["status"] == "WAITING"
        assert r.event["needs_attention"] is True


def test_notification_permission_prompt_waiting(pepper, aliases):
    r = norm(claude_hook("Notification", notification_type="permission_prompt",
                         message="Claude needs your permission to use Bash"), pepper, aliases)
    assert r.action == "post" and r.event["status"] == "WAITING"
    assert "Bash" not in json.dumps(r.event)  # message never echoed


def test_notification_auth_success_is_heartbeat(pepper, aliases):
    r = norm(claude_hook("Notification", notification_type="auth_success"), pepper, aliases)
    assert r.action == "heartbeat" and r.event is None


def test_notification_idle_prompt_is_not_waiting(pepper, aliases):
    """A session that finished and is waiting for the user's next message fires
    Notification:idle_prompt — that is NOT an approval request, so it must NOT light
    WAITING (regression: several idle sessions falsely lit the orb amber)."""
    r = norm(claude_hook("Notification", notification_type="idle_prompt"), pepper, aliases)
    assert r.action == "post"
    assert r.event["status"] == "IDLE"
    assert r.event["status"] != "WAITING"
    assert not r.event.get("needs_attention")


def test_posttoolusefailure_error(pepper, aliases):
    r = norm(claude_hook("PostToolUseFailure", tool_name="Bash",
                         error="ENOENT: /Users/hulu/x not found"), pepper, aliases)
    assert r.event["status"] == "ERROR"
    assert r.event["error_label"] in ("unknown", "tool_error")  # path-bearing msg -> unknown
    assert "/Users/" not in json.dumps(r.event)


def test_stop_done(pepper, aliases):
    r = norm(claude_hook("Stop", stop_hook_active=False), pepper, aliases)
    assert r.event["status"] == "DONE"


def test_unknown_event_is_heartbeat_not_clobber(pepper, aliases):
    r = norm(claude_hook("SomeBrandNewEvent2027"), pepper, aliases)
    assert r.action == "heartbeat"
    assert "SomeBrandNewEvent2027" in r.diag  # name preserved for diagnostics


# --------------------------------------------------------------------------- #
# 3. cwd -> alias mechanism + the KILLER privacy test.
# --------------------------------------------------------------------------- #
def test_cwd_local_label_is_basename(pepper, aliases):
    """Local single-owner mode (default): the orb shows the REAL folder name
    (readable), not a hash. The parent path / secret dir never appears — basename only."""
    r = norm(claude_hook("PreToolUse", tool_name="Read", tool_input={}), pepper, aliases)
    pa = r.event["project"]
    assert pa == "client-acme-prod"  # the cwd basename, readable
    assert "/Users/" not in pa and "secret" not in pa and "hulu" not in pa


def test_cwd_hmac_when_local_labels_off(pepper, aliases, monkeypatch):
    """AGENTLAMP_LOCAL_LABELS=0 (relay-grade): cwd collapses to project-<hmac6>,
    basename never appears — the original privacy guarantee for cloud-relay mode."""
    import importlib

    import collector.config as cfgmod
    monkeypatch.setenv("AGENTLAMP_LOCAL_LABELS", "0")
    importlib.reload(cfgmod)
    try:
        r = norm(claude_hook("PreToolUse", tool_name="Read", tool_input={}), pepper, aliases)
        pa = r.event["project"]
        assert pa.startswith("project-") and len(pa.split("-", 1)[1]) == 6
        for seg in ("client", "acme", "prod", "secret", "hulu"):
            assert seg not in pa
    finally:
        monkeypatch.delenv("AGENTLAMP_LOCAL_LABELS", raising=False)
        importlib.reload(cfgmod)


def test_cwd_mapped_uses_alias(pepper, aliases):
    rec = claude_hook("PreToolUse", tool_name="Read", tool_input={})
    rec["hook"]["cwd"] = "/Users/hulu/work/acme"
    r = norm(rec, pepper, aliases)
    assert r.event["project"] == "project-a"


def test_session_id_is_hmac_label(pepper, aliases):
    r = norm(claude_hook("UserPromptSubmit", prompt="hi"), pepper, aliases)
    assert r.event["provider_session_id"].startswith("hmac:")
    assert "claude-sess-abc123" not in json.dumps(r.event)


def _hostile_hook():
    rec = claude_hook(
        "PreToolUse",
        tool_name="Bash",
        tool_input={"command": "curl -H 'Authorization: Bearer sk-deadbeefcafe' "
                               "https://acme.internal/secret > /Users/hulu/.ssh/id_rsa",
                    "file_path": "/Users/hulu/secret/client-acme-prod/auth.ts"},
        prompt="my password is hunter2 and my key is sk-live-9999",
    )
    rec["hook"]["cwd"] = "/Users/hulu/secret/client-acme-prod"
    rec["hook"]["transcript_path"] = "/Users/hulu/.claude/x/transcript.jsonl"
    return rec


def test_no_raw_anything_survives_hmac_mode(pepper, aliases, monkeypatch):
    """Relay-grade (LOCAL_LABELS=0): a hostile hook with secrets in EVERY field
    leaks NONE of them — not even the folder basename."""
    import importlib

    import collector.config as cfgmod
    monkeypatch.setenv("AGENTLAMP_LOCAL_LABELS", "0")
    importlib.reload(cfgmod)
    try:
        out = json.dumps(norm(_hostile_hook(), pepper, aliases).event)
        for needle in ("/Users/", "secret", "client", "acme", "sk-deadbeef", "sk-live",
                       "id_rsa", ".ssh", "hunter2", "password", "Bearer", "Authorization",
                       "curl", "transcript", "auth.ts", "internal"):
            assert needle not in out, f"LEAK: {needle!r} survived into {out}"
    finally:
        monkeypatch.delenv("AGENTLAMP_LOCAL_LABELS", raising=False)
        importlib.reload(cfgmod)


def test_local_mode_shows_basename_but_never_path_secrets(pepper, aliases):
    """Local mode (default): the folder BASENAME is shown (readable, intended), but
    the full path, parent dirs, secrets, commands, prompts, and file paths NEVER
    survive — only the leaf folder name."""
    out = json.dumps(norm(_hostile_hook(), pepper, aliases).event)
    assert "client-acme-prod" in out  # the basename IS the readable label (intended)
    for needle in ("/Users/", "secret", "sk-deadbeef", "sk-live", "id_rsa", ".ssh",
                   "hunter2", "password", "Bearer", "Authorization", "curl",
                   "transcript", "auth.ts", "internal"):
        assert needle not in out, f"LEAK: {needle!r} survived into {out}"


# --------------------------------------------------------------------------- #
# 4. proxy bypass — must reach a local server even with a bogus env proxy set.
# --------------------------------------------------------------------------- #
class _Echo(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *a):  # silence
        pass


def test_netpost_bypasses_env_proxy(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _Echo)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        # A bogus proxy on a closed port: if the opener honored it, the POST fails.
        for var in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "ALL_PROXY"):
            monkeypatch.setenv(var, "http://127.0.0.1:1")
        code, body = netpost.post_json(f"http://127.0.0.1:{port}/admin/event", {"x": 1})
        assert code == 200 and body == {"ok": True}
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------- #
# 5. daemon drain — posts the shorthand, deletes the file; quarantines a 422.
# --------------------------------------------------------------------------- #
def test_drain_posts_and_deletes(_isolated_state, monkeypatch):
    config.ensure_dirs()
    rec = claude_hook("PreToolUse", tool_name="Edit", tool_input={"file_path": "/Users/hulu/x"})
    qf = config.QUEUE_DIR / "0000000000000001-1-aaaaaaaa.json"
    qf.write_text(json.dumps(rec))
    sent = []
    monkeypatch.setattr(netpost, "post_json", lambda url, payload, **k: (sent.append((url, payload)) or (200, {"applied": True})))
    counts = daemon.drain_once(config.load_pepper(), config.load_aliases())
    assert counts["posted"] == 1
    assert not qf.exists()
    url, payload = sent[0]
    assert url.endswith("/admin/event")
    assert payload["status"] == "CODING" and payload["provider"] == "claude"
    assert "/Users/" not in json.dumps(payload)


def test_drain_quarantines_rejected(_isolated_state, monkeypatch):
    config.ensure_dirs()
    qf = config.QUEUE_DIR / "0000000000000002-1-bbbbbbbb.json"
    qf.write_text(json.dumps(claude_hook("PreToolUse", tool_name="Read", tool_input={})))
    monkeypatch.setattr(netpost, "post_json",
                        lambda url, payload, **k: (422, {"rejected": True, "reason": "unknown_field:x", "payload_hash": "deadbeef"}))
    counts = daemon.drain_once(config.load_pepper(), config.load_aliases())
    assert counts["rejected"] == 1
    assert not qf.exists()
    dl = list(config.DEAD_LETTER_DIR.glob("*.reason.json"))
    assert len(dl) == 1
    meta = json.loads(dl[0].read_text())
    assert meta["reason"] == "unknown_field:x" and meta["payload_hash"] == "deadbeef"


def test_drain_unreachable_keeps_retrying_never_drops(_isolated_state, monkeypatch):
    """A server restart must lose NOTHING: transport failures leave the record in
    the queue and retry indefinitely (the reaper, not a retry cap, bounds it)."""
    config.ensure_dirs()
    qf = config.QUEUE_DIR / "0000000000000003-1-cccccccc.json"
    qf.write_text(json.dumps(claude_hook("Stop")))

    def boom(url, payload, **k):
        raise netpost.PostError("connection refused")

    monkeypatch.setattr(netpost, "post_json", boom)
    for _ in range(20):
        c = daemon.drain_once(config.load_pepper(), config.load_aliases())
        assert c["requeued"] == 1 and qf.exists()  # never dropped, always retried
    # Server comes back -> the SAME record now posts successfully.
    sent = []
    monkeypatch.setattr(netpost, "post_json", lambda u, p, **k: (sent.append(p) or (200, {})))
    c = daemon.drain_once(config.load_pepper(), config.load_aliases())
    assert c["posted"] == 1 and not qf.exists() and sent[0]["status"] == "DONE"


def test_drain_normalize_crash_is_quarantined_not_fatal(_isolated_state, monkeypatch):
    """HIGH regression backstop: even if normalize_record raises (some future
    unforeseen shape), the daemon quarantines that one record and the loop keeps
    going — one poison record can never stall the whole queue."""
    config.ensure_dirs()
    (config.QUEUE_DIR / "0000000000000004-1-dddddddd.json").write_text(
        json.dumps(claude_hook("PreToolUse", tool_name="Read", tool_input={})))
    (config.QUEUE_DIR / "0000000000000005-1-eeeeeeee.json").write_text(json.dumps(claude_hook("Stop")))
    real = daemon.normalize_record
    seen = {"n": 0}

    def flaky(record, **k):
        seen["n"] += 1
        if seen["n"] == 1:
            raise RuntimeError("simulated unforeseen normalize crash")
        return real(record, **k)

    monkeypatch.setattr(daemon, "normalize_record", flaky)
    sent = []
    monkeypatch.setattr(netpost, "post_json", lambda u, p, **k: (sent.append(p) or (200, {})))
    c = daemon.drain_once(config.load_pepper(), config.load_aliases())
    assert c["dropped"] == 1 and c["posted"] == 1  # loop survived; 2nd record posted
    assert sent and sent[0]["status"] == "DONE"
    assert any(config.DEAD_LETTER_DIR.glob("*.reason.json"))


def test_normalize_poison_tool_name_no_crash(pepper, aliases):
    for bad in ({"x": 1}, ["a"], 42, True):
        rec = claude_hook("PreToolUse", tool_name=bad, tool_input={})
        r = norm(rec, pepper, aliases)  # must not raise
        assert r.action == "post" and r.event["status"] in ("CODING", "READING", "TESTING")


# --------------------------------------------------------------------------- #
# 7. review fixes — account sanitize, Codex failure, SubagentStop, status_detail.
# --------------------------------------------------------------------------- #
def test_account_email_or_tier_collapsed_not_emitted(monkeypatch, pepper, aliases):
    import importlib

    import collector.config as cfgmod
    # normalize_record reads config.ACCOUNT at call time, so reloading config alone
    # (which re-reads AGENTLAMP_ACCOUNT) is enough.
    for bad in ("hulu@example.com", "Pro", "max", "/Users/hulu/x"):
        monkeypatch.setenv("AGENTLAMP_ACCOUNT", bad)
        importlib.reload(cfgmod)
        r = norm(claude_hook("UserPromptSubmit", prompt="hi"), pepper, aliases)
        acct = r.event["account"]
        assert acct.startswith("account-"), f"{bad} -> {acct} (should collapse)"
        assert "@" not in acct and bad.lower() not in acct.lower()
    monkeypatch.delenv("AGENTLAMP_ACCOUNT", raising=False)
    importlib.reload(cfgmod)


def test_account_main_stays_main(pepper, aliases):
    # Default AGENTLAMP_ACCOUNT is "main" (a neutral label) — must pass through.
    r = norm(claude_hook("Stop"), pepper, aliases)
    assert r.event["account"] == "main"


def test_codex_posttooluse_failure_is_error(pepper, aliases):
    rec = codex_hook("PostToolUse", tool_name="shell",
                     tool_input={"command": ["bash", "-lc", "false"]},
                     tool_result={"exit_code": 1, "error": "command failed"})
    r = norm(rec, pepper, aliases)
    assert r.event["status"] == "ERROR" and r.event["needs_attention"] is True
    assert r.event["error_label"] in S.ERROR_LABEL_ENUM


def test_posttooluse_success_not_error(pepper, aliases):
    rec = codex_hook("PostToolUse", tool_name="apply_patch",
                     tool_input={"input": "*** Begin Patch"}, tool_result={"exit_code": 0})
    r = norm(rec, pepper, aliases)
    assert r.event["status"] == "CODING"  # success -> normal category status


def test_subagentstop_is_done(pepper, aliases):
    r = norm(claude_hook("SubagentStop"), pepper, aliases)
    assert r.action == "post" and r.event["status"] == "DONE"


def test_precompact_sets_compacting_detail(pepper, aliases, server_client):
    r = norm(claude_hook("PreCompact"), pepper, aliases)
    assert r.event["status"] == "THINKING" and r.event["status_detail"] == "compacting"
    # server _to_envelope now forwards status_detail through the sanitizer.
    resp = server_client.post("/admin/event", json=r.event)
    assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------- #
# 8. reaper — bounded queue / orphaned tmp / dead-letter cap.
# --------------------------------------------------------------------------- #
def test_reaper_removes_orphaned_tmp(_isolated_state, monkeypatch):
    config.ensure_dirs()
    monkeypatch.setattr(config, "TMP_TTL_S", 0)  # any tmp counts as stale
    raw = '{"cwd":"/Users/hulu/secret/client-acme-prod","tool_input":{"command":"cat ~/.ssh/id_rsa"}}'
    (config.QUEUE_DIR / "0000-deadbeef.json.tmp").write_text(raw)
    reaped = daemon.reap(__import__("time").time() + 1)
    assert reaped["tmp"] == 1
    assert not list(config.QUEUE_DIR.glob("*.tmp"))


def test_reaper_caps_queue_oldest_first(_isolated_state, monkeypatch):
    import os as _os
    import time as _t
    config.ensure_dirs()
    monkeypatch.setattr(config, "MAX_QUEUE_FILES", 3)
    monkeypatch.setattr(config, "QUEUE_TTL_S", 1e9)  # isolate the count-cap from the age-TTL path
    now = _t.time()
    paths = []
    for i in range(6):
        p = config.QUEUE_DIR / f"{i:016d}-1-x.json"
        p.write_text(json.dumps(claude_hook("Stop")))
        _os.utime(p, (now - (6 - i), now - (6 - i)))  # recent, ascending (i=0 oldest)
        paths.append(p)
    reaped = daemon.reap(now)
    assert reaped["overflow"] == 3 and reaped["aged"] == 0
    # The 3 OLDEST are gone; the 3 newest remain.
    assert not paths[0].exists() and not paths[2].exists()
    assert paths[3].exists() and paths[5].exists()


def test_queue_dir_is_0700(_isolated_state):
    import stat
    config.ensure_dirs()
    mode = stat.S_IMODE(config.QUEUE_DIR.stat().st_mode)
    assert mode == 0o700, oct(mode)


# --------------------------------------------------------------------------- #
# 9. hook_sink never hangs on a never-EOF stdin (fire-and-forget <1s guarantee).
# --------------------------------------------------------------------------- #
def test_hook_sink_does_not_hang_on_open_pipe(_isolated_state):
    import time as _t
    qd = _isolated_state / "queue"
    hook_sink = config.SRC_DIR / "collector" / "hook_sink.py"
    env = dict(os.environ, AGENTLAMP_QUEUE_DIR=str(qd))
    # A pipe whose write end we keep open and never close => no EOF. The deadline
    # must still let the hook exit well under a couple seconds.
    p = subprocess.Popen([sys.executable, str(hook_sink), "--provider", "claude"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    p.stdin.write(b'{"hook_event_name":"Stop"')  # partial, no newline, never closed
    p.stdin.flush()
    start = _t.time()
    rc = p.wait(timeout=5)  # SIGALRM deadline (0.8s) should end it; 5s is the test safety net
    elapsed = _t.time() - start
    assert rc == 0
    assert elapsed < 3.0, f"hook hung for {elapsed:.1f}s"


# --------------------------------------------------------------------------- #
# 6. INTEGRATION — the emitted shorthand is accepted by the REAL server and
#    drives the right frame. Proves daemon output <-> server sanitizer parity.
# --------------------------------------------------------------------------- #
@pytest.fixture
def server_client():
    from fastapi.testclient import TestClient
    from agentlamp_server.app import app, _build_state

    app.state.frame = _build_state()
    return TestClient(app)


def test_emitted_shorthand_accepted_by_server(pepper, aliases, server_client):
    r = norm(claude_hook("PreToolUse", tool_name="Edit", tool_input={"file_path": "/x"}), pepper, aliases)
    resp = server_client.post("/admin/event", json=r.event)
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is True
    frame = server_client.get(
        "/api/v1/device/orb-01/frame",
        headers={"Authorization": "Bearer dev-local-token", "X-Frame-Schema-Version": "1"},
    ).json()
    assert frame["primary"]["status"] == "CODING"
    assert frame["primary"]["provider"] == "Claude"
    # The project alias the server stored leaks no path segment.
    assert "/Users/" not in json.dumps(frame)


def test_full_session_arc_drives_frame(pepper, aliases, server_client):
    """prompt -> THINKING, tool -> CODING, permission -> WAITING, stop -> DONE,
    each accepted by the real server and reflected in the frame."""
    arc = [
        (claude_hook("UserPromptSubmit", prompt="do the thing"), "THINKING"),
        (claude_hook("PreToolUse", tool_name="Write", tool_input={"file_path": "/x"}), "CODING"),
        (claude_hook("PermissionRequest", tool_name="Bash", tool_input={"command": "rm x"}), "WAITING"),
        (claude_hook("Stop"), "DONE"),
    ]
    for rec, expected in arc:
        r = norm(rec, pepper, aliases)
        resp = server_client.post("/admin/event", json=r.event)
        assert resp.status_code == 200, resp.text
        frame = server_client.get(
            "/api/v1/device/orb-01/frame",
            headers={"Authorization": "Bearer dev-local-token", "X-Frame-Schema-Version": "1"},
        ).json()
        assert frame["primary"]["status"] == expected, f"{rec['hook']['hook_event_name']} -> {frame['primary']['status']}"
