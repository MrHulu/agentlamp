"""Proxy-bypassing local HTTP POST (stdlib only).

The collector POSTs to a LOCAL server (``127.0.0.1:8787``) while a Clash-family
proxy may be exporting ``http_proxy`` / ``https_proxy`` / ``all_proxy`` env vars.
Those would route a loopback request through the proxy listener and time out
silently (kickoff GOTCHA #1, cost ~20 min last session).

The fix is a per-request bypass that touches NOTHING in the system proxy config
(Boss death command #1 — never read/modify/configure the system proxy):

    urllib.request.build_opener(urllib.request.ProxyHandler({}))

``ProxyHandler({})`` with an EMPTY mapping registers ZERO scheme handlers, so
``build_opener`` drops it entirely (verified: the resulting opener has no
``ProxyHandler`` and installs no ``*_open`` proxy method) and never calls
``getproxies()`` — the env vars (``http_proxy``/``all_proxy``/``no_proxy``, any
casing) and the macOS SystemConfiguration proxy are NEVER read. We build that
opener ONCE and reuse it; we call ``_OPENER.open()`` (never ``urlopen()``, which
would use the env-derived default opener and route through the proxy).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

# Built once; reused for every call. Empty ProxyHandler => unconditional bypass.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class PostError(Exception):
    """Transport-level failure (connection refused / timeout / DNS). The caller
    decides whether to retry; carries no response body."""


def post_json(url: str, payload: dict, *, timeout: float = 3.0) -> tuple[int, dict]:
    """POST ``payload`` as JSON to ``url``, bypassing any env proxy.

    Returns ``(status_code, parsed_body)``. A 4xx/5xx is returned (not raised) so
    the caller can read an application-level rejection (e.g. the server's 422
    ``{"rejected": true, "reason": ...}``). A transport failure raises ``PostError``.
    """
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Connection": "close"},
    )
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, _read_json(resp)
    except urllib.error.HTTPError as e:  # server reached, app-level error
        body = e.read()
        try:
            return e.code, json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            return e.code, {}
    except (urllib.error.URLError, OSError, TimeoutError) as e:  # transport failure
        raise PostError(str(e)) from e


def post_empty(url: str, *, timeout: float = 3.0) -> tuple[int, dict]:
    """POST with an empty body (used for ``/admin/heartbeat``)."""
    req = urllib.request.Request(url, data=b"", method="POST", headers={"Connection": "close"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, _read_json(resp)
    except urllib.error.HTTPError as e:
        return e.code, {}
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise PostError(str(e)) from e


def _read_json(resp) -> dict:
    try:
        raw = resp.read()
        return json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        return {}
