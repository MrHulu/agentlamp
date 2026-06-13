"""Cloud-side VALIDATE-ONLY gate (relay mode) — the independent second gate.

🚨 BUILD-SPEC invariant I1 (docs/devlog/16): the collector (Python) is the ONLY component
that performs raw->safe TRANSFORMS (``sanitize.py``). In relay mode it pushes the
*already-sanitized* output to the cloud. This module is the cloud's independent gate: it
does **NOT** re-run the transforms — re-deriving 800 lines of NFKC/regex heuristics in a
second runtime (and, for the TS port, a different RegExp/Unicode engine) risks silent
under-redaction. Instead it STRICTLY VALIDATES that the received event is already clean:

  1. key allowlist + forbidden-key reject (envelope + payload),
  2. forbidden-pattern reject scan over every leaf (the backstop),
  3. provider mandatory + enum,
  4. typed enum membership (validate-if-present; absence is not a leak),
  5. ``needs_attention`` is bool,
  6. alias-shape fields positively match the neutral display shape (relay strict).

Reject — never coerce. A non-canonical value means a buggy/hostile collector; the event is
dropped. This is the Python REFERENCE for the TypeScript Worker/DO gate
(``src/cloud/src/validate.ts``); both assert against
``tests/fixtures/parity/sanitize_corpus.json`` (generated from here). DRY: every enum /
forbidden-key set / alias-shape gate is imported from ``sanitize.py`` — zero duplication on
the Python side; the TS side reimplements and is corpus-verified.
"""
from __future__ import annotations

from . import sanitize as S

# The sanitized-OUTPUT envelope shape (sanitize_event's ``out``), NOT the raw input.
VALIDATE_ENVELOPE_KEYS = {
    "schema_version",
    "provider",
    "provider_event_name",
    "provider_session_id",
    "event_time",
    "payload",
    "sanitization",
}

# The sanitized-OUTPUT payload shape. NOTE: ``display_title`` (output), never
# ``session_title`` (the raw input key sanitize.py consumes) — the collector already
# transformed it.
VALIDATE_PAYLOAD_KEYS = {
    "status",
    "tool_category",
    "status_detail",
    "task_label",
    "project_alias",
    "account_alias",
    "display_title",
    "model",
    "error_label",
    "confidence",
    "needs_attention",
}

# enum field -> allowed set (validate-if-present). status compares upper-case, the rest lower.
_ENUM_FIELDS = {
    "status": S.STATUS_ENUM,
    "tool_category": S.TOOL_CATEGORY_ENUM,
    "status_detail": S.STATUS_DETAIL_ENUM,
    "task_label": S.TASK_LABEL_ENUM,
    "model": S.MODEL_ENUM,
    "error_label": S.ERROR_LABEL_ENUM,
    "confidence": S.CONFIDENCE_ENUM,
}

# alias-shape fields: must POSITIVELY match the neutral display shape (relay strict — never
# the readable display label, which is local-mode only).
_ALIAS_FIELDS = ("project_alias", "account_alias", "display_title")

# Quota windows the device renders (device_frame_api.md → Frame Schema v1 → quota: w5 / week).
_QUOTA_WINDOW_TYPES = {"5h", "week"}


def _reject_keys(obj: dict, known: set, *, where: str, ph: str) -> None:
    """Default-deny: any forbidden raw-leak key, or any key outside ``known``, rejects."""
    for key in obj:
        if key in S._FORBIDDEN_KEYS:
            raise S.SanitizationError(f"forbidden_key:{where}.{key}", ph)
        if key not in known:
            raise S.SanitizationError(f"unknown_field:{where}.{key}", ph)


