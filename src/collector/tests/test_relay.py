"""PIECE K — relay-mode signed push + enroll tests.

Covers:
  * the FROZEN HMAC byte-spec: the collector's signing of a known body reproduces
    ``tests/fixtures/parity/hmac_vectors.json`` exactly (I2 — cross-language parity);
  * the relay-mode daemon drain: signed push success (end-to-end against a real
    local stub that VERIFIES the signature via the server's IngestVerifier),
    401 stale_timestamp resync-once (no loop), per-event reject → dead-letter,
    request-level reject → dead-letter, transport failure → leave + retry;
  * local mode unchanged when relay is off;
  * the OS-keyring secret store round-trip + delete (revoke);
  * ``agentlamp enroll`` idempotency + ``revoke``.

Run:  cd <repo> && ./.venv/bin/python -m pytest src/collector/tests/test_relay.py -q
"""
from __future__ import annotations

import importlib
import json
import os
import pathlib
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from collector import config, daemon, netpost, relaypost, secretstore
from agentlamp_server.ingest import (
    IngestVerifier,
    KeyStore,
    canonical_string,
    payload_sha256_hex,
    sign,
)

_FIXTURES = config.REPO_ROOT / "tests" / "fixtures" / "parity"


def claude_hook(event, **fields):
    base = {
        "session_id": "claude-sess-abc123",
        "cwd": "/Users/hulu/secret/client-acme-prod",
        "hook_event_name": event,
    }
    base.update(fields)
    return {"provider": "claude", "received_at": 1.0, "hook": base}


# --------------------------------------------------------------------------- #
# 1. HMAC parity — the collector reproduces the frozen byte-spec (I2).
# --------------------------------------------------------------------------- #
def test_collector_sign_matches_hmac_vectors_parity_corpus():
    """The collector's signing of each known body MUST reproduce hmac_vectors.json
    byte-for-byte: payload sha256, canonical string, AND signature. This is the
    cross-language release gate — the TS side asserts the SAME corpus."""
    vectors = json.loads((_FIXTURES / "hmac_vectors.json").read_text())
    assert vectors, "parity corpus must not be empty"
    for v in vectors:
        body = v["body_utf8"].encode("utf-8")
        secret = v["secret_utf8"].encode("utf-8")
        headers = relaypost.sign_headers(
            secret=secret, kid=v["kid"], collector_id=v["collector_id"],
            raw_body=body, timestamp=v["timestamp"], nonce=v["nonce"],
        )
        # payload sha256 over the exact raw bytes
        assert headers["X-ACO-Payload-SHA256"] == v["payload_sha256"], v["kid"]
        # canonical string (rebuilt from the same fields the header carries)
        canon = canonical_string(
            "POST", v["path"], v["kid"], str(v["timestamp"]), v["nonce"], v["payload_sha256"],
        )
        assert canon == v["canonical_string"], v["kid"]
        # signature carries the v1= prefix; strip it before comparing
        assert headers["X-ACO-Signature"].startswith("v1=")
        assert headers["X-ACO-Signature"][3:] == v["signature"], v["kid"]


def test_payload_sha256_is_over_exact_bytes_not_reserialized():
    """The hash MUST be over the bytes that are sent. build_request_body returns the
    buffer; signing the SAME buffer reproduces a digest the server recomputes."""
    events = [relaypost.build_ingest_event(
        {"provider": "claude", "account": "main", "project": "project-x",
         "provider_session_id": "hmac:abc", "status": "CODING", "model": "claude"},
        source_seq=1, event_time=1780000000, pepper=b"test-pepper-32-bytes-aaaaaaaaaaaa")]
    raw = relaypost.build_request_body(events, collector_id="laptop-2", batch_id="b1")
    assert payload_sha256_hex(raw) == relaypost.payload_sha256_hex(raw)
    # No raw path/secret in the signed bytes (collector already sanitized).
    assert "/Users/" not in raw.decode("utf-8")


# --------------------------------------------------------------------------- #
# A real local relay stub that VERIFIES the signature with the cloud's own
# IngestVerifier (so the SIGNATURE / replay / clock-window / revocation behaviour
# is exercised against the real verifier). It does NOT run the per-event
# VALIDATE-only gate — it returns ``accepted`` once the signature verifies, which
# is enough to drive the drain failure-policy tests (resync / dead-letter / retry).
# The payload-shape contract (I1/I2: collector OUTPUT passes the cloud gate) is
# proven separately by ``test_relay_e2e_collector_output_passes_live_validate_gate``,
# which drives the batch through the REAL ``app.collector_ingest`` route.
# --------------------------------------------------------------------------- #
def _make_verifying_handler(kid: str, secret: bytes, *, force=None, server_time=None,
                            captured=None):
    keys = KeyStore({kid: secret})
    verifier = IngestVerifier(keys)

    class _RelayStub(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n)
            collector_id = self.path.split("/")[-2]
            v = verifier.verify(
                collector_id=collector_id, method="POST", path=self.path, raw_body=raw,
                kid=self.headers.get("X-ACO-Key-Id", ""),
                timestamp=self.headers.get("X-ACO-Timestamp", ""),
                nonce=self.headers.get("X-ACO-Nonce", ""),
                payload_sha256=(self.headers.get("X-ACO-Payload-SHA256", "")).lower(),
                signature=(self.headers.get("X-ACO-Signature", "")[3:]
                           if (self.headers.get("X-ACO-Signature", "")).lower().startswith("v1=")
                           else self.headers.get("X-ACO-Signature", "")),
            )
            if captured is not None:
                captured.append({"raw": raw, "headers": dict(self.headers)})
            if force == "stale":
                self._json(401, {"ok": False, "reason": "stale_timestamp",
                                 "server_time": server_time or 0})
                return
            if not v.ok:
                self._json(v.http_status, {"ok": False, "reason": v.reason,
                                           "server_time": v.server_time})
                return
            body = json.loads(raw.decode("utf-8"))
            results = []
            for ev in body.get("events", []):
                if force == "event_reject":
                    results.append({"event_id": ev.get("event_id"), "status": "rejected",
                                    "reason": "sanitization_failed"})
                else:
                    results.append({"event_id": ev.get("event_id"), "status": "accepted"})
            self._json(200, {"ok": True, "server_time": v.server_time, "ingest_id": "ing_x",
                             "accepted": sum(1 for r in results if r["status"] == "accepted"),
                             "rejected": sum(1 for r in results if r["status"] == "rejected"),
                             "duplicates": 0, "results": results})

        def _json(self, code, obj):
            b = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    return _RelayStub


