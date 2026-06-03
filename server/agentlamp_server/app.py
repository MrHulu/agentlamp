"""FastAPI app — AgentLamp LOCAL LAN frame server (local mode, no cloud).

In local mode the collector itself serves the frame over the LAN (the device
polls it directly). This app owns aggregation + display priority + frame
generation directly (``docs/architecture/architecture.md`` → local mode), and
issues/verifies device bearer tokens + serves the pairing endpoint
(``docs/security/pairing_and_auth.md`` → Local Mode).

Contract source of truth:
  * ``docs/api/device_frame_api.md``      — frame schema v1, errors, headers
  * ``docs/cloud/cloud_contract.md``      — priority + frame-generation rules
  * ``docs/security/sanitization_policy.md`` — default-deny sanitizer
  * ``docs/providers/provider_normalization.md`` — provider/event normalization

Run (local mode):
    .venv/bin/python -m agentlamp_server
    .venv/bin/uvicorn agentlamp_server.app:app --host 0.0.0.0 --port 8787
"""
from __future__ import annotations

import os
import secrets
import time

from fastapi import Body, FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import ingest as I
from . import sanitize as S
from .preview import render_preview
from .state import FRAME_SCHEMA_VERSION, FrameState

# --------------------------------------------------------------------------- #
# Configuration (env-overridable; never hard-commit a production secret).
# --------------------------------------------------------------------------- #
DEV_DEVICE_ID = os.environ.get("AGENTLAMP_DEV_DEVICE_ID", "orb-01")
DEV_DEVICE_TOKEN = os.environ.get("AGENTLAMP_DEV_DEVICE_TOKEN", "dev-local-token")
ALIAS_FILE = os.environ.get("AGENTLAMP_ALIAS_FILE", "~/.config/agentlamp/aliases.toml")

# --------------------------------------------------------------------------- #
# /admin/* access control (2026-06-03 hardening).
# The local frame server binds 0.0.0.0 (the device polls it across the LAN), so EVERY box on
# the WiFi could otherwise hit the unauthenticated /admin/* routes (inject events, set quota,
# mint pairing codes). The device only ever uses /frame + /pair (unaffected). Gate /admin/*:
#   * allow if the request client is loopback (127.0.0.1 / ::1 / ::ffff:127.0.0.1), OR
#   * allow if a configured AGENTLAMP_LOCAL_ADMIN_TOKEN is presented as a Bearer (lets an
#     operator drive /admin/* from another box when they explicitly provision a shared token),
#   * else 403.
# TestClient's ASGI client host is "testclient" (no real socket); it is treated as loopback so
# the 274-test suite (and any in-process driver) keeps working without provisioning a token.
# This is intentionally a coarse network gate, NOT per-user auth: the strong /admin auth
# (Cloudflare Access / MFA, build-spec §Auth model) lives at the relay edge, not on this
# LAN-only local server. Documented in docs/security/sanitization_policy.md.
# --------------------------------------------------------------------------- #
LOCAL_ADMIN_TOKEN = os.environ.get("AGENTLAMP_LOCAL_ADMIN_TOKEN", "").strip()
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"})
# TestClient (and other in-process ASGI drivers) report this synthetic host — treat as local
# so tests + same-process tooling are not blocked. A real LAN peer reports its routable IP.
_TEST_CLIENT_HOSTS = frozenset({"testclient", ""})


def _build_state() -> FrameState:
    try:
        aliases = S.load_alias_map(ALIAS_FILE)
    except Exception:  # missing/malformed alias file must never crash the server
        aliases = S.AliasMap()
    return FrameState(
        aliases=aliases,
        device_token=DEV_DEVICE_TOKEN,
        device_id=DEV_DEVICE_ID,
    )


app = FastAPI(title="AgentLamp Local Frame Server", version="0.1.0")
app.state.frame = _build_state()
# Relay-mode signed ingest verifier (empty key store in local mode → ingest rejects all,
# which is correct: local mode never uses the ingest hop). Keys come from the environment;
# a real relay loads them from a secrets store, never a committed default.
app.state.ingest = I.IngestVerifier(I.load_keys_from_env(os.environ))