def validate_sanitized_event(event: dict) -> dict:
    """Validate an already-sanitized event envelope. Returns it unchanged, or raises
    ``S.SanitizationError`` (metadata only) on the first violation. Pure validation — no
    transform. Strict: a non-canonical value rejects rather than coerces."""
    ph = S.payload_hash(event)
    if not isinstance(event, dict):
        raise S.SanitizationError("event_not_object", ph)

    # 1. key allowlist + forbidden-key reject.
    _reject_keys(event, VALIDATE_ENVELOPE_KEYS, where="event", ph=ph)
    payload = event.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise S.SanitizationError("payload_not_object", ph)
    _reject_keys(payload, VALIDATE_PAYLOAD_KEYS, where="payload", ph=ph)

    # 2. forbidden-pattern reject scan over EVERY leaf (here ``model`` is already an enum and
    #    ``display_title`` is already an hmac/neutral label, so nothing is exempt — stricter
    #    than the collector's input scan, which exempts the two pre-transform fields).
    S._scan_leaves({k: v for k, v in event.items() if k != "payload"}, ph)
    S._scan_leaves(payload, ph)

    # 3. provider mandatory + enum.
    if str(event.get("provider", "")).strip().lower() not in S.PROVIDER_ENUM:
        raise S.SanitizationError("provider_not_in_enum", ph)

    # 4. typed enum membership (validate-if-present, strict — no coercion).
    for f, allowed in _ENUM_FIELDS.items():
        if payload.get(f) is not None:
            val = str(payload[f]).strip()
            cmp = val.upper() if f == "status" else val.lower()
            if cmp not in allowed:
                raise S.SanitizationError(f"enum:{f}", ph)

    # 5. needs_attention is bool if present.
    if "needs_attention" in payload and not isinstance(payload["needs_attention"], bool):
        raise S.SanitizationError("needs_attention_not_bool", ph)

    # 6. alias-shape fields: positively neutral (relay strict) OR the owner display-label
    #    shape (multi-segment kebab/underscore, bounded — sanitize.looks_like_display_label).
    #    Owner-labels mode legitimately transits readable labels (folder names / session
    #    titles, e.g. ``fix-orphan-chrome-processes``); the display shape still bans every
    #    path/email/scheme character and step 2's forbidden scan already ran on every leaf.
    for f in _ALIAS_FIELDS:
        v = payload.get(f)
        if v is not None and not (
            S.looks_like_neutral_alias(str(v)) or S.looks_like_display_label(str(v))
        ):
            raise S.SanitizationError(f"alias_shape:{f}", ph)

    # 7. provider_session_id shape gate (2026-06-03 hardening): it is leaf-scanned (step 2) but
    #    a forbidden-pattern-clean free-text string ("please fix auth now") would otherwise
    #    survive as the stored SESSION KEY. Require the canonical opaque shape (`hmac:<hex>` or a
    #    high-entropy token) — reject, never coerce, a non-canonical id (a buggy/hostile collector).
    psid = event.get("provider_session_id")
    if psid is not None and not S.looks_like_session_id(str(psid)):
        raise S.SanitizationError("session_id_shape", ph)

    return event