def _make_admin_stub(admin_token: str, *, captured=None, enrolled=None):
    """A stub relay /admin endpoint mirroring the REAL cloud route's contract
    (src/cloud/src/index.ts): constant bearer gate, JSON body, uniform errors.

      POST /admin/collectors/{kid}/enroll  body={"secret": "..."}  → 200 {"ok":true,"kid":kid}
      POST /admin/collectors/{kid}/revoke  body={}                 → 200 {"ok":true,"revoked":kid}

    Auth: 403 admin_disabled when the relay's token is unset; 401 admin_unauthorized
    when the bearer is missing/wrong; 400 bad_request when enroll has an empty secret.
    ``captured`` records each request; ``enrolled`` is the live {kid: secret} registry
    (so a test can prove the kid+secret actually landed server-side, then got revoked).
    """
    reg = enrolled if enrolled is not None else {}

    class _AdminStub(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n) if n else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                body = {}
            parts = [p for p in self.path.split("/") if p]  # ["admin","collectors",kid,action]
            kid = parts[2] if len(parts) >= 3 else ""
            action = parts[3] if len(parts) >= 4 else ""
            if captured is not None:
                captured.append({"path": self.path, "headers": dict(self.headers),
                                 "body": body, "kid": kid, "action": action})
            # 1. fail-CLOSED if the relay's admin token is unset.
            if not admin_token:
                self._json(403, {"error": "admin_disabled", "retry": False})
                return
            # 2. bearer presence + match (constant-ish; the test only needs correctness).
            auth = self.headers.get("Authorization", "")
            if not auth.lower().startswith("bearer "):
                self._json(401, {"error": "admin_unauthorized", "retry": False})
                return
            presented = auth[7:].strip()
            if presented != admin_token:
                self._json(401, {"error": "admin_unauthorized", "retry": False})
                return
            # 3. dispatch.
            if action == "enroll":
                secret = str(body.get("secret", "")).strip()
                if not secret:
                    self._json(400, {"error": "bad_request", "retry": False})
                    return
                reg[kid] = secret
                self._json(200, {"ok": True, "kid": kid})
                return
            if action == "revoke":
                reg.pop(kid, None)
                self._json(200, {"ok": True, "revoked": kid})
                return
            self._json(404, {"error": "not_found", "retry": False})

        def _json(self, code, obj):
            b = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    return _AdminStub


def _relay_env(monkeypatch, host, kid="k1", secret="test-collector-secret", collector_id="laptop-2"):
    monkeypatch.setenv("AGENTLAMP_MODE", "relay")
    monkeypatch.setenv("AGENTLAMP_RELAY_HOST", host)
    monkeypatch.setenv("AGENTLAMP_RELAY_KID", kid)
    monkeypatch.setenv("AGENTLAMP_COLLECTOR_ID", collector_id)
    monkeypatch.setenv("AGENTLAMP_RELAY_SECRET", secret)
    monkeypatch.setenv("AGENTLAMP_LOCAL_LABELS", "0")  # relay → HMAC labels
    import collector.config as cfgmod
    importlib.reload(cfgmod)
    # daemon imported config at module load; re-point its reference too.
    importlib.reload(daemon)


def _clear_relay_env(monkeypatch):
    for k in ("AGENTLAMP_MODE", "AGENTLAMP_RELAY_HOST", "AGENTLAMP_RELAY_KID",
              "AGENTLAMP_COLLECTOR_ID", "AGENTLAMP_RELAY_SECRET", "AGENTLAMP_LOCAL_LABELS"):
        monkeypatch.delenv(k, raising=False)
    import collector.config as cfgmod
    importlib.reload(cfgmod)
    importlib.reload(daemon)


# --------------------------------------------------------------------------- #
# 2. relay drain — signed push success, verified end-to-end by the stub.
# --------------------------------------------------------------------------- #
def test_relay_drain_signs_and_pushes_accepted(_isolated_state, monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _make_verifying_handler("k1", b"test-collector-secret"))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        _relay_env(monkeypatch, f"http://127.0.0.1:{port}")
        daemon.config.ensure_dirs()
        qf = daemon.config.QUEUE_DIR / "0000000000000001-1-aaaaaaaa.json"
        qf.write_text(json.dumps(claude_hook("PreToolUse", tool_name="Edit", tool_input={})))
        counts = daemon.drain_once(daemon.config.load_pepper(), daemon.config.load_aliases())
        assert counts["posted"] == 1
        assert not qf.exists()                     # accepted → deleted
        assert not list(daemon.config.DEAD_LETTER_DIR.glob("*.reason.json"))
    finally:
        srv.shutdown()
        _clear_relay_env(monkeypatch)


# --------------------------------------------------------------------------- #
# 2b. END-TO-END through the REAL cloud route (FastAPI TestClient) — proves the
#     collector OUTPUT passes the live VALIDATE-only gate (BUILD-SPEC I1/I2).
#
#     This is the contract-proving test the stub above can NOT prove: the stub
#     returns ``accepted`` after verifying only the HMAC SIGNATURE; it never runs
#     ``validate_sanitized_event``. Here the request hits the production
#     ``app.collector_ingest`` route, which (a) verifies the signature via the real
#     ``IngestVerifier`` and (b) drives every event through ``state.apply_validated_event
#     → validate.validate_sanitized_event`` — the VALIDATE-only gate (I1). If the
#     collector pushed a non-output shape (raw ``session_title``, a payload
#     ``updated_at``, a multi-segment alias), the gate would REJECT and ``rejected``
#     would be ``> 0``. ``accepted == 1 and rejected == 0`` ⇒ collector↔cloud agreement.
# --------------------------------------------------------------------------- #
def _signed_relay_request(client, *, collector_id, kid, secret, events_body):
    """Sign ``events_body`` (the request-body bytes) the way the collector does and
    POST it through the real FastAPI route. Returns the parsed JSON response."""
    headers = relaypost.sign_headers(
        secret=secret, kid=kid, collector_id=collector_id, raw_body=events_body,
        idempotency_key=f"{collector_id}:e2e-1",
    )
    headers["Idempotency-Key"] = f"{collector_id}:e2e-1"
    r = client.post(
        f"/api/v1/collectors/{collector_id}/events", content=events_body, headers=headers,
    )
    return r


def test_relay_e2e_collector_output_passes_live_validate_gate(_isolated_state, monkeypatch):
    """END-TO-END: a real collector relay batch through the REAL app.collector_ingest
    route + live VALIDATE-only gate asserts accepted==1, rejected==0. Proves I1/I2
    collector↔cloud agreement: the collector pushes the sanitize_event OUTPUT shape
    (display_title as title-<hmac>, neutral aliases, canonical enums, NO session_title,
    NO updated_at), which the cloud's validate-only gate accepts.

    Regression for the BLOCKING bug: the collector previously hand-mapped raw fields
    and added payload.updated_at, so the cloud REJECTED every event (accepted==0).
    """
    from fastapi.testclient import TestClient

    from agentlamp_server import app as app_mod
    from agentlamp_server import ingest as I

    kid, secret, collector_id = "k1", b"e2e-collector-secret", "laptop-e2e"
    pepper = b"agentlamp-test-pepper-32-bytes!!"
    aliases = relaypost.S.AliasMap()

    # Provision the relay key the same way production does: AGENTLAMP_COLLECTOR_KEYS.
    monkeypatch.setenv("AGENTLAMP_COLLECTOR_KEYS", f"{kid}:{secret.decode()}")
    app_mod.app.state.ingest = I.IngestVerifier(I.load_keys_from_env(os.environ))
    app_mod.app.state.frame = app_mod._build_state()  # fresh materialized state
    client = TestClient(app_mod.app)

    # A realistic shorthand as normalize_record emits in relay mode: neutral project
    # alias already applied, plus a RAW free-text session_title (the field that, if
    # hand-mapped, would fail the neutral-alias shape) and an enum tool_category.
    shorthand = {
        "provider": "claude", "account": "main", "project": "project-7f3a9c",
        "provider_session_id": "hmac:abc123def456", "model": "claude",
        "status": "CODING", "tool_category": "edit",
        "session_title": "Fix the login bug",  # raw → sanitizer → title-<hmac>
        "provider_event_name": "PreToolUse",
    }
    # Build the ingest event with the collector's OWN builder (runs sanitize_event).
    ev = relaypost.build_ingest_event(shorthand, source_seq=1, pepper=pepper, aliases=aliases)
    # Proof the OUTPUT shape is what we push: title-<hmac>, no raw title, no updated_at.
    assert ev["payload"]["display_title"].startswith("title-")
    assert "session_title" not in ev["payload"]
    assert "updated_at" not in ev["payload"]

    raw = relaypost.build_request_body([ev], collector_id=collector_id, batch_id="e2e-b1")
    r = _signed_relay_request(client, collector_id=collector_id, kid=kid, secret=secret,
                              events_body=raw)

    assert r.status_code == 200, r.text
    body = r.json()
    # The collector output passed the LIVE validate-only gate end-to-end.
    assert body["accepted"] == 1, body
    assert body["rejected"] == 0, body
    assert body["results"][0]["status"] == "accepted", body
    # The materialized state actually upserted the session (the gate applied it).
    frame = app_mod.app.state.frame.build_frame(app_mod.DEV_DEVICE_ID)
    assert frame  # a real frame was generated from the accepted event

    monkeypatch.delenv("AGENTLAMP_COLLECTOR_KEYS", raising=False)


