"""Relay-mode signed push (stdlib only).

Local mode POSTs a plain shorthand body to ``/admin/event`` over loopback
(``netpost.post_json``). RELAY mode instead signs each batch and POSTs it to a
remote Cloudflare relay:

    POST {relay_host}/api/v1/collectors/{kid}/events
    X-ACO-Key-Id:        <kid>
    X-ACO-Timestamp:     <unix_seconds>
    X-ACO-Nonce:         <128-bit lowercase hex>
    X-ACO-Payload-SHA256:<hex sha256 of the EXACT raw body bytes>
    X-ACO-Signature:     v1=<hex hmac-sha256 of the canonical string>
    Idempotency-Key:     <stable batch key>

The signature is produced with the IDENTICAL canonical-string construction the
server verifies — we DRY-import ``canonical_string`` / ``sign`` /
``payload_sha256_hex`` straight from ``agentlamp_server.ingest`` (the byte-spec
single source of truth, frozen by ``tests/fixtures/parity/hmac_vectors.json``).
No re-implementation of the HMAC here — re-typing the canonical string would be a
parity foot-gun (I2).

NO hardcoded host/account: the relay host + ``kid`` + secret all arrive from the
caller (config/env/keyring), never baked in (I3).

On ``401 stale_timestamp`` the server returns its ``server_time``; the caller
resyncs ONCE from that offset (no loop — contract: "must not loop"). On a
per-event ``rejected`` in ``results[]`` (or a request-level reject) the caller
dead-letters with the reason + payload hash only, never the raw value.
"""
from __future__ import annotations

import json
import secrets
import time
import urllib.error
import urllib.request

# DRY: the canonical byte-spec lives ONLY in the server reference. config.py has
# already put <repo>/server on sys.path, so this import works from the collector.
from agentlamp_server.ingest import (  # noqa: E402
    canonical_string,
    payload_sha256_hex,
    sign,
)

# DRY: the ONLY raw->safe transform lives in the server sanitizer (BUILD-SPEC I1,
# docs/devlog/16). The relay path runs THAT sanitizer locally and serializes its
# OUTPUT — it never hand-maps raw fields (the cloud then only VALIDATES the output
# shape, never re-running 800 lines of NFKC/regex heuristics in TS).
from agentlamp_server import sanitize as S  # noqa: E402

# Reuse the same proxy-bypassing opener the local path uses (Boss death command
# #1 — never read/modify the system proxy; ProxyHandler({}) drops all schemes).
from .netpost import USER_AGENT, PostError, _OPENER  # noqa: E402

SCHEMA_VERSION = 1


class RelayPushResult:
    """Outcome of one signed push attempt.

    Attributes
    ----------
    ok:            request-level success (HTTP 200 and a parseable body)
    http_status:   the HTTP status code (or 0 on transport failure)
    reason:        request-level rejection reason (``stale_timestamp`` etc.) or ""
    server_time:   server clock echoed on a reject (for a one-shot resync)
    results:       per-event ``[{event_id,status,reason?}, ...]`` on a 200
    """

    __slots__ = ("ok", "http_status", "reason", "server_time", "results", "body")

    def __init__(self, ok, http_status, reason="", server_time=0, results=None, body=None):
        self.ok = ok
        self.http_status = http_status
        self.reason = reason
        self.server_time = server_time
        self.results = results or []
        self.body = body or {}