def _client_is_local(request: Request) -> bool:
    """True iff the request originates from loopback (or an in-process ASGI test client)."""
    client = request.client
    host = (client.host if client else "") or ""
    return host in _LOOPBACK_HOSTS or host in _TEST_CLIENT_HOSTS


def _admin_authorized(request: Request) -> bool:
    """An /admin/* request is allowed iff it is loopback-local OR carries the configured
    AGENTLAMP_LOCAL_ADMIN_TOKEN bearer (only honoured when the env var is non-empty)."""
    if _client_is_local(request):
        return True
    if LOCAL_ADMIN_TOKEN:
        token = _bearer(request.headers.get("authorization"))
        # Constant-time compare so a remote caller can't time-probe the token.
        if secrets.compare_digest(token, LOCAL_ADMIN_TOKEN):
            return True
    return False


@app.middleware("http")
async def _gate_admin_routes(request: Request, call_next):
    """Reject non-local, non-token requests to /admin/* (the LAN-exposed local server)."""
    if request.url.path.startswith("/admin/") and not _admin_authorized(request):
        return JSONResponse(status_code=403, content={"error": "admin_forbidden", "retry": False})
    return await call_next(request)


def _state() -> FrameState:
    return app.state.frame


def _bearer(authorization: str | None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


# --------------------------------------------------------------------------- #
# Liveness.
# --------------------------------------------------------------------------- #
@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "service": "agentlamp-frame-server", "v": FRAME_SCHEMA_VERSION}


# --------------------------------------------------------------------------- #
# Device frame API (read-only) — device_frame_api.md.
# --------------------------------------------------------------------------- #
@app.get("/api/v1/device/{device_id}/frame")
def get_frame(
    device_id: str,
    authorization: str | None = Header(default=None),
    x_frame_schema_version: str | None = Header(default=None),
) -> JSONResponse:
    """Return a v1 frame. Bearer auth (token never in URL); schema negotiation
    ``min(server, requested)``; error envelope ``{"error","retry"}``.

    The schema-version header is accepted as a **string** and coerced
    defensively: a malformed value (``"abc"``) must NOT surface FastAPI's default
    ``{"detail": ...}`` 422 — every error on this endpoint is the contract
    ``{"error","retry"}`` envelope (device_frame_api.md → Error Responses)."""
    st = _state()
    token = _bearer(authorization)

    # Auth precedence: bad token first, then unknown device (contract table).
    verdict = st.verify_device_token(device_id, token)
    if verdict == "bad_token":
        return JSONResponse(status_code=401, content={"error": "bad_token", "retry": False})
    if verdict == "unknown_device":
        return JSONResponse(status_code=404, content={"error": "unknown_device", "retry": False})

    # Defensive coercion: a non-integer / absent header falls back to the server
    # version (the device contract says it sends its max-supported int; a garbage
    # value is treated as "negotiate to server default", never a 422/500).
    requested = _coerce_schema_version(x_frame_schema_version, FRAME_SCHEMA_VERSION)
    negotiated = min(FRAME_SCHEMA_VERSION, max(1, requested))
    try:
        frame = st.build_frame(device_id, schema_version=negotiated)
    except Exception:
        return JSONResponse(
            status_code=503, content={"error": "frame_unavailable", "retry": True}
        )

    headers = {"X-Frame-Schema-Version": str(negotiated)}
    return JSONResponse(content=frame, headers=headers)


# --------------------------------------------------------------------------- #
# Pairing (token exchange) — pairing_and_auth.md → Local Mode.
# POST /api/v1/device/{device_id}/pair  { "pairing_code": "..." } -> device_token
#
# Contract (pairing_and_auth.md → Device Pairing 1-3): the token is returned
# **once**, ONLY in exchange for a valid one-time pairing code that the local
# admin/CLI issued, and the code is **burned** on use. This holds for EVERY
# device, including the dev device — there is no "absent/bogus code → mint the
# dev token" shortcut (that would make the one-time-code contract vacuous and
# leak a read-only token to any unauthenticated caller on the LAN).
# --------------------------------------------------------------------------- #
@app.post("/api/v1/device/{device_id}/pair")
def pair_device(device_id: str, body: dict = Body(default_factory=dict)) -> JSONResponse:
    if not _valid_device_id(device_id):
        return JSONResponse(status_code=404, content={"error": "unknown_device", "retry": False})
    st = _state()
    code = str(body.get("pairing_code") or body.get("code") or "").strip()

    token = st.redeem_pairing_code(device_id, code) if code else None
    if token is None:
        # Invalid / absent / expired / already-burned code → never mint a token.
        return JSONResponse(
            status_code=400, content={"error": "bad_pairing_code", "retry": False}
        )
    return JSONResponse(content={"device_token": token, "device_id": device_id})