def test_relay_e2e_payload_with_updated_at_would_be_rejected(_isolated_state, monkeypatch):
    """Negative control for the e2e gate: a hand-mapped payload that carries the
    disallowed ``updated_at`` key (the OLD collector behaviour) is REJECTED by the
    SAME live route — so the positive test above is proving a real gate, not a stub
    that rubber-stamps everything."""
    from fastapi.testclient import TestClient

    from agentlamp_server import app as app_mod
    from agentlamp_server import ingest as I

    kid, secret, collector_id = "k1", b"e2e-collector-secret", "laptop-e2e2"
    monkeypatch.setenv("AGENTLAMP_COLLECTOR_KEYS", f"{kid}:{secret.decode()}")
    app_mod.app.state.ingest = I.IngestVerifier(I.load_keys_from_env(os.environ))
    app_mod.app.state.frame = app_mod._build_state()
    client = TestClient(app_mod.app)

    bad_event = {
        "event_id": "evt_bad01", "event_type": "session.status", "provider": "claude",
        "provider_event_name": "PreToolUse", "account_alias": "main", "source_seq": 1,
        "event_time": 1780000000, "dedupe_key": "claude:x:1",
        "payload": {
            "status": "CODING", "model": "claude", "account_alias": "main",
            "project_alias": "project-a", "session_id": "hmac:abc",
            "updated_at": 1780000000,  # NOT an allowed payload key (envelope-level only)
        },
    }
    raw = relaypost.build_request_body([bad_event], collector_id=collector_id, batch_id="e2e-b2")
    r = _signed_relay_request(client, collector_id=collector_id, kid=kid, secret=secret,
                              events_body=raw)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] == 0 and body["rejected"] == 1, body
    assert "updated_at" in body["results"][0]["reason"], body

    monkeypatch.delenv("AGENTLAMP_COLLECTOR_KEYS", raising=False)


def test_relay_push_carries_no_raw_path(_isolated_state, monkeypatch):
    captured = []
    srv = HTTPServer(("127.0.0.1", 0),
                     _make_verifying_handler("k1", b"test-collector-secret", captured=captured))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        _relay_env(monkeypatch, f"http://127.0.0.1:{port}")
        daemon.config.ensure_dirs()
        (daemon.config.QUEUE_DIR / "0000000000000009-1-zzzz.json").write_text(
            json.dumps(claude_hook("PreToolUse", tool_name="Read",
                                    tool_input={"file_path": "/Users/hulu/.ssh/id_rsa"})))
        daemon.drain_once(daemon.config.load_pepper(), daemon.config.load_aliases())
        assert captured, "stub must have received the push"
        sent = captured[0]["raw"].decode("utf-8")
        assert "/Users/" not in sent and "id_rsa" not in sent  # nothing raw on the wire
        # HTTP header names are case-insensitive on the wire — look them up that way.
        hdrs = {k.lower(): v for k, v in captured[0]["headers"].items()}
        assert hdrs.get("x-aco-signature", "").startswith("v1=")
        assert hdrs.get("x-aco-payload-sha256")  # signed digest present
    finally:
        srv.shutdown()
        _clear_relay_env(monkeypatch)


# --------------------------------------------------------------------------- #
# 3. 401 stale_timestamp → resync ONCE from server_time, no loop.
# --------------------------------------------------------------------------- #
def test_relay_stale_timestamp_resyncs_once_no_loop(_isolated_state, monkeypatch):
    import time as _t
    future = int(_t.time()) + 10_000     # server clock far ahead → local ts is "stale"
    srv = HTTPServer(("127.0.0.1", 0),
                     _make_verifying_handler("k1", b"test-collector-secret",
                                             force="stale", server_time=future))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        _relay_env(monkeypatch, f"http://127.0.0.1:{port}")
        daemon._RELAY_CLOCK_OFFSET = 0.0
        daemon.config.ensure_dirs()
        qf = daemon.config.QUEUE_DIR / "0000000000000002-1-bbbb.json"
        qf.write_text(json.dumps(claude_hook("Stop")))
        c = daemon.drain_once(daemon.config.load_pepper(), daemon.config.load_aliases())
        # record left for retry (not dropped, not dead-lettered)
        assert c["requeued"] == 1 and qf.exists()
        assert not list(daemon.config.DEAD_LETTER_DIR.glob("*.reason.json"))
        # the offset was learned ONCE (no tight loop inside drain_once)
        assert daemon._RELAY_CLOCK_OFFSET > 5_000    # ~ +10000s, applied next loop
    finally:
        srv.shutdown()
        daemon._RELAY_CLOCK_OFFSET = 0.0
        _clear_relay_env(monkeypatch)


def test_relay_resync_then_next_loop_succeeds(_isolated_state, monkeypatch):
    """After a stale resync sets the offset, the corrected timestamp is in-window so a
    server whose clock is ahead now accepts the SAME record on the next drain."""
    import time as _t
    future = int(_t.time()) + 10_000

    class _Handler(_make_verifying_handler("k1", b"test-collector-secret")):
        pass

    # A stub that fakes a server clock 10000s ahead for the real verifier.
    keys = KeyStore({"k1": b"test-collector-secret"})

    class _AheadStub(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n)
            ts = int(self.headers.get("X-ACO-Timestamp", "0"))
            # signature must still verify; emulate server "now" = future.
            sig = self.headers.get("X-ACO-Signature", "")
            sig = sig[3:] if sig.lower().startswith("v1=") else sig
            dig = (self.headers.get("X-ACO-Payload-SHA256", "")).lower()
            expect = sign(b"test-collector-secret",
                          canonical_string("POST", self.path, "k1",
                                            self.headers.get("X-ACO-Timestamp", ""),
                                            self.headers.get("X-ACO-Nonce", ""), dig))
            ok_sig = (sig == expect and dig == payload_sha256_hex(raw))
            in_window = abs(future - ts) <= 300
            if not ok_sig:
                self._json(401, {"ok": False, "reason": "bad_signature", "server_time": future})
                return
            if not in_window:
                self._json(401, {"ok": False, "reason": "stale_timestamp", "server_time": future})
                return
            body = json.loads(raw.decode("utf-8"))
            results = [{"event_id": e.get("event_id"), "status": "accepted"}
                       for e in body.get("events", [])]
            self._json(200, {"ok": True, "server_time": future, "results": results,
                             "accepted": len(results), "rejected": 0, "duplicates": 0})

        def _json(self, code, obj):
            b = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    srv = HTTPServer(("127.0.0.1", 0), _AheadStub)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        _relay_env(monkeypatch, f"http://127.0.0.1:{port}")
        daemon._RELAY_CLOCK_OFFSET = 0.0
        daemon.config.ensure_dirs()
        qf = daemon.config.QUEUE_DIR / "0000000000000003-1-cccc.json"
        qf.write_text(json.dumps(claude_hook("Stop")))
        c1 = daemon.drain_once(daemon.config.load_pepper(), daemon.config.load_aliases())
        assert c1["requeued"] == 1 and qf.exists()       # first loop: stale → resync
        c2 = daemon.drain_once(daemon.config.load_pepper(), daemon.config.load_aliases())
        assert c2["posted"] == 1 and not qf.exists()      # second loop: corrected ts accepted
    finally:
        srv.shutdown()
        daemon._RELAY_CLOCK_OFFSET = 0.0
        _clear_relay_env(monkeypatch)