def _shorthand_to_sanitize_envelope(shorthand: dict) -> dict:
    """Map a collector shorthand (``normalize.normalize_record`` output:
    ``{provider, account, project, provider_session_id, model, session_title?, ...}``)
    into the PROVIDER-EVENT envelope that ``sanitize.sanitize_event`` consumes.

    This is the raw INPUT shape (``session_title``, ``project``→``project_alias``,
    ``account``→``account_alias``) — the sanitizer turns it into the safe OUTPUT
    shape (``display_title`` as a ``title-<hmac>`` label, neutral aliases, canonical
    enums). It mirrors ``app._to_envelope`` so the relay path and the local
    ``/admin/event`` path feed the SAME sanitizer the same way."""
    payload: dict = {}
    if "status" in shorthand:
        payload["status"] = shorthand["status"]
    if "status_detail" in shorthand:
        payload["status_detail"] = shorthand["status_detail"]
    if "tool_category" in shorthand:
        payload["tool_category"] = shorthand["tool_category"]
    if "task" in shorthand or "task_label" in shorthand:
        payload["task_label"] = shorthand.get("task_label", shorthand.get("task"))
    if "project" in shorthand or "project_alias" in shorthand:
        payload["project_alias"] = shorthand.get("project_alias", shorthand.get("project"))
    if "account" in shorthand or "account_alias" in shorthand:
        payload["account_alias"] = shorthand.get("account_alias", shorthand.get("account"))
    if "model" in shorthand:
        payload["model"] = shorthand["model"]
    if "error_label" in shorthand:
        payload["error_label"] = shorthand["error_label"]
    if "confidence" in shorthand:
        payload["confidence"] = shorthand["confidence"]
    if "needs_attention" in shorthand:
        payload["needs_attention"] = shorthand["needs_attention"]
    if shorthand.get("session_title") is not None:
        # RAW title — the sanitizer (safe_title) is the gate that drops a
        # path/secret-bearing title and HMAC-collapses it to ``title-<hmac>``.
        payload["session_title"] = shorthand["session_title"]
    return {
        "schema_version": 1,
        "provider": shorthand.get("provider", "manual"),
        "provider_event_name": shorthand.get("provider_event_name", "manual.inject"),
        "provider_session_id": shorthand.get("provider_session_id"),
        "event_time": shorthand.get("event_time"),
        "payload": payload,
    }


def build_ingest_event(
    shorthand: dict,
    *,
    source_seq: int,
    event_time: float | None = None,
    pepper: bytes,
    aliases: "S.AliasMap | None" = None,
    local_display: bool = False,
) -> dict:
    """Wrap one collector shorthand body into the ingest event shape the relay
    VALIDATES (``collector_ingest_api.md`` → Body).

    BUILD-SPEC I1/I2 (docs/devlog/16): the payload here is the SERIALIZED OUTPUT of
    the collector-side sanitizer (``sanitize.sanitize_event`` run with the
    collector's pepper + alias map, relay mode = ``local_display=False``). We do NOT
    hand-map raw fields — a raw ``session_title`` becomes the sanitized ``display_title``
    (a ``title-<hmac>`` label), enums are canonical, aliases neutral/HMAC. The cloud
    then only validates this output shape (``validate.validate_sanitized_event``); it
    never re-runs the transforms. ``session_id`` rides in the payload (the server pops
    it to the envelope level); there is NO ``updated_at`` in the payload — event timing
    rides at the envelope level (``event_time``) only.
    """
    if not isinstance(pepper, (bytes, bytearray)) or not pepper:
        raise ValueError("build_ingest_event requires a non-empty pepper (collector-side sanitize)")
    et = int(event_time if event_time is not None else time.time())
    aliases = aliases if aliases is not None else S.AliasMap()

    # Run the ONLY transform — the collector-side sanitizer — and serialize its OUTPUT.
    # relay mode keeps the strict neutral shape (no readable display labels leak).
    # ``local_display`` is the OWNER opt-in (config.OWNER_LABELS): relay mode normally keeps the
    # strict neutral shape (HMAC labels, no readable names leak), but an owner mirroring to their
    # OWN private relay can pass readable project/title labels via the sanitizer's single-owner
    # ``display`` path. Default False keeps the public-repo posture HMAC-safe.
    src_envelope = _shorthand_to_sanitize_envelope(shorthand)
    src_envelope["event_time"] = et
    clean = S.sanitize_event(src_envelope, aliases=aliases, pepper=pepper, local_display=local_display)

    # clean["payload"] is already the safe OUTPUT shape (display_title/neutral aliases/
    # canonical enums, NO session_title, NO updated_at). Copy it verbatim.
    payload: dict = dict(clean.get("payload") or {})

    provider = clean.get("provider", shorthand.get("provider", "manual"))
    # session_id rides in the payload (the server pops it to the envelope level).
    psid = clean.get("provider_session_id")
    if psid is not None:
        payload["session_id"] = psid

    # event_id: per-event idempotency anchor; dedupe_key: server-side replay dedupe.
    eid = "evt_" + secrets.token_hex(8)
    return {
        "event_id": eid,
        "event_type": "session.status",
        "provider": provider,
        "provider_event_name": clean.get("provider_event_name", "manual.inject"),
        "account_alias": payload.get("account_alias", "main"),
        "source_seq": source_seq,
        "event_time": et,
        "dedupe_key": f"{provider}:{psid or 'nosession'}:{source_seq}",
        "payload": payload,
    }


