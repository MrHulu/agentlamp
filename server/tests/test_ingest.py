"""Relay signed-ingest tests — the security acceptance from security_model.md +
collector_ingest_api.md: HMAC verify, replay (timestamp/nonce), idempotency, limits,
charset, and per-event sanitization (independent cloud gate, no poison-pill stall).
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from agentlamp_server import ingest as I

KID = "k1"
SECRET = b"test-collector-secret"
T = 1_780_000_000          # fixed "now" the verifier sees
CID = "collector-mac-main"
PATH = f"/api/v1/collectors/{CID}/events"


@pytest.fixture
def relay_client():
    """TestClient whose ingest verifier holds ONE known key and a FIXED clock, so signature /
    timestamp / nonce / idempotency behaviour is deterministic."""
    from fastapi.testclient import TestClient
    from agentlamp_server.app import app, _build_state

    app.state.frame = _build_state()
    app.state.ingest = I.IngestVerifier(
        I.KeyStore({KID: SECRET}),
        now=lambda: float(T),
        nonce_store=I.NonceStore(I.NONCE_TTL_S),
        idem_store=I.IdempotencyStore(),
    )
    return TestClient(app)


def _body(events=None):
    return {
        "schema_version": 1,
        "collector_id": CID,
        "sent_at": T,
        "events": events if events is not None else [{
            "event_id": "evt_1",
            "event_type": "session.upsert",
            "provider": "claude",
            "account_alias": "main",
            "event_time": T - 2,
            "payload": {"session_id": "hmac:abc123", "project_alias": "project-a",
                        "status": "CODING", "model": "claude", "task_label": "implementing"},
        }],
    }


def _post(client, body_obj, *, kid=KID, secret=SECRET, ts=T, nonce="ab" * 16,
          idem=None, break_sig=False, break_hash=False, cid=CID):
    raw = json.dumps(body_obj).encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    if break_hash:
        sha = "0" * 64
    path = f"/api/v1/collectors/{cid}/events"
    canon = "\n".join(["v1", "POST", path, kid, str(ts), nonce, sha])
    sig = hmac.new(secret, canon.encode(), hashlib.sha256).hexdigest()
    if break_sig:
        sig = "f" * 64
    headers = {
        "X-ACO-Key-Id": kid,
        "X-ACO-Timestamp": str(ts),
        "X-ACO-Nonce": nonce,
        "X-ACO-Payload-SHA256": sha,
        "X-ACO-Signature": "v1=" + sig,
        "Content-Type": "application/json",
    }
    if idem:
        headers["Idempotency-Key"] = idem
    return client.post(path, content=raw, headers=headers)


# --------------------------------------------------------------------------- #
# Happy path + the security acceptance gates.
# --------------------------------------------------------------------------- #
def test_valid_signed_ingest_accepted(relay_client):
    r = _post(relay_client, _body())
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] and j["accepted"] == 1 and j["rejected"] == 0
    assert j["results"][0]["status"] == "accepted"


def test_bad_signature_401(relay_client):
    r = _post(relay_client, _body(), break_sig=True)
    assert r.status_code == 401 and r.json()["reason"] == "bad_signature"


def test_stale_timestamp_401_with_server_time(relay_client):
    r = _post(relay_client, _body(), ts=T - 400, nonce="cd" * 16)
    assert r.status_code == 401 and r.json()["reason"] == "stale_timestamp"
    assert r.json()["server_time"] == T   # collector resyncs from this, no loop


def test_reused_nonce_409(relay_client):
    n = "ef" * 16
    assert _post(relay_client, _body(), nonce=n).status_code == 200
    r2 = _post(relay_client, _body(), nonce=n)   # same nonce, otherwise valid
    assert r2.status_code == 409 and r2.json()["reason"] == "reused_nonce"


def test_payload_hash_mismatch_400(relay_client):
    r = _post(relay_client, _body(), break_hash=True)
    assert r.status_code == 400 and r.json()["reason"] == "payload_hash_mismatch"


def test_unknown_or_revoked_kid_403(relay_client):
    r = _post(relay_client, _body(), kid="ghost", secret=b"whatever")
    assert r.status_code == 403 and r.json()["reason"] == "collector_revoked"


def test_bad_collector_id_charset_400(relay_client):
    r = _post(relay_client, _body(), cid="bad id!")
    assert r.status_code == 400 and r.json()["reason"] == "bad_collector_id"


def test_batch_too_large_413(relay_client):
    many = [dict(_body()["events"][0], event_id=f"e{i}") for i in range(I.MAX_EVENTS_PER_REQUEST + 1)]
    r = _post(relay_client, _body(many))
    assert r.status_code == 413 and r.json()["reason"] == "batch_too_large"


def test_body_too_large_413(relay_client):
    big = _body()
    big["pad"] = "x" * (I.MAX_BODY_BYTES + 10)
    r = _post(relay_client, big)
    assert r.status_code == 413 and r.json()["reason"] == "body_too_large"


def test_idempotency_returns_prior_result(relay_client):
    r1 = _post(relay_client, _body(), nonce="11" * 16, idem="batch-001")
    assert r1.status_code == 200 and "duplicate" not in r1.json()
    # Retry: SAME idempotency key, FRESH nonce (a legit retry) → prior result, not re-applied.
    r2 = _post(relay_client, _body(), nonce="22" * 16, idem="batch-001")
    assert r2.status_code == 200 and r2.json().get("duplicate") is True
    assert r2.json()["ingest_id"] == r1.json()["ingest_id"]


def test_poison_event_rejected_per_event_not_request(relay_client):
    """A leak-bearing event is rejected in results[] (HTTP 200) while clean events still apply —
    one poison event must not stall the batch (cloud independent sanitize gate)."""
    events = [
        {"event_id": "ok1", "event_type": "session.upsert", "provider": "claude",
         "account_alias": "main", "payload": {"session_id": "hmac:a", "project_alias": "project-a",
                                              "status": "CODING", "task_label": "implementing"}},
        {"event_id": "leak", "event_type": "session.upsert", "provider": "claude",
         "account_alias": "main", "payload": {"session_id": "hmac:b",
                                              "project_alias": "/Users/hulu/secret/path",
                                              "status": "CODING", "task_label": "implementing"}},
    ]
    r = _post(relay_client, _body(events))
    assert r.status_code == 200
    j = r.json()
    res = {x["event_id"]: x for x in j["results"]}
    assert res["ok1"]["status"] == "accepted"
    assert res["leak"]["status"] == "rejected"   # path leak caught by the cloud gate
    assert j["accepted"] == 1 and j["rejected"] == 1


def test_quota_window_path_account_rejected_per_event(relay_client):
    """CRITICAL (docs/devlog/16 I1): the quota.window branch previously called set_quota DIRECTLY
    with the attacker-controlled account_alias/provider, bypassing the validate gate — a signed
    batch could put "/Users/.../secret" into frame.quota[].account served to the device. The quota
    branch now passes the SAME default-deny gate as session.* (validate_quota_event)."""
    events = [
        {"event_id": "q_leak", "event_type": "quota.window", "provider": "claude",
         "account_alias": "/Users/hulu/secret-project",
         "payload": {"window_type": "5h", "used_ratio": 0.95}},
        {"event_id": "q_ok", "event_type": "quota.window", "provider": "claude",
         "account_alias": "main", "payload": {"window_type": "5h", "used_ratio": 0.95}},
        {"event_id": "q_nan", "event_type": "quota.window", "provider": "claude",
         "account_alias": "work", "payload": {"window_type": "5h", "used_ratio": "x"}},
    ]
    r = _post(relay_client, _body(events))
    assert r.status_code == 200
    j = r.json()
    res = {x["event_id"]: x for x in j["results"]}
    assert res["q_leak"]["status"] == "rejected"
    assert res["q_ok"]["status"] == "accepted"
    assert res["q_nan"]["status"] == "rejected"   # float("x") raises → rejected
    assert j["accepted"] == 1 and j["rejected"] == 2
    # The device frame NEVER carries the leaked account — only the validated "main".
    frame = relay_client.app.state.frame.build_frame("orb-01")
    accounts = [q["account"] for q in frame.get("quota", [])]
    assert "/Users/hulu/secret-project" not in accounts


def test_empty_keystore_rejects_all_403():
    """A relay with NO provisioned key (e.g. local mode's default) accepts nothing."""
    from fastapi.testclient import TestClient
    from agentlamp_server.app import app, _build_state
    app.state.frame = _build_state()
    app.state.ingest = I.IngestVerifier(I.KeyStore({}), now=lambda: float(T))
    r = _post(TestClient(app), _body())
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# Unit: canonical string + signing round-trip (both sides build the same bytes).
# --------------------------------------------------------------------------- #
def test_canonical_string_exact_shape():
    cs = I.canonical_string("POST", PATH, KID, str(T), "ab" * 16, "0" * 64)
    assert cs == "v1\nPOST\n" + PATH + "\n" + KID + "\n" + str(T) + "\n" + "ab" * 16 + "\n" + "0" * 64
    assert cs.count("\n") == 6 and not cs.endswith("\n")
