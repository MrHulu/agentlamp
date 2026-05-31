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
import time

from fastapi import Body, FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import sanitize as S
from .preview import render_preview
from .state import FRAME_SCHEMA_VERSION, FrameState

# --------------------------------------------------------------------------- #
# Configuration (env-overridable; never hard-commit a production secret).
# --------------------------------------------------------------------------- #
DEV_DEVICE_ID = os.environ.get("AGENTLAMP_DEV_DEVICE_ID", "orb-01")
DEV_DEVICE_TOKEN = os.environ.get("AGENTLAMP_DEV_DEVICE_TOKEN", "dev-local-token")
ALIAS_FILE = os.environ.get("AGENTLAMP_ALIAS_FILE", "~/.config/agentlamp/aliases.toml")


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


@app.post("/admin/quota")
def admin_quota(body: dict = Body(...)) -> JSONResponse:
    st = _state()
    try:
        st.set_quota(
            provider=str(body["provider"]),
            account_alias=str(body.get("account") or body.get("account_alias") or "main"),
            window_type=str(body.get("window_type") or "5h"),
            used_ratio=float(body.get("used_ratio", 0.0)),
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