def build_heartbeat_event(*, source_seq: int = 1, event_time: float | None = None) -> dict:
    """A SIGNED ``collector.heartbeat`` ingest event (P1 liveness, docs/devlog/16).

    In relay mode a posted session event already refreshes the cloud's collector
    heartbeat — but when the owner is idle-but-present (no hook firing) the relay's
    ``last_collector_heartbeat`` would go stale and the whole fleet would flip to
    offline. So the daemon periodically pushes THIS event, signed exactly like any
    ingest batch (X-ACO-* over the canonical body).

    The cloud short-circuits ``event_type == "collector.heartbeat"`` BEFORE the
    validate-only gate (server ``app._apply_ingest_event`` / cloud ``relay_do.ts``):
    it just bumps the heartbeat clock and returns ``accepted``. So the event carries
    NO payload — there is nothing to sanitize or leak (no cwd/title/model), which is
    why it never goes near the I1 transforms. It rides as a normal 1-event batch.
    """
    et = int(event_time if event_time is not None else time.time())
    return {
        "event_id": "hb_" + secrets.token_hex(8),
        "event_type": "collector.heartbeat",
        "provider": "collector",
        "provider_event_name": "collector.heartbeat",
        "source_seq": source_seq,
        "event_time": et,
        "dedupe_key": f"collector.heartbeat:{et}:{source_seq}",
        # No payload: a heartbeat carries no sanitizable content (it never touches the
        # I1 transforms — the cloud bumps its clock and returns accepted).
        "payload": {},
    }