# Local pairing-code issuance (plays the local-CLI role; LAN/localhost only).
@app.post("/admin/device/{device_id}/code")
def issue_code(device_id: str, body: dict = Body(default_factory=dict)) -> JSONResponse:
    if not _valid_device_id(device_id):
        return JSONResponse(status_code=400, content={"error": "bad_device_id"})
    token = str(body.get("device_token") or DEV_DEVICE_TOKEN)
    code = _state().issue_pairing_code(device_id, token)
    return JSONResponse(content={"pairing_code": code, "device_id": device_id, "ttl_s": 600})


# --------------------------------------------------------------------------- #
# Admin event injection (drives state) — recomputes the frame.
# Accepts a shorthand body and wraps it in a provider event envelope, so the
# full default-deny sanitizer still runs on injected events.
# --------------------------------------------------------------------------- #
@app.post("/admin/event")
def admin_event(body: dict = Body(...)) -> JSONResponse:
    st = _state()
    envelope = _to_envelope(body)
    try:
        result = st.apply_event(envelope)
    except S.SanitizationError as exc:
        return JSONResponse(
            status_code=422,
            content={"rejected": True, "reason": exc.reason, "payload_hash": exc.payload_hash},
        )
    frame = st.build_frame(DEV_DEVICE_ID)
    return JSONResponse(content={"applied": True, "status": result["status"], "frame": frame})


# --------------------------------------------------------------------------- #
# Relay-mode signed collector ingest — collector_ingest_api.md.
# Request-level failures (signature / replay / limits) → HTTP status; per-event
# failures (validation / unknown field) → results[] with HTTP 200, so one poison
# event never stalls the batch. The cloud's INDEPENDENT gate VALIDATES the already-sanitized
# output shape (state.apply_validated_event → validate.py), it does NOT re-run the transforms (I1).
# --------------------------------------------------------------------------- #
def _ingest_event_to_envelope(ev: dict) -> dict:
    """Map an ingest event (collector_ingest_api.md body) to the provider-event envelope
    that ``FrameState.apply_event`` (and thus ``sanitize_event``) consumes."""
    payload = dict(ev.get("payload") or {})
    if ev.get("account_alias") is not None and "account_alias" not in payload:
        payload["account_alias"] = ev["account_alias"]
    psid = payload.pop("session_id", None)  # session_id lives at envelope level
    return {
        "schema_version": ev.get("schema_version", 1),
        "provider": ev.get("provider", "manual"),
        "provider_event_name": ev.get("provider_event_name"),
        "provider_session_id": psid,
        "event_time": ev.get("event_time"),
        "payload": payload,
    }


def _apply_ingest_event(st: FrameState, ev: dict) -> dict:
    """Apply one ingest event, returning a per-event result dict. Never raises — a bad event
    becomes ``{"status":"rejected","reason":...}`` so the rest of the batch still applies."""
    eid = str(ev.get("event_id") or "")
    if not isinstance(ev, dict):
        return {"event_id": eid, "status": "rejected", "reason": "event_not_object"}
    etype = str(ev.get("event_type") or "")
    try:
        if etype == "collector.heartbeat":
            st.collector_heartbeat()
            return {"event_id": eid, "status": "accepted"}
        if etype == "quota.window":
            # CRITICAL (docs/devlog/16, I1): set_quota writes account_alias + provider straight
            # into the materialized frame (frame.quota[].account) served to the device, so the
            # quota branch MUST pass the same independent VALIDATE gate as session.* — it was
            # previously bypassed, letting a signed batch put "/Users/.../secret" on the device.
            from . import validate as Vd

            q = Vd.validate_quota_event(ev)  # raises SanitizationError on any non-canonical value
            st.set_quota(
                provider=q["provider"],
                account_alias=q["account_alias"],
                window_type=q["window_type"],
                used_ratio=q["used_ratio"],
                confidence=q["confidence"],
                is_estimated=q["is_estimated"],
            )
            return {"event_id": eid, "status": "accepted"}
        # session.* / alert.* / unknown → through the independent VALIDATE-only gate (I1,
        # docs/devlog/16): the collector already sanitized; the cloud validates the output
        # shape (validate.py) and never re-runs the transforms.
        st.apply_validated_event(_ingest_event_to_envelope(ev))
        return {"event_id": eid, "status": "accepted"}
    except S.SanitizationError as exc:
        return {"event_id": eid, "status": "rejected", "reason": exc.reason}
    except (KeyError, ValueError, TypeError) as exc:
        return {"event_id": eid, "status": "rejected", "reason": f"bad_event:{type(exc).__name__}"}


