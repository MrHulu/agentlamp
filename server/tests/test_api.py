"""HTTP API tests via FastAPI TestClient — auth, errors, pairing, admin, preview.

Contract: docs/api/device_frame_api.md (errors table, headers), docs/security/
pairing_and_auth.md (token exchange), docs/ui/display_spec.md (simulator).
"""
from __future__ import annotations

import json

AUTH = {"Authorization": "Bearer dev-local-token", "X-Frame-Schema-Version": "1"}


def test_frame_requires_bearer(client):
    r = client.get("/api/v1/device/orb-01/frame")  # no token
    assert r.status_code == 401
    assert r.json() == {"error": "bad_token", "retry": False}


def test_frame_bad_token(client):
    r = client.get("/api/v1/device/orb-01/frame", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert r.json()["error"] == "bad_token"


def test_frame_unknown_device(client):
    r = client.get("/api/v1/device/ghost-99/frame", headers=AUTH)
    assert r.status_code == 404
    assert r.json() == {"error": "unknown_device", "retry": False}


def test_frame_ok_and_header_echo(client):
    r = client.get("/api/v1/device/orb-01/frame", headers=AUTH)
    assert r.status_code == 200
    assert r.headers.get("X-Frame-Schema-Version") == "1"
    body = r.json()
    assert body["v"] == 1
    assert body["device_id"] == "orb-01"
    # Under 2 KB on the wire.
    assert len(r.content) < 2048


def test_frame_schema_negotiation_header(client):
    r = client.get(
        "/api/v1/device/orb-01/frame",
        headers={"Authorization": "Bearer dev-local-token", "X-Frame-Schema-Version": "9"},
    )
    assert r.status_code == 200
    assert r.headers.get("X-Frame-Schema-Version") == "1"
    assert r.json()["v"] == 1


def test_frame_malformed_schema_version_header_uses_contract_envelope(client):
    """Regression (P1): a non-integer X-Frame-Schema-Version must NOT surface the
    FastAPI default {detail:...} 422 — it is coerced to the server version and a
    normal 200 frame is returned (every error on this route is {error,retry})."""
    r = client.get(
        "/api/v1/device/orb-01/frame",
        headers={"Authorization": "Bearer dev-local-token", "X-Frame-Schema-Version": "abc"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "detail" not in body  # not the FastAPI validation-error shape
    assert body["v"] == 1
    assert r.headers.get("X-Frame-Schema-Version") == "1"


def test_frame_missing_schema_version_header_ok(client):
    # Absent header → negotiate to server default, 200.
    r = client.get(
        "/api/v1/device/orb-01/frame", headers={"Authorization": "Bearer dev-local-token"}
    )
    assert r.status_code == 200
    assert r.json()["v"] == 1


def test_build_frame_non_int_schema_version_raises_sanitization_error():
    """Regression (P1): build_frame coerces a non-int schema_version into a
    SanitizationError, never a raw ValueError the caller could 500 on."""
    from agentlamp_server import sanitize as S
    from agentlamp_server.state import FrameState

    st = FrameState(device_token="dev-local-token", device_id="orb-01")
    import pytest

    with pytest.raises(S.SanitizationError):
        st.build_frame("orb-01", schema_version="not-an-int")


def test_admin_event_drives_frame(client):
    ev = {"provider": "codex", "account": "main", "status": "WAITING", "project": "project-a", "task": "waiting"}
    r = client.post("/admin/event", json=ev)
    assert r.status_code == 200
    assert r.json()["applied"] is True
    # Frame now reflects the alert.
    f = client.get("/api/v1/device/orb-01/frame", headers=AUTH).json()
    assert f["scene"] == "alert"
    assert f["primary"]["status"] == "WAITING"
    assert f["primary"]["provider"] == "Codex"


def test_admin_event_rejects_leak(client):
    # A path in a field rejects the event (422) and never reaches state.
    ev = {"provider": "codex", "account": "main", "status": "CODING", "project": "/Users/hulu/work/x"}
    r = client.post("/admin/event", json=ev)
    assert r.status_code == 422
    assert r.json()["rejected"] is True


def test_admin_quota_drives_alert(client):
    client.post("/admin/event", json={"provider": "claude", "account": "work", "status": "CODING", "project": "project-a"})
    r = client.post("/admin/quota", json={"provider": "claude", "account": "work", "window_type": "5h", "used_ratio": 0.95, "confidence": "medium"})
    assert r.status_code == 200
    f = client.get("/api/v1/device/orb-01/frame", headers=AUTH).json()
    assert f["scene"] == "alert"
    assert f["accent"] == "red"


# --------------------------------------------------------------------------- #
# /admin/quota goes through the SINGLE quota sink (state.set_quota), so the same
# default-deny gate guards BOTH the relay path AND /admin/quota (docs/devlog/16 I1,
# 2026-06-03 hardening). Previously /admin/quota wrote a raw account straight to the frame.
# --------------------------------------------------------------------------- #
def test_admin_quota_rejects_path_account(client):
    """A path/forbidden account_alias must be REJECTED (422) by the quota sink — never written
    into frame.quota[].account served to the device."""
    r = client.post("/admin/quota", json={"provider": "claude", "account": "/tmp/secret",
                                           "window_type": "5h", "used_ratio": 0.5})
    assert r.status_code == 422
    assert r.json()["rejected"] is True
    f = client.get("/api/v1/device/orb-01/frame", headers=AUTH).json()
    assert all(q["account"] != "/tmp/secret" for q in f.get("quota", []))


def test_admin_quota_rejects_tilde_and_plan_tier_account(client):
    for bad in ("~/secret", "/etc/passwd", "Max"):
        r = client.post("/admin/quota", json={"provider": "claude", "account": bad,
                                              "window_type": "5h", "used_ratio": 0.5})
        assert r.status_code == 422, bad


def test_admin_quota_rejects_bool_used_ratio(client):
    """float(True)==1.0 must NOT slip a bool past the sink (parity with the TS gate). The route
    passes used_ratio RAW so the sink's bool-reject fires."""
    r = client.post("/admin/quota", json={"provider": "claude", "account": "main",
                                          "window_type": "5h", "used_ratio": True})
    assert r.status_code == 422
    assert r.json()["rejected"] is True


def test_admin_quota_rejects_out_of_range_and_bad_window(client):
    r = client.post("/admin/quota", json={"provider": "claude", "account": "main",
                                          "window_type": "5h", "used_ratio": 1.5})
    assert r.status_code == 422
    r = client.post("/admin/quota", json={"provider": "claude", "account": "main",
                                          "window_type": "daily", "used_ratio": 0.5})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# /admin/* LAN access control (2026-06-03 hardening). The local server binds 0.0.0.0;
# /admin/* is gated to loopback OR a configured AGENTLAMP_LOCAL_ADMIN_TOKEN bearer. The
# device path (/frame + /pair) is unaffected. TestClient is treated as loopback.
# --------------------------------------------------------------------------- #
def test_admin_local_client_allowed(client):
    """The in-process TestClient (synthetic 'testclient' host) is treated as local → allowed."""
    r = client.post("/admin/heartbeat")
    assert r.status_code == 200


def test_admin_forbidden_for_non_local_client():
    """A non-loopback client with no admin token is rejected 403 on /admin/* (but NOT on /frame
    or /pair)."""
    from fastapi.testclient import TestClient
    from agentlamp_server import app as app_mod

    app_mod.app.state.frame = app_mod._build_state()
    # Drive the app with a routable client host (simulates a LAN peer).
    c = TestClient(app_mod.app, client=("192.168.1.50", 54321))
    r = c.post("/admin/heartbeat")
    assert r.status_code == 403
    assert r.json() == {"error": "admin_forbidden", "retry": False}
    # The device-facing routes are NOT gated by this control.
    rf = c.get("/api/v1/device/orb-01/frame")  # 401 (bad_token), NOT 403 admin_forbidden
    assert rf.status_code == 401
    rp = c.post("/api/v1/device/orb-01/pair", json={})  # 400 bad_pairing_code, NOT 403
    assert rp.status_code == 400


def test_admin_token_allows_non_local_client(monkeypatch):
    """A non-loopback client presenting the configured AGENTLAMP_LOCAL_ADMIN_TOKEN is allowed."""
    import importlib

    monkeypatch.setenv("AGENTLAMP_LOCAL_ADMIN_TOKEN", "s3cret-admin-token")
    from agentlamp_server import app as app_mod

    importlib.reload(app_mod)  # re-read LOCAL_ADMIN_TOKEN from env at import time
    try:
        from fastapi.testclient import TestClient

        app_mod.app.state.frame = app_mod._build_state()
        c = TestClient(app_mod.app, client=("192.168.1.50", 54321))
        # Wrong/absent token → 403.
        assert c.post("/admin/heartbeat").status_code == 403
        # Correct token → allowed.
        r = c.post("/admin/heartbeat", headers={"Authorization": "Bearer s3cret-admin-token"})
        assert r.status_code == 200
    finally:
        monkeypatch.delenv("AGENTLAMP_LOCAL_ADMIN_TOKEN", raising=False)
        importlib.reload(app_mod)  # restore module state for subsequent tests


def test_admin_reset(client):
    client.post("/admin/event", json={"provider": "claude", "account": "work", "status": "CODING", "project": "project-a"})
    r = client.post("/admin/reset")
    assert r.status_code == 200
    f = client.get("/api/v1/device/orb-01/frame", headers=AUTH).json()
    assert f["scene"] in ("sleep", "offline")


def test_pair_bogus_code_rejected(client):
    # Contract (pairing_and_auth.md): a bogus / unissued code never mints a token,
    # even for the dev device. Regression for the pairing-token-leak finding.
    r = client.post("/api/v1/device/orb-01/pair", json={"pairing_code": "irrelevant-stub"})
    assert r.status_code == 400
    assert r.json() == {"error": "bad_pairing_code", "retry": False}


def test_pair_absent_code_rejected(client):
    # An empty / absent pairing code is also rejected (no token leak).
    r = client.post("/api/v1/device/orb-01/pair", json={})
    assert r.status_code == 400
    assert r.json()["error"] == "bad_pairing_code"


def test_pair_dev_device_with_valid_issued_code(client):
    # The dev device pairs ONLY with a valid issued one-time code, returned once.
    issued = client.post("/admin/device/orb-01/code", json={"device_token": "dev-local-token"})
    assert issued.status_code == 200
    code = issued.json()["pairing_code"]
    r = client.post("/api/v1/device/orb-01/pair", json={"pairing_code": code})
    assert r.status_code == 200
    assert r.json()["device_token"] == "dev-local-token"
    assert r.json()["device_id"] == "orb-01"
    # Burned on use — replay fails.
    r2 = client.post("/api/v1/device/orb-01/pair", json={"pairing_code": code})
    assert r2.status_code == 400


def test_pair_real_code_flow(client):
    # Issue a one-time code for a NEW device, then redeem it.
    issued = client.post("/admin/device/orb-02/code", json={"device_token": "tok-orb-02"})
    assert issued.status_code == 200
    code = issued.json()["pairing_code"]
    r = client.post("/api/v1/device/orb-02/pair", json={"pairing_code": code})
    assert r.status_code == 200
    assert r.json()["device_token"] == "tok-orb-02"
    # Code is burned — second redeem fails.
    r2 = client.post("/api/v1/device/orb-02/pair", json={"pairing_code": code})
    assert r2.status_code == 400


def test_pair_bad_device_id(client):
    r = client.post("/api/v1/device/bad id!/pair", json={"pairing_code": "x"})
    assert r.status_code == 404


def test_preview_serves_html(client):
    r = client.get("/preview")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "AgentLamp" in r.text
    assert "orb-01" in r.text  # device id substituted
    assert "/admin/event" in r.text  # inject controls present


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_token_never_in_url(client):
    # The frame endpoint ignores a query-string token (contract: tokens in URL
    # are rejected). Passing ?token=... must NOT authenticate.
    r = client.get("/api/v1/device/orb-01/frame?token=dev-local-token")
    assert r.status_code == 401