# --------------------------------------------------------------------------- #
# 4. per-event reject → dead-letter (reason + hash only, never raw).
# --------------------------------------------------------------------------- #
def test_relay_per_event_reject_dead_letters(_isolated_state, monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0),
                     _make_verifying_handler("k1", b"test-collector-secret", force="event_reject"))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        _relay_env(monkeypatch, f"http://127.0.0.1:{port}")
        daemon.config.ensure_dirs()
        qf = daemon.config.QUEUE_DIR / "0000000000000004-1-dddd.json"
        qf.write_text(json.dumps(claude_hook("PreToolUse", tool_name="Read", tool_input={})))
        c = daemon.drain_once(daemon.config.load_pepper(), daemon.config.load_aliases())
        assert c["rejected"] == 1 and not qf.exists()
        dl = list(daemon.config.DEAD_LETTER_DIR.glob("*.reason.json"))
        assert len(dl) == 1
        meta = json.loads(dl[0].read_text())
        assert "sanitization_failed" in meta["reason"]
        assert meta["payload_hash"] and "/Users/" not in json.dumps(meta)  # hash only, no raw
    finally:
        srv.shutdown()
        _clear_relay_env(monkeypatch)


# --------------------------------------------------------------------------- #
# 5. request-level reject (bad signature / revoked) → dead-letter (can't pass).
# --------------------------------------------------------------------------- #
def test_relay_bad_signature_dead_letters(_isolated_state, monkeypatch):
    # The stub knows secret "right" but the collector signs with "wrong" → 401.
    srv = HTTPServer(("127.0.0.1", 0), _make_verifying_handler("k1", b"right-secret"))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        _relay_env(monkeypatch, f"http://127.0.0.1:{port}", secret="wrong-secret")
        daemon.config.ensure_dirs()
        qf = daemon.config.QUEUE_DIR / "0000000000000005-1-eeee.json"
        qf.write_text(json.dumps(claude_hook("Stop")))
        c = daemon.drain_once(daemon.config.load_pepper(), daemon.config.load_aliases())
        assert c["rejected"] == 1 and not qf.exists()
        dl = list(daemon.config.DEAD_LETTER_DIR.glob("*.reason.json"))
        assert len(dl) == 1
        assert "relay_bad_signature" in json.loads(dl[0].read_text())["reason"]
    finally:
        srv.shutdown()
        _clear_relay_env(monkeypatch)


def test_relay_revoked_kid_dead_letters(_isolated_state, monkeypatch):
    # Stub provisioned with a DIFFERENT kid → our kid is unknown/revoked → 403.
    srv = HTTPServer(("127.0.0.1", 0), _make_verifying_handler("other", b"test-collector-secret"))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        _relay_env(monkeypatch, f"http://127.0.0.1:{port}", kid="k1", secret="test-collector-secret")
        daemon.config.ensure_dirs()
        qf = daemon.config.QUEUE_DIR / "0000000000000006-1-ffff.json"
        qf.write_text(json.dumps(claude_hook("Stop")))
        c = daemon.drain_once(daemon.config.load_pepper(), daemon.config.load_aliases())
        assert c["rejected"] == 1 and not qf.exists()
        assert "relay_collector_revoked" in json.loads(
            list(daemon.config.DEAD_LETTER_DIR.glob("*.reason.json"))[0].read_text())["reason"]
    finally:
        srv.shutdown()
        _clear_relay_env(monkeypatch)


# --------------------------------------------------------------------------- #
# 6. transport failure → leave + retry (reaper bounds), never drop.
# --------------------------------------------------------------------------- #
def test_relay_transport_failure_keeps_retrying(_isolated_state, monkeypatch):
    # Point at a closed port → connection refused → PostError → requeue.
    _relay_env(monkeypatch, "http://127.0.0.1:1")  # nothing listening
    try:
        daemon.config.ensure_dirs()
        qf = daemon.config.QUEUE_DIR / "0000000000000007-1-gggg.json"
        qf.write_text(json.dumps(claude_hook("Stop")))
        for _ in range(5):
            c = daemon.drain_once(daemon.config.load_pepper(), daemon.config.load_aliases())
            assert c["requeued"] == 1 and qf.exists()     # never dropped
        assert not list(daemon.config.DEAD_LETTER_DIR.glob("*.reason.json"))
    finally:
        _clear_relay_env(monkeypatch)


def test_relay_unenrolled_requeues_until_secret_present(_isolated_state, monkeypatch):
    """Relay host configured but no secret yet (partial enroll) → leave + retry, not
    a crash and not a dead-letter (a later enroll fixes it)."""
    monkeypatch.setenv("AGENTLAMP_MODE", "relay")
    monkeypatch.setenv("AGENTLAMP_RELAY_HOST", "http://127.0.0.1:1")
    monkeypatch.setenv("AGENTLAMP_RELAY_KID", "k1")
    monkeypatch.delenv("AGENTLAMP_RELAY_SECRET", raising=False)
    import collector.config as cfgmod
    importlib.reload(cfgmod)
    importlib.reload(daemon)
    try:
        daemon.config.ensure_dirs()
        qf = daemon.config.QUEUE_DIR / "0000000000000008-1-hhhh.json"
        qf.write_text(json.dumps(claude_hook("Stop")))
        c = daemon.drain_once(daemon.config.load_pepper(), daemon.config.load_aliases())
        assert c["requeued"] == 1 and qf.exists()
    finally:
        _clear_relay_env(monkeypatch)


# --------------------------------------------------------------------------- #
# 7. local mode is unchanged when relay is OFF.
# --------------------------------------------------------------------------- #
def test_local_mode_unchanged_when_relay_off(_isolated_state, monkeypatch):
    _clear_relay_env(monkeypatch)
    assert daemon.config.RELAY_MODE is False
    daemon.config.ensure_dirs()
    qf = daemon.config.QUEUE_DIR / "0000000000000010-1-iiii.json"
    qf.write_text(json.dumps(claude_hook("PreToolUse", tool_name="Edit", tool_input={})))
    sent = []
    monkeypatch.setattr(netpost, "post_json",
                        lambda url, payload, **k: (sent.append((url, payload)) or (200, {"applied": True})))
    c = daemon.drain_once(daemon.config.load_pepper(), daemon.config.load_aliases())
    assert c["posted"] == 1 and not qf.exists()
    assert sent[0][0].endswith("/admin/event")  # still the local loopback route