@app.post("/api/v1/collectors/{collector_id}/events")
async def collector_ingest(
    collector_id: str,
    request: Request,
    x_aco_key_id: str | None = Header(default=None),
    x_aco_timestamp: str | None = Header(default=None),
    x_aco_nonce: str | None = Header(default=None),
    x_aco_payload_sha256: str | None = Header(default=None),
    x_aco_signature: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> JSONResponse:
    raw = await request.body()
    verifier: I.IngestVerifier = app.state.ingest
    # Strip the v1=… prefix the contract puts on the signature header.
    sig = (x_aco_signature or "")
    if sig.lower().startswith("v1="):
        sig = sig[3:]

    v = verifier.verify(
        collector_id=collector_id,
        method="POST",
        path=f"/api/v1/collectors/{collector_id}/events",
        raw_body=raw,
        kid=x_aco_key_id or "",
        timestamp=x_aco_timestamp or "",
        nonce=x_aco_nonce or "",
        payload_sha256=(x_aco_payload_sha256 or "").lower(),
        signature=sig,
    )
    if not v.ok:
        # stale_timestamp carries server_time so the collector resyncs its clock (no loop).
        return JSONResponse(status_code=v.http_status,
                            content={"ok": False, "reason": v.reason, "server_time": v.server_time})

    # Idempotency: a retried batch (fresh nonce, SAME key) returns the prior result verbatim.
    idem = (idempotency_key or "").strip()
    if idem:
        prior = verifier.idem.get(idem, v.server_time)
        if prior is not None:
            return JSONResponse(content={**prior, "duplicate": True})

    # Parse body + enforce batch/schema limits (request-level).
    try:
        import json as _json
        body = _json.loads(raw or b"{}")
    except ValueError:
        return JSONResponse(status_code=400, content={"ok": False, "reason": "bad_json",
                                                      "server_time": v.server_time})
    if int(body.get("schema_version", 1)) != I.SUPPORTED_SCHEMA_VERSION:
        return JSONResponse(status_code=400, content={"ok": False, "reason": "schema_version_unsupported",
                                                      "server_time": v.server_time})
    events = body.get("events")
    if not isinstance(events, list):
        return JSONResponse(status_code=400, content={"ok": False, "reason": "bad_events",
                                                      "server_time": v.server_time})
    if len(events) > I.MAX_EVENTS_PER_REQUEST:
        return JSONResponse(status_code=413, content={"ok": False, "reason": "batch_too_large",
                                                      "server_time": v.server_time})

    st = _state()
    results = [_apply_ingest_event(st, ev) for ev in events]
    accepted = sum(1 for r in results if r["status"] == "accepted")
    rejected = sum(1 for r in results if r["status"] == "rejected")
    resp = {
        "ok": True,
        "server_time": v.server_time,
        "ingest_id": "ing_" + secrets.token_hex(8),
        "accepted": accepted,
        "duplicates": 0,
        "rejected": rejected,
        "results": results,
    }
    if idem:
        verifier.idem.put(idem, resp, v.server_time)
    return JSONResponse(content=resp)


@app.post("/admin/quota")
def admin_quota(body: dict = Body(...)) -> JSONResponse:
    st = _state()
    try:
        # Pass used_ratio RAW (no pre-float()) so the single quota sink (state.set_quota) does the
        # gating — pre-coercing here would let float(True)==1.0 slip a bool past the sink's
        # bool-reject (the parity divergence #5 closes). The sink validates account/provider/
        # window/ratio and raises SanitizationError on any non-canonical value.
        st.set_quota(
            provider=str(body["provider"]),
            account_alias=str(body.get("account") or body.get("account_alias") or "main"),
            window_type=str(body.get("window_type") or "5h"),
            used_ratio=body.get("used_ratio", 0.0),
            confidence=str(body.get("confidence") or "unknown"),
            is_estimated=bool(body.get("is_estimated", True)),
        )
    except (KeyError, ValueError, S.SanitizationError) as exc:
        return JSONResponse(status_code=422, content={"rejected": True, "reason": str(exc)})
    return JSONResponse(content={"applied": True, "frame": st.build_frame(DEV_DEVICE_ID)})


@app.post("/admin/heartbeat")
def admin_heartbeat() -> JSONResponse:
    _state().collector_heartbeat()
    return JSONResponse(content={"ok": True})


@app.post("/admin/reset")
def admin_reset() -> JSONResponse:
    _state().reset()
    return JSONResponse(content={"ok": True, "frame": _state().build_frame(DEV_DEVICE_ID)})


# --------------------------------------------------------------------------- #
# Live simulator — /preview (display_spec.md → Browser Simulator).
# --------------------------------------------------------------------------- #
@app.get("/preview", response_class=HTMLResponse)
def preview(request: Request) -> HTMLResponse:
    device_id = request.query_params.get("device") or DEV_DEVICE_ID
    token = request.query_params.get("token") or DEV_DEVICE_TOKEN
    return HTMLResponse(content=render_preview(device_id, token))


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #
import re as _re

_DEVICE_ID_RE = _re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _valid_device_id(device_id: str) -> bool:
    return bool(_DEVICE_ID_RE.match(device_id))


def _coerce_schema_version(raw: str | None, default: int) -> int:
    """Coerce the ``X-Frame-Schema-Version`` header to an int, never raising.
    A missing/blank/non-integer value falls back to ``default`` (negotiation
    then clamps it to the server-supported range)."""
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _to_envelope(body: dict) -> dict:
    """Wrap an admin shorthand ``{provider,account,status,project,task,...}`` into
    a provider event envelope so the same default-deny sanitizer processes it.

    The shorthand uses ``account``/``project`` keys (already-aliased neutral
    values); they map onto ``account_alias``/``project_alias`` in the payload."""
    payload: dict = {}
    if "status" in body:
        payload["status"] = body["status"]
    if "status_detail" in body:
        payload["status_detail"] = body["status_detail"]
    if "tool_category" in body:
        payload["tool_category"] = body["tool_category"]
    if "task" in body or "task_label" in body:
        payload["task_label"] = body.get("task_label", body.get("task"))
    if "project" in body or "project_alias" in body:
        payload["project_alias"] = body.get("project_alias", body.get("project"))
    if "account" in body or "account_alias" in body:
        payload["account_alias"] = body.get("account_alias", body.get("account"))
    if body.get("session_title") is not None:
        payload["session_title"] = body["session_title"]
    if "model" in body:
        payload["model"] = body["model"]
    if "error_label" in body:
        payload["error_label"] = body["error_label"]
    if "needs_attention" in body:
        payload["needs_attention"] = body["needs_attention"]
    return {
        "schema_version": 1,
        "provider": body.get("provider", "manual"),
        "adapter": "manual",
        "event_type": "session.status",
        "provider_event_name": body.get("provider_event_name", "manual.inject"),
        "provider_session_id": body.get(
            "provider_session_id",
            f"hmac:{body.get('provider','manual')}-{body.get('account','main')}-{body.get('project','x')}",
        ),
        "event_time": int(time.time()),
        "payload": payload,
    }


def main() -> None:
    import uvicorn

    bind = os.environ.get("AGENTLAMP_LOCAL_BIND", os.environ.get("AGENTLAMP_BIND", "0.0.0.0:8787"))
    host, _, port = bind.partition(":")
    uvicorn.run(app, host=host or "0.0.0.0", port=int(port or "8787"))


if __name__ == "__main__":
    main()