def build_request_body(events: list[dict], *, collector_id: str, batch_id: str | None = None) -> bytes:
    """Serialize a batch into the EXACT raw bytes that get hashed + signed.

    The bytes the signature covers MUST be the bytes sent — we compute the body
    ONCE here and the caller transmits this same buffer (never re-serializes), so
    ``payload_sha256`` always matches what the server recomputes over the body.
    """
    body = {
        "schema_version": SCHEMA_VERSION,
        "collector_id": collector_id,
        "sent_at": int(time.time()),
        "batch_id": batch_id or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(3),
        "events": events,
    }
    # ensure_ascii=False keeps neutral unicode aliases byte-stable across languages;
    # separators drop whitespace so the hash is deterministic.
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_headers(*, secret: bytes, kid: str, collector_id: str, raw_body: bytes,
                 timestamp: int | None = None, nonce: str | None = None,
                 idempotency_key: str | None = None) -> dict:
    """Produce the X-ACO-* headers for a signed push, using the server's canonical
    string verbatim (DRY import). Returns a ready-to-send header dict."""
    ts = int(timestamp if timestamp is not None else time.time())
    nc = nonce or secrets.token_hex(16)          # 128-bit lowercase hex
    path = f"/api/v1/collectors/{collector_id}/events"
    digest = payload_sha256_hex(raw_body)
    canonical = canonical_string("POST", path, kid, str(ts), nc, digest)
    signature = sign(secret, canonical)
    headers = {
        "Content-Type": "application/json",
        "Connection": "close",
        "User-Agent": USER_AGENT,  # Cloudflare edge blocks the stock python-urllib UA (error 1010)
        "X-ACO-Key-Id": kid,
        "X-ACO-Timestamp": str(ts),
        "X-ACO-Nonce": nc,
        "X-ACO-Payload-SHA256": digest,
        "X-ACO-Signature": "v1=" + signature,
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def push_batch(*, relay_host: str, collector_id: str, kid: str, secret: bytes,
               shorthands: list[dict], pepper: bytes,
               aliases: "S.AliasMap | None" = None, clock_offset: float = 0.0,
               batch_id: str | None = None, idempotency_key: str | None = None,
               local_display: bool = False,
               timeout: float = 5.0) -> RelayPushResult:
    """Sign + POST one batch of shorthand bodies to the relay.

    ``relay_host`` is the caller-supplied base URL (config/env, never hardcoded).
    ``pepper`` + ``aliases`` drive the collector-side sanitizer (I1: the relay path
    serializes the sanitizer OUTPUT, never raw fields). ``clock_offset`` is the
    (server_time - local_time) correction learned from a prior ``stale_timestamp``
    resync — applied to the timestamp so a skewed local clock still produces a fresh,
    in-window signature.

    Returns a ``RelayPushResult``; raises ``PostError`` only on a transport failure
    (so the caller can leave the records and retry, exactly like local mode).
    """
    host = relay_host.rstrip("/")
    events = [
        build_ingest_event(s, source_seq=i + 1, pepper=pepper, aliases=aliases,
                           local_display=local_display)
        for i, s in enumerate(shorthands)
    ]
    raw = build_request_body(events, collector_id=collector_id, batch_id=batch_id)
    ts = int(time.time() + clock_offset)
    headers = sign_headers(
        secret=secret, kid=kid, collector_id=collector_id, raw_body=raw,
        timestamp=ts, idempotency_key=idempotency_key,
    )
    url = f"{host}/api/v1/collectors/{collector_id}/events"
    req = urllib.request.Request(url, data=raw, method="POST", headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            body = _read_json(resp)
            return RelayPushResult(
                ok=True, http_status=resp.status, server_time=int(body.get("server_time", 0)),
                results=body.get("results") or [], body=body,
            )
    except urllib.error.HTTPError as e:  # request-level reject (401/403/409/413/...)
        body = _read_json_from_bytes(e.read())
        return RelayPushResult(
            ok=False, http_status=e.code, reason=str(body.get("reason", "")),
            server_time=int(body.get("server_time", 0)), results=body.get("results") or [],
            body=body,
        )
    except (urllib.error.URLError, OSError, TimeoutError) as e:  # transport
        raise PostError(str(e)) from e


def push_heartbeat(*, relay_host: str, collector_id: str, kid: str, secret: bytes,
                   clock_offset: float = 0.0, timeout: float = 5.0) -> RelayPushResult:
    """Sign + POST a single ``collector.heartbeat`` event to the relay (P1 liveness).

    Same signed-batch path as ``push_batch`` (the cloud only accepts SIGNED ingest
    batches on this route), but the single event is a payload-less heartbeat — so it
    never runs the collector-side sanitizer and never touches the I1 transforms.
    A FRESH nonce is generated per call (no idempotency key — each heartbeat is a new
    liveness ping, not a retried record), so a replayed old heartbeat is rejected by
    the relay's nonce/timestamp window. Raises ``PostError`` only on transport failure.
    """
    host = relay_host.rstrip("/")
    ev = build_heartbeat_event()
    raw = build_request_body([ev], collector_id=collector_id)
    ts = int(time.time() + clock_offset)
    headers = sign_headers(secret=secret, kid=kid, collector_id=collector_id, raw_body=raw,
                           timestamp=ts)
    url = f"{host}/api/v1/collectors/{collector_id}/events"
    req = urllib.request.Request(url, data=raw, method="POST", headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            body = _read_json(resp)
            return RelayPushResult(
                ok=True, http_status=resp.status, server_time=int(body.get("server_time", 0)),
                results=body.get("results") or [], body=body,
            )
    except urllib.error.HTTPError as e:
        body = _read_json_from_bytes(e.read())
        return RelayPushResult(
            ok=False, http_status=e.code, reason=str(body.get("reason", "")),
            server_time=int(body.get("server_time", 0)), results=body.get("results") or [],
            body=body,
        )
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise PostError(str(e)) from e


def build_quota_event(quota: dict, *, source_seq: int, event_time: float | None = None) -> dict:
    """Wrap one estimated quota window (``quota.compute_quota`` output) into the ``quota.window``
    ingest event the relay validates (``validate.validateQuotaEvent``).

    The payload carries ONLY neutral numeric fields (provider + neutral account + window_type +
    used_ratio) — no transcript content, path, or prompt. There is nothing to sanitize, so (like
    ``collector.heartbeat``) it never touches the I1 transforms; the relay validates the shape and
    writes it straight into the materialized frame's ``quota`` block.
    """
    et = int(event_time if event_time is not None else time.time())
    provider = str(quota.get("provider", "claude"))
    account = str(quota.get("account_alias", "main"))
    window = str(quota.get("window_type", "5h"))
    return {
        "event_id": "qta_" + secrets.token_hex(8),
        "event_type": "quota.window",
        "provider": provider,
        "provider_event_name": "quota.sample",
        "account_alias": account,
        "source_seq": source_seq,
        "event_time": et,
        "dedupe_key": f"quota:{provider}:{account}:{window}:{et}",
        "payload": _quota_payload(quota, window),
    }


def _quota_payload(quota: dict, window: str) -> dict:
    """Numeric quota payload + optional display metadata (plan tier + reset epoch) when present."""
    payload = {
        "window_type": window,
        "used_ratio": float(quota.get("used_ratio", 0.0)),
        "confidence": str(quota.get("confidence", "low")),
        "is_estimated": bool(quota.get("is_estimated", True)),
    }
    plan = quota.get("plan")
    if plan:
        payload["plan"] = str(plan)
    reset = quota.get("reset_at")
    if isinstance(reset, (int, float)) and not isinstance(reset, bool) and reset > 0:
        payload["reset_at"] = int(reset)
    return payload


def push_quota(*, relay_host: str, collector_id: str, kid: str, secret: bytes,
               quotas: list[dict], clock_offset: float = 0.0, timeout: float = 5.0) -> RelayPushResult:
    """Sign + POST a batch of ``quota.window`` events to the relay (one per window).

    Same signed-batch transport as ``push_batch``/``push_heartbeat`` (the cloud only accepts SIGNED
    ingest batches), but the events are payload-numeric quota samples — no collector-side sanitizer
    runs. Raises ``PostError`` only on a transport failure (caller leaves it for the next cycle).
    """
    host = relay_host.rstrip("/")
    events = [build_quota_event(q, source_seq=i + 1) for i, q in enumerate(quotas)]
    raw = build_request_body(events, collector_id=collector_id)
    ts = int(time.time() + clock_offset)
    headers = sign_headers(secret=secret, kid=kid, collector_id=collector_id, raw_body=raw,
                           timestamp=ts)
    url = f"{host}/api/v1/collectors/{collector_id}/events"
    req = urllib.request.Request(url, data=raw, method="POST", headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            body = _read_json(resp)
            return RelayPushResult(
                ok=True, http_status=resp.status, server_time=int(body.get("server_time", 0)),
                results=body.get("results") or [], body=body,
            )
    except urllib.error.HTTPError as e:
        body = _read_json_from_bytes(e.read())
        return RelayPushResult(
            ok=False, http_status=e.code, reason=str(body.get("reason", "")),
            server_time=int(body.get("server_time", 0)), results=body.get("results") or [],
            body=body,
        )
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise PostError(str(e)) from e


def resync_offset(server_time: int) -> float:
    """Compute the clock offset to apply after a ``stale_timestamp`` (server_time -
    local). Applied ONCE per stale reject — the caller does NOT loop (contract)."""
    if not server_time:
        return 0.0
    return float(server_time) - time.time()


def _read_json(resp) -> dict:
    try:
        return _read_json_from_bytes(resp.read())
    except Exception:
        return {}


def _read_json_from_bytes(raw: bytes) -> dict:
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        return {}