# --------------------------------------------------------------------------- #
# 8. secret store (OS keyring) round-trip + delete (revoke).
# --------------------------------------------------------------------------- #
def test_secretstore_roundtrip_and_delete(_isolated_state, monkeypatch):
    # Force the file fallback so the test never touches the developer's real keychain.
    monkeypatch.setattr(secretstore, "_try_keyring", lambda: None)
    monkeypatch.setattr(secretstore.sys, "platform", "test-os")  # not darwin/linux → file
    backend = secretstore.set_secret("k1", "s3cr3t")
    assert backend == "file"
    assert secretstore.get_secret("k1") == "s3cr3t"
    assert secretstore.delete_secret("k1") is True
    assert secretstore.get_secret("k1") is None         # revoke removed it


# --------------------------------------------------------------------------- #
# 9. enroll — installs the whole stack, idempotent; revoke removes it.
# --------------------------------------------------------------------------- #
def test_enroll_installs_whole_stack_idempotent(_isolated_state, monkeypatch, tmp_path, capsys):
    from collector import cli
    # Reload cli so it binds to the fixture-reloaded config (tmp paths).
    importlib.reload(cli)
    monkeypatch.setattr(secretstore, "_try_keyring", lambda: None)
    monkeypatch.setattr(secretstore.sys, "platform", "test-os")
    claude_settings = tmp_path / "claude" / "settings.json"

    # A live stub relay /admin endpoint — enroll must REGISTER the kid+secret here (I5).
    captured, registry = [], {}
    srv = HTTPServer(("127.0.0.1", 0), _make_admin_stub("admin-tok-xyz",
                                                        captured=captured, enrolled=registry))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        args = [
            "enroll",
            "--relay-host", f"http://127.0.0.1:{port}",
            "--collector-id", "laptop-2",
            "--kid", "k7", "--secret", "enroll-secret",
            "--admin-token", "admin-tok-xyz",
            "--write-claude", str(claude_settings),
        ]
        rc = cli.main(args)
        assert rc == 0
        # 1 hooks installed
        assert claude_settings.exists()
        hooks = json.loads(claude_settings.read_text())["hooks"]
        assert "PreToolUse" in hooks and "Stop" in hooks
        # 2 pepper available (env override in tests; a real machine persists a 0600 file)
        assert cli.config.load_pepper()  # non-empty key ready
        # 3 alias map created
        assert pathlib.Path(cli.config.ALIAS_FILE).exists()
        # 4 secret stored
        assert secretstore.get_secret("k7") == "enroll-secret"
        # 5 relay env written, enabling relay push
        env_path = pathlib.Path(cli.config.CONFIG_DIR) / cli.ENV_FILENAME
        assert env_path.exists()
        env_text = env_path.read_text()
        assert "AGENTLAMP_MODE=relay" in env_text
        assert "127.0.0.1" in env_text
        assert "AGENTLAMP_LOCAL_LABELS=0" in env_text     # relay forces HMAC labels (I3 privacy)
        # 6 REGISTERED with the cloud — the kid+secret landed in the relay's registry,
        #   POSTed to the enroll route with the admin bearer (I5: self-enroll, no redeploy).
        assert registry.get("k7") == "enroll-secret"
        assert captured[0]["action"] == "enroll" and captured[0]["kid"] == "k7"
        assert captured[0]["body"] == {"secret": "enroll-secret"}
        assert captured[0]["headers"].get("Authorization") == "Bearer admin-tok-xyz"

        # Idempotent: re-running adds NO duplicate hook entry, keeps the secret, and
        # the server-side enroll is a safe re-put (same kid+secret again).
        before = len(hooks["PreToolUse"])
        rc2 = cli.main(args)
        assert rc2 == 0
        hooks2 = json.loads(claude_settings.read_text())["hooks"]
        assert len(hooks2["PreToolUse"]) == before          # no double-install
        assert secretstore.get_secret("k7") == "enroll-secret"
        assert registry.get("k7") == "enroll-secret"        # still registered (idempotent)
        assert len(captured) == 2 and captured[1]["action"] == "enroll"
    finally:
        srv.shutdown()


def test_enroll_registers_kid_secret_with_admin_token_from_env(_isolated_state, monkeypatch):
    """The admin token may come from AGENTLAMP_ADMIN_TOKEN (not just --admin-token),
    and a loaded secret (no --secret on a re-enroll) is still the value REGISTERED."""
    from collector import cli
    importlib.reload(cli)
    monkeypatch.setattr(secretstore, "_try_keyring", lambda: None)
    monkeypatch.setattr(secretstore.sys, "platform", "test-os")
    # Pre-store the secret so this enroll loads it (no --secret) and still registers it.
    secretstore.set_secret("k8", "loaded-secret")

    captured, registry = [], {}
    srv = HTTPServer(("127.0.0.1", 0), _make_admin_stub("env-admin-token",
                                                        captured=captured, enrolled=registry))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("AGENTLAMP_ADMIN_TOKEN", "env-admin-token")
        rc = cli.main(["enroll", "--relay-host", f"http://127.0.0.1:{port}",
                       "--collector-id", "laptop-3", "--kid", "k8"])
        assert rc == 0
        assert registry.get("k8") == "loaded-secret"        # loaded secret registered
        assert captured[0]["body"] == {"secret": "loaded-secret"}
        assert captured[0]["headers"].get("Authorization") == "Bearer env-admin-token"
    finally:
        srv.shutdown()


def test_enroll_missing_admin_token_errors_clearly(_isolated_state, monkeypatch, capsys):
    """No admin token (neither --admin-token nor env) → enroll FAILS with a clear,
    actionable error and does NOT silently leave the computer un-registered (I5)."""
    from collector import cli
    importlib.reload(cli)
    monkeypatch.setattr(secretstore, "_try_keyring", lambda: None)
    monkeypatch.setattr(secretstore.sys, "platform", "test-os")
    monkeypatch.delenv("AGENTLAMP_ADMIN_TOKEN", raising=False)

    rc = cli.main(["enroll", "--relay-host", "https://relay.example.com",
                   "--collector-id", "laptop-2", "--kid", "k7", "--secret", "enroll-secret"])
    assert rc != 0
    err = capsys.readouterr().err.lower()
    assert "admin token" in err
    assert "--admin-token" in err or "agentlamp_admin_token" in err


