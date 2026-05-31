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