def validate_quota_event(ev: dict) -> dict:
    """Validate a ``quota.window`` ingest event BEFORE it reaches ``set_quota`` (the
    CRITICAL second gate this module exists for). ``set_quota`` writes ``account_alias`` +
    ``provider`` straight into the materialized frame (``frame.quota[].account``), so the
    quota branch MUST pass the SAME default-deny gate as ``session.*``: the attacker-controlled
    ``account_alias`` is otherwise served to the device verbatim (e.g. ``/Users/.../secret``).

    Reject — never coerce or store a raw value:
      * ``provider`` mandatory + in :data:`sanitize.PROVIDER_ENUM`,
      * ``account_alias`` POSITIVELY neutral (``looks_like_neutral_alias``) AND clean of any
        forbidden pattern (``assert_clean`` — the leaf backstop),
      * ``window_type`` in ``{"5h", "week"}``,
      * ``used_ratio`` a FINITE float in ``0..1`` (NaN / inf / out-of-range reject — mirrors
        Python ``float()`` raising → rejected, the NaN divergence the TS port must match),
      * ``confidence`` (if present) in :data:`sanitize.CONFIDENCE_ENUM`.

    Returns the resolved ``{provider, account_alias, window_type, used_ratio, confidence,
    is_estimated}`` on success, or raises ``S.SanitizationError`` (metadata only). The caller
    feeds the returned canonical values into ``set_quota`` — never the raw event.
    """
    import math

    if not isinstance(ev, dict):
        raise S.SanitizationError("event_not_object", "")
    p = ev.get("payload")
    if p is None:
        p = {}
    if not isinstance(p, dict):
        raise S.SanitizationError("payload_not_object", S.payload_hash(ev))
    ph = S.payload_hash(ev)

    # provider mandatory + enum (envelope or payload, mirroring the ingest branch's lookup).
    provider = str(ev.get("provider") or p.get("provider") or "").strip().lower()
    if provider not in S.PROVIDER_ENUM:
        raise S.SanitizationError("provider_not_in_enum", ph)

    # account_alias: positively neutral AND forbidden-pattern clean (no /Users/, no plan tier,
    # no path/email/key smuggled in). Never coerce — a non-neutral value rejects the event.
    account_alias = str(ev.get("account_alias") or p.get("account_alias") or "")
    S.assert_clean(account_alias, ph=ph)
    if not S.looks_like_neutral_alias(account_alias):
        raise S.SanitizationError("alias_shape:account_alias", ph)

    # window_type enum.
    window_type = str(p.get("window_type") or "")
    if window_type not in _QUOTA_WINDOW_TYPES:
        raise S.SanitizationError("enum:window_type", ph)

    # used_ratio: a finite float in [0, 1]. Reject bool BEFORE float() — Python float(True)==1.0
    # would silently coerce a boolean into a ratio, whereas TS (Number(true)===1 but the gate
    # rejects typeof boolean) drops it: a parity divergence the corpus `reject_used_ratio_bool`
    # case locks. NaN / inf / non-numeric / out-of-range reject (never clamp — a raw
    # out-of-range value means a buggy/hostile collector).
    raw_ratio = p.get("used_ratio")
    if isinstance(raw_ratio, bool):
        raise S.SanitizationError("quota_used_ratio_not_float", ph)
    try:
        used_ratio = float(raw_ratio)
    except (TypeError, ValueError):
        raise S.SanitizationError("quota_used_ratio_not_float", ph)
    if not math.isfinite(used_ratio) or used_ratio < 0.0 or used_ratio > 1.0:
        raise S.SanitizationError("quota_used_ratio_out_of_range", ph)

    # confidence (optional): enum-if-present.
    confidence = p.get("confidence")
    if confidence is not None:
        confidence = str(confidence).strip().lower()
        if confidence not in S.CONFIDENCE_ENUM:
            raise S.SanitizationError("enum:confidence", ph)
    else:
        confidence = "unknown"

    # plan (optional display metadata): keep a recognized tier lowercased; drop anything else
    # (forgiving — an unrecognized plan must never reject the quota). This is the ONE field where a
    # plan tier is intentionally allowed (it never flows through the alias neutrality check).
    plan = ""
    plan_raw = p.get("plan")
    if plan_raw is not None:
        pv = str(plan_raw).strip().lower()
        if pv in S._PLAN_TIERS or pv in ("free", "unknown"):
            plan = pv

    # reset_at (optional): finite epoch seconds > 0; otherwise dropped to None (forgiving; reject
    # bool BEFORE float() like used_ratio so True→1.0 can't masquerade as a timestamp).
    reset_at = None
    reset_raw = p.get("reset_at")
    if reset_raw is not None and not isinstance(reset_raw, bool):
        try:
            rn = float(reset_raw)
        except (TypeError, ValueError):
            rn = None
        if rn is not None and math.isfinite(rn) and rn > 0:
            reset_at = int(rn)

    return {
        "provider": provider,
        "account_alias": account_alias,
        "window_type": window_type,
        "used_ratio": used_ratio,
        "confidence": confidence,
        "is_estimated": bool(p.get("is_estimated", True)),
        "plan": plan,
        "reset_at": reset_at,
    }