def test_enroll_no_cloud_register_skips_admin_post(_isolated_state, monkeypatch, capsys):
    """--no-cloud-register is the explicit local-only escape hatch: enroll succeeds
    without an admin token and never POSTs to the relay (but warns it is unregistered)."""
    from collector import cli
    importlib.reload(cli)
    monkeypatch.setattr(secretstore, "_try_keyring", lambda: None)
    monkeypatch.setattr(secretstore.sys, "platform", "test-os")
    monkeypatch.delenv("AGENTLAMP_ADMIN_TOKEN", raising=False)
    # If it tried to POST, this would explode — proving it does NOT call the cloud.
    monkeypatch.setattr(cli.netpost, "post_json",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not POST")))

    rc = cli.main(["enroll", "--relay-host", "https://relay.example.com",
                   "--collector-id", "laptop-2", "--kid", "k7", "--secret", "enroll-secret",
                   "--no-cloud-register"])
    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "skipped" in out and "no-cloud-register" in out


def test_enroll_bad_admin_token_propagates_clear_error(_isolated_state, monkeypatch, capsys):
    """A WRONG admin token → the relay returns 401; enroll surfaces a clear non-zero
    error (the kid is NOT registered) rather than pretending success."""
    from collector import cli
    importlib.reload(cli)
    monkeypatch.setattr(secretstore, "_try_keyring", lambda: None)
    monkeypatch.setattr(secretstore.sys, "platform", "test-os")

    captured, registry = [], {}
    srv = HTTPServer(("127.0.0.1", 0), _make_admin_stub("the-real-token",
                                                        captured=captured, enrolled=registry))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        rc = cli.main(["enroll", "--relay-host", f"http://127.0.0.1:{port}",
                       "--collector-id", "laptop-2", "--kid", "k7", "--secret", "enroll-secret",
                       "--admin-token", "WRONG-token"])
        assert rc != 0
        assert "k7" not in registry                          # nothing registered
        err = capsys.readouterr().err.lower()
        assert "401" in err and "admin token" in err
    finally:
        srv.shutdown()


def test_enroll_requires_host(_isolated_state, monkeypatch):
    """--relay-host is still required (relay push cannot be enabled without it). A
    missing --kid is NO LONGER an error — it MINTS one (see the minting tests)."""
    from collector import cli
    importlib.reload(cli)
    monkeypatch.setattr(secretstore, "_try_keyring", lambda: None)
    monkeypatch.setattr(secretstore.sys, "platform", "test-os")
    # missing --relay-host → non-zero, relay push not enabled.
    rc = cli.main(["enroll", "--kid", "k7", "--secret", "s", "--no-cloud-register"])
    assert rc != 0


# --------------------------------------------------------------------------- #
# 10. MED #1 / P0 — enroll MINTS a fresh kid + high-entropy secret when none given.
# --------------------------------------------------------------------------- #
def test_enroll_mints_fresh_kid_and_secret_when_none_supplied(_isolated_state, monkeypatch):
    """THE headline (P0): ``agentlamp enroll`` with NO --kid/--secret mints a fresh
    kid + a high-entropy (256-bit) secret, stores the secret in the keyring, and
    REGISTERS it with the cloud — that is what makes the one-liner real."""
    from collector import cli
    importlib.reload(cli)
    monkeypatch.setattr(secretstore, "_try_keyring", lambda: None)
    monkeypatch.setattr(secretstore.sys, "platform", "test-os")

    captured, registry = [], {}
    srv = HTTPServer(("127.0.0.1", 0), _make_admin_stub("admin-tok", captured=captured, enrolled=registry))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        rc = cli.main(["enroll", "--relay-host", f"http://127.0.0.1:{port}",
                       "--collector-id", "laptop-mint", "--admin-token", "admin-tok"])
        assert rc == 0
        # Exactly one kid was minted + registered server-side.
        assert len(registry) == 1, registry
        minted_kid, minted_secret = next(iter(registry.items()))
        # kid matches the relay KID charset and is the minted ``k<hex8>`` shape.
        from agentlamp_server.ingest import KID_RE
        assert KID_RE.match(minted_kid) and minted_kid.startswith("k")
        # secret is high-entropy: 256-bit → 64 hex chars, NOT a guessable default.
        assert len(minted_secret) == 64 and all(c in "0123456789abcdef" for c in minted_secret)
        # stored in the keyring under the minted kid (one-line setup is complete).
        assert secretstore.get_secret(minted_kid) == minted_secret
        # the secret POSTed to the cloud matches what was stored (no divergence).
        assert captured[0]["body"] == {"secret": minted_secret}
    finally:
        srv.shutdown()


def test_enroll_minted_kid_differs_each_fresh_machine(_isolated_state, monkeypatch, tmp_path):
    """Two fresh enrolls (different config dirs) mint DIFFERENT kids + secrets — no
    fixed/shared credential across machines (I3: nothing hardcoded)."""
    from collector import cli

    def _one_enroll(cfgdir):
        monkeypatch.setenv("AGENTLAMP_CONFIG_DIR", str(cfgdir))
        import collector.config as cfgmod
        importlib.reload(cfgmod)
        importlib.reload(cli)
        monkeypatch.setattr(secretstore, "_try_keyring", lambda: None)
        monkeypatch.setattr(secretstore.sys, "platform", "test-os")
        captured, registry = [], {}
        srv = HTTPServer(("127.0.0.1", 0), _make_admin_stub("t", captured=captured, enrolled=registry))
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            rc = cli.main(["enroll", "--relay-host", f"http://127.0.0.1:{port}",
                           "--collector-id", "lap", "--admin-token", "t"])
            assert rc == 0
            return next(iter(registry.items()))
        finally:
            srv.shutdown()

    kid1, sec1 = _one_enroll(tmp_path / "m1")
    kid2, sec2 = _one_enroll(tmp_path / "m2")
    assert kid1 != kid2 and sec1 != sec2


# --------------------------------------------------------------------------- #
# 11. MED #3 — enroll/revoke REQUIRE https:// for the relay host (admin + secret).
# --------------------------------------------------------------------------- #
def test_enroll_rejects_plaintext_http_public_host(_isolated_state, monkeypatch, capsys):
    """A public http:// relay host is REJECTED before any secret/cloud step — it
    would leak the admin bearer + collector secret on the wire (MED #3)."""
    from collector import cli
    importlib.reload(cli)
    monkeypatch.setattr(secretstore, "_try_keyring", lambda: None)
    monkeypatch.setattr(secretstore.sys, "platform", "test-os")
    # If it reached the cloud step it would POST — prove it never does.
    monkeypatch.setattr(cli.netpost, "post_json",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not POST")))
    rc = cli.main(["enroll", "--relay-host", "http://relay.example.com",
                   "--collector-id", "lap", "--admin-token", "t"])
    assert rc != 0
    err = capsys.readouterr().err.lower()
    assert "https" in err
    # And no secret was minted/stored for a rejected host (we bailed at step 0).


def test_enroll_allows_loopback_http(_isolated_state, monkeypatch):
    """A loopback http:// host (local stub / dev relay) is allowed — it never reaches
    a network, so the secret/bearer never go out in plaintext over a real link."""
    from collector import cli
    importlib.reload(cli)
    monkeypatch.setattr(secretstore, "_try_keyring", lambda: None)
    monkeypatch.setattr(secretstore.sys, "platform", "test-os")
    captured, registry = [], {}
    srv = HTTPServer(("127.0.0.1", 0), _make_admin_stub("t", captured=captured, enrolled=registry))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        rc = cli.main(["enroll", "--relay-host", f"http://127.0.0.1:{port}",
                       "--collector-id", "lap", "--kid", "k1", "--secret", "s",
                       "--admin-token", "t"])
        assert rc == 0 and registry.get("k1") == "s"
    finally:
        srv.shutdown()


def test_enroll_insecure_localhost_rejects_public_host(_isolated_state, monkeypatch, capsys):
    """--insecure-localhost only widens to LOOPBACK http://; a public http:// host is
    STILL rejected with a clear message (it is not loopback)."""
    from collector import cli
    importlib.reload(cli)
    monkeypatch.setattr(secretstore, "_try_keyring", lambda: None)
    monkeypatch.setattr(secretstore.sys, "platform", "test-os")
    rc = cli.main(["enroll", "--relay-host", "http://relay.example.com",
                   "--collector-id", "lap", "--admin-token", "t", "--insecure-localhost"])
    assert rc != 0
    assert "loopback" in capsys.readouterr().err.lower()


def test_revoke_rejects_plaintext_http_public_host(_isolated_state, monkeypatch, capsys):
    """revoke also refuses a public http:// relay host — the revoke route carries the
    admin bearer too (MED #3). The local secret is still forgotten (best-effort)."""
    from collector import cli
    importlib.reload(cli)
    monkeypatch.setattr(secretstore, "_try_keyring", lambda: None)
    monkeypatch.setattr(secretstore.sys, "platform", "test-os")
    secretstore.set_secret("k9", "tok")
    monkeypatch.setattr(cli.netpost, "post_json",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not POST")))
    rc = cli.main(["revoke", "--kid", "k9", "--relay-host", "http://relay.example.com",
                   "--admin-token", "t"])
    # revoke returns 0 (local cleanup done) but the cloud step is an ERROR (https).
    out = capsys.readouterr().out.lower()
    assert "https" in out and "error" in out
    assert secretstore.get_secret("k9") is None  # local secret still forgotten


# --------------------------------------------------------------------------- #
# 12. MED #4 — secret / admin-token NOT on argv (stdin / env / prompt).
# --------------------------------------------------------------------------- #
def test_enroll_secret_and_token_from_stdin_not_argv(_isolated_state, monkeypatch):
    """--secret-stdin + --admin-token-stdin read both off stdin (one line each), so
    NEITHER appears on argv. The values still land + register correctly."""
    from collector import cli
    importlib.reload(cli)
    monkeypatch.setattr(secretstore, "_try_keyring", lambda: None)
    monkeypatch.setattr(secretstore.sys, "platform", "test-os")

    captured, registry = [], {}
    srv = HTTPServer(("127.0.0.1", 0), _make_admin_stub("stdin-admin-tok",
                                                        captured=captured, enrolled=registry))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    import io
    # stdin: first line = secret (read in step 4), second line = admin token (step 6).
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("stdin-secret\nstdin-admin-tok\n"))
    try:
        rc = cli.main(["enroll", "--relay-host", f"http://127.0.0.1:{port}",
                       "--collector-id", "lap", "--kid", "k7",
                       "--secret-stdin", "--admin-token-stdin"])
        assert rc == 0
        assert secretstore.get_secret("k7") == "stdin-secret"
        assert registry.get("k7") == "stdin-secret"
        assert captured[0]["headers"].get("Authorization") == "Bearer stdin-admin-tok"
    finally:
        srv.shutdown()


def test_macos_set_keeps_secret_off_argv(monkeypatch):
    """MED #4: _macos_set must NOT put the secret on argv — it passes a trailing -w
    (no value) and feeds the secret via stdin. Assert the recorded argv has no secret."""
    calls = {}

    def _fake_run(cmd, **kw):
        calls["cmd"] = cmd
        calls["input"] = kw.get("input")
        class _R:  # minimal CompletedProcess
            returncode = 0
        return _R()

    monkeypatch.setattr(secretstore.subprocess, "run", _fake_run)
    ok = secretstore._macos_set("svc", "acct", "TOP-SECRET-VALUE")
    assert ok is True
    assert "TOP-SECRET-VALUE" not in calls["cmd"]          # secret is NOT in argv
    assert calls["cmd"][-1] == "-w"                          # trailing -w → stdin prompt
    assert calls["input"] == b"TOP-SECRET-VALUE\n"           # secret fed via stdin


# --------------------------------------------------------------------------- #
# 13. MED #5 — Windows fail-closed + source-free relay.json config.
# --------------------------------------------------------------------------- #
def test_secretstore_windows_fails_closed_no_plaintext(_isolated_state, monkeypatch):
    """On Windows with NO keyring backend, set_secret FAILS CLOSED (raises) rather
    than silently writing the collector secret to an unprotected plaintext file."""
    monkeypatch.setattr(secretstore, "_try_keyring", lambda: None)
    monkeypatch.setattr(secretstore, "_is_windows", lambda: True)
    monkeypatch.setattr(secretstore.sys, "platform", "win32")  # not darwin/linux
    with pytest.raises(secretstore.SecretStoreError):
        secretstore.set_secret("k1", "s3cr3t")
    # And nothing was written to the fallback file.
    assert secretstore.get_secret("k1") is None


def test_secretstore_windows_explicit_optin_writes_file(_isolated_state, monkeypatch):
    """The explicit allow_insecure_file=True opt-in still works on a locked-down box."""
    monkeypatch.setattr(secretstore, "_try_keyring", lambda: None)
    monkeypatch.setattr(secretstore, "_is_windows", lambda: True)
    monkeypatch.setattr(secretstore.sys, "platform", "win32")
    backend = secretstore.set_secret("k1", "s3cr3t", allow_insecure_file=True)
    assert backend == "file"
    assert secretstore.get_secret("k1") == "s3cr3t"


def test_enroll_writes_source_free_relay_json_config(_isolated_state, monkeypatch):
    """enroll writes a portable relay.json that config.py reads DIRECTLY (no POSIX
    sourcing) — so a Windows / cron / bare daemon launch picks up the relay config."""
    from collector import cli
    importlib.reload(cli)
    monkeypatch.setattr(secretstore, "_try_keyring", lambda: None)
    monkeypatch.setattr(secretstore.sys, "platform", "test-os")
    captured, registry = [], {}
    srv = HTTPServer(("127.0.0.1", 0), _make_admin_stub("t", captured=captured, enrolled=registry))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        rc = cli.main(["enroll", "--relay-host", f"http://127.0.0.1:{port}",
                       "--collector-id", "laptop-json", "--kid", "k7", "--secret", "s",
                       "--admin-token", "t"])
        assert rc == 0
        json_path = pathlib.Path(cli.config.CONFIG_DIR) / cli.RELAY_JSON_FILENAME
        assert json_path.exists()
        data = json.loads(json_path.read_text())
        assert data["mode"] == "relay" and data["kid"] == "k7"
        assert data["collector_id"] == "laptop-json" and data["local_labels"] is False

        # config.py reads it DIRECTLY — clear the env so ONLY the file drives config.
        for k in ("AGENTLAMP_RELAY_HOST", "AGENTLAMP_RELAY_KID", "AGENTLAMP_COLLECTOR_ID",
                  "AGENTLAMP_MODE", "AGENTLAMP_LOCAL_LABELS"):
            monkeypatch.delenv(k, raising=False)
        import collector.config as cfgmod
        importlib.reload(cfgmod)
        assert cfgmod.RELAY_MODE is True                 # file alone enabled relay mode
        assert cfgmod.RELAY_KID == "k7"
        assert cfgmod.COLLECTOR_ID == "laptop-json"
        assert cfgmod.LOCAL_LABELS is False              # relay → HMAC labels, source-free
    finally:
        srv.shutdown()


def test_config_env_wins_over_relay_json_file(_isolated_state, monkeypatch):
    """ENV always wins over relay.json (tests/CI + explicit override stay
    authoritative); the file only fills the gaps."""
    import json as _json
    cfgdir = pathlib.Path(os.environ["AGENTLAMP_CONFIG_DIR"])
    cfgdir.mkdir(parents=True, exist_ok=True)
    (cfgdir / "relay.json").write_text(_json.dumps(
        {"mode": "relay", "relay_host": "https://file-host", "kid": "file-kid",
         "collector_id": "file-cid", "local_labels": False}))
    monkeypatch.setenv("AGENTLAMP_RELAY_KID", "env-kid")
    monkeypatch.setenv("AGENTLAMP_RELAY_HOST", "https://env-host")
    import collector.config as cfgmod
    importlib.reload(cfgmod)
    assert cfgmod.RELAY_KID == "env-kid"                 # env wins
    assert cfgmod.RELAY_HOST == "https://env-host"
    assert cfgmod.COLLECTOR_ID == "file-cid"             # gap filled from file


# --------------------------------------------------------------------------- #
# 14. P1 — RELAY-mode signed collector.heartbeat keeps the cloud liveness fresh.
# --------------------------------------------------------------------------- #
def test_relay_heartbeat_is_signed_and_accepted(_isolated_state, monkeypatch):
    """In RELAY mode the daemon's heartbeat is a SIGNED collector.heartbeat pushed to
    the relay (NOT the loopback /admin/heartbeat), verified end-to-end by the stub's
    real IngestVerifier — so an idle-but-present owner is never marked offline (P1)."""
    captured = []
    srv = HTTPServer(("127.0.0.1", 0),
                     _make_verifying_handler("k1", b"test-collector-secret", captured=captured))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        _relay_env(monkeypatch, f"http://127.0.0.1:{port}")
        daemon.config.ensure_dirs()
        ok = daemon._heartbeat()
        assert ok is True                                # signature verified + accepted
        assert captured, "relay must have received the heartbeat push"
        body = json.loads(captured[0]["raw"].decode("utf-8"))
        ev = body["events"][0]
        assert ev["event_type"] == "collector.heartbeat"
        assert ev["payload"] == {}                        # no sanitizable content
        hdrs = {k.lower(): v for k, v in captured[0]["headers"].items()}
        assert hdrs.get("x-aco-signature", "").startswith("v1=")   # it is SIGNED
        assert "/Users/" not in captured[0]["raw"].decode("utf-8")  # nothing raw
    finally:
        srv.shutdown()
        _clear_relay_env(monkeypatch)


def test_relay_heartbeat_misconfig_returns_false_no_crash(_isolated_state, monkeypatch):
    """No secret yet (partial enroll) → relay heartbeat is a no-op returning False
    (the next loop retries), never a crash."""
    monkeypatch.setenv("AGENTLAMP_MODE", "relay")
    monkeypatch.setenv("AGENTLAMP_RELAY_HOST", "http://127.0.0.1:1")
    monkeypatch.setenv("AGENTLAMP_RELAY_KID", "k1")
    monkeypatch.delenv("AGENTLAMP_RELAY_SECRET", raising=False)
    import collector.config as cfgmod
    importlib.reload(cfgmod)
    importlib.reload(daemon)
    try:
        assert daemon._heartbeat() is False
    finally:
        _clear_relay_env(monkeypatch)


def test_local_mode_heartbeat_still_loopback(_isolated_state, monkeypatch):
    """Local mode heartbeat is UNCHANGED — it hits the loopback /admin/heartbeat,
    never a signed relay push."""
    _clear_relay_env(monkeypatch)
    assert daemon.config.RELAY_MODE is False
    seen = []
    monkeypatch.setattr(daemon.netpost, "post_empty",
                        lambda url, **k: (seen.append(url) or (200, {})))
    assert daemon._heartbeat() is True
    assert seen and seen[0].endswith("/admin/heartbeat")


def test_revoke_hits_cloud_and_forgets_secret(_isolated_state, monkeypatch, tmp_path, capsys):
    """revoke must (1) hit the public /admin/collectors/{kid}/revoke route so a leaked
    secret is rejected everywhere at once (I4), AND (2) forget the local secret +
    disable relay push here."""
    from collector import cli
    importlib.reload(cli)
    monkeypatch.setattr(secretstore, "_try_keyring", lambda: None)
    monkeypatch.setattr(secretstore.sys, "platform", "test-os")

    captured, registry = [], {}
    srv = HTTPServer(("127.0.0.1", 0), _make_admin_stub("admin-tok",
                                                        captured=captured, enrolled=registry))
    port = srv.server_address[1]
    host = f"http://127.0.0.1:{port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # Enroll first so the kid is registered server-side + stored locally.
        cli.main(["enroll", "--relay-host", host, "--collector-id", "laptop-2",
                  "--kid", "k9", "--secret", "tok", "--admin-token", "admin-tok"])
        assert registry.get("k9") == "tok"
        assert secretstore.get_secret("k9") == "tok"
        env_path = pathlib.Path(cli.config.CONFIG_DIR) / cli.ENV_FILENAME
        assert env_path.exists()
        captured.clear()

        rc = cli.main(["revoke", "--kid", "k9", "--relay-host", host, "--admin-token", "admin-tok"])
        assert rc == 0
        # 1 server-side: hit the revoke route with the bearer; the kid is gone server-side.
        assert captured[0]["action"] == "revoke" and captured[0]["kid"] == "k9"
        assert captured[0]["headers"].get("Authorization") == "Bearer admin-tok"
        assert "k9" not in registry
        # 2 local: secret forgotten + relay push disabled.
        assert secretstore.get_secret("k9") is None
        assert not env_path.exists()
        out = capsys.readouterr().out.lower()
        assert "revoked kid=k9 at the relay" in out
    finally:
        srv.shutdown()


def test_revoke_forgets_secret_and_disables_relay_local_only(_isolated_state, monkeypatch, tmp_path, capsys):
    """With no relay host available, revoke still forgets the local secret + disables
    relay push, and documents the server-side step (cloud revoke skipped, not faked)."""
    from collector import cli
    importlib.reload(cli)
    monkeypatch.setattr(secretstore, "_try_keyring", lambda: None)
    monkeypatch.setattr(secretstore.sys, "platform", "test-os")
    monkeypatch.delenv("AGENTLAMP_RELAY_HOST", raising=False)
    # Seed local state the way enroll would, but offline (no cloud).
    secretstore.set_secret("k9", "tok")
    cli._write_env("https://r.example.com", "k9", "laptop-2")
    env_path = pathlib.Path(cli.config.CONFIG_DIR) / cli.ENV_FILENAME
    assert env_path.exists()
    # No relay host on argv/env → cloud revoke is skipped, never POSTs.
    monkeypatch.setattr(cli.netpost, "post_json",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not POST")))

    rc = cli.main(["revoke", "--kid", "k9", "--no-cloud-revoke"])
    assert rc == 0
    assert secretstore.get_secret("k9") is None         # secret forgotten
    assert not env_path.exists()                         # relay push disabled locally
    out = capsys.readouterr().out.lower()
    assert "delete this kid from the relay" in out  # documents the server-side revoke


def test_status_and_doctor_run(_isolated_state, monkeypatch, capsys):
    from collector import cli
    _clear_relay_env(monkeypatch)
    importlib.reload(cli)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "mode:" in out and "local" in out
    # doctor in local mode: pepper may not yet exist → still returns an int (0 or 1)
    rc = cli.main(["doctor"])
    assert rc in (0, 1)
