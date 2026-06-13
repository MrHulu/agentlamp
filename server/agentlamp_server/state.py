"""Aggregation + display-priority + frame generation (local mode).

In local mode the collector owns aggregation, priority, and frame generation
directly (``docs/architecture/architecture.md`` → Ownership Boundaries). The
priority and frame-generation rules are the single source of truth in
``docs/cloud/cloud_contract.md`` and reused verbatim here.

Pipeline:  events → sessions/quota → priority order → scene selection → frame.

Session liveness (``architecture.md`` → Session Lifetime / Liveness):
  * no event for a session > **120 s** → ``STALE``
  * no event for a session > **600 s** → ``OFFLINE``/closed

Frame constraints (``device_frame_api.md``):
  * body < 2 KB (hard cap, trimmed server-side before send),
  * ``fleet`` ≤ 5 (the device renders 5 rows; truncate lowest priority, overflow active
    agents fold into the ``fleet_more`` count),
  * ``quota`` ≤ 2 (top-2 risk),
  * ``primary.provider`` is the Title-case provider label (``Codex``/``Claude``); a
    ``fleet`` row's ``provider`` field instead carries the CLEAN project label (rows group
    by project) with the count in the separate ``count`` field — no baked ``xN`` suffix.
"""
from __future__ import annotations

import json
import os
import secrets as _secrets
import threading
import time
from dataclasses import dataclass, field

from . import sanitize as S

# --------------------------------------------------------------------------- #
# Liveness timeouts (architecture.md → Session Lifetime / Liveness).
# --------------------------------------------------------------------------- #
# Env-tunable (defaults match architecture.md). Lets an operator widen the windows
# for a quieter lamp, or compress them for a fast end-to-end test.
STALE_AFTER_S = float(os.environ.get("AGENTLAMP_STALE_AFTER_S", "120"))
OFFLINE_AFTER_S = float(os.environ.get("AGENTLAMP_OFFLINE_AFTER_S", "600"))
COLLECTOR_HEARTBEAT_STALE_S = float(os.environ.get("AGENTLAMP_HEARTBEAT_STALE_S", "90"))
# A finished/idle session stays on the fleet ROSTER for this long after its last event, so a
# session COMPLETING does not instantly vanish from the device list (Boss 2026-06-09: "session
# 完成后你就不显示了"). Active sessions still appear regardless; this only governs how long the
# calm DONE/IDLE states linger as roster rows before they age off (active sessions age to
# STALE/OFFLINE via _effective_status independently).
ROSTER_TTL_S = float(os.environ.get("AGENTLAMP_ROSTER_TTL_S", "1800"))

# Frame TTL (poll interval is 3-5 s; ttl is the firmware's grace window).
FRAME_TTL = 5
FRAME_SCHEMA_VERSION = 1
FRAME_BYTE_CAP = 2048
# Max fleet rows in a frame. The device renders exactly this many; keeping the WIRE cap
# equal to the render cap means no row is sent-but-invisible and "5" has one home.
# (firmware renderer.h::fleet draws min(fleetCount, 5); preview.py slices to the same.)
FLEET_MAX_ROWS = 5

# Quota risk threshold that flips the scene to `alert` / contributes a modifier.
QUOTA_DANGER_RATIO = 0.90
LOW_QUOTA_MODIFIER_RATIO = 0.80  # "Low quota: +30" applies at/above this burn.

# --------------------------------------------------------------------------- #
# Priority rules (cloud_contract.md → Priority Rules). Single source of truth.
# --------------------------------------------------------------------------- #
BASE_SCORE = {
    "WAITING": 100,
    "ERROR": 90,
    "CODING": 70,
    "THINKING": 65,
    "TESTING": 60,
    "READING": 55,
    "DONE": 20,
    "IDLE": 0,
    "UNKNOWN": 0,  # internal fallback, scores like IDLE, never a distinct scene
    # OFFLINE/STALE are liveness states, scored low so a live session wins focus.
    "STALE": 5,
    "OFFLINE": 0,
}

MODIFIER_LOW_QUOTA = 30
MODIFIER_PINNED = 50
MODIFIER_STALE_10MIN = -20

# "Active" = an agent genuinely working right now. IDLE/DONE/UNKNOWN are calm, and
# STALE/OFFLINE are liveness (not-working) states. Single source of truth shared by
# the scene selector (≥2 active → fleet overview) and the fleet-row aggregation, so
# the "how many are busy" answer can never drift between the two (R2/TASK-010).
_ACTIVE_EXCLUDED = frozenset({"IDLE", "DONE", "UNKNOWN", "STALE", "OFFLINE"})


def _is_active(eff_status: str) -> bool:
    return eff_status not in _ACTIVE_EXCLUDED


def _display_label(s: "Session") -> str:
    """The row/focus label for a session: its sanitized title when the session is named
    (claude --name / /rename → display_title), else the project alias. Lets named sessions
    surface individually instead of collapsing into the project's `ai-center xN` aggregate
    (R4/TASK-012). Falls back to the em-dash placeholder when neither is present."""
    return s.display_title or s.project_alias or "—"

# --------------------------------------------------------------------------- #
# status → accent (from the design mockup CSS, docs/ui/mockups/scenes.html).
# accent enum: blue|cyan|purple|yellow|green|red|white|muted.
#   idle    #4d7cff  blue       thinking #6d6bff  purple
#   coding  #a06bff  purple     reading  #22d3ee  cyan
#   testing #2dd4a7  green      waiting  #ffb020  yellow
#   done    #34d399  green      error    #ff5470  red
#   offline #7b8696  muted      stale    #d6dae0  white
# --------------------------------------------------------------------------- #
STATUS_ACCENT = {
    "IDLE": "blue",
    "THINKING": "purple",
    "CODING": "purple",
    "READING": "cyan",
    "TESTING": "green",
    "WAITING": "yellow",
    "DONE": "green",
    "ERROR": "red",
    "OFFLINE": "muted",
    "STALE": "white",
    "UNKNOWN": "muted",  # firmware renders UNKNOWN muted, like IDLE
}

# Provider wire enum → Title-case display label (device_frame_api.md).
PROVIDER_DISPLAY = {"codex": "Codex", "claude": "Claude", "manual": "Manual"}

# Scene headlines (one dominant word; display_spec.md → Layout Rules).
SCENE_HEADLINE = {
    "boot": "AGENTLAMP",
    "pairing": "PAIRING",
    "fleet": "AGENTS",
    "focus": "FOCUS",
    "quota": "QUOTA",
    "alert": "ACTION REQUIRED",
    "offline": "OFFLINE",
    "stale": "STALE",
    "diagnostics": "DIAGNOSTICS",
    "sleep": "SLEEP",
}


# --------------------------------------------------------------------------- #
# In-memory materialized state.
# --------------------------------------------------------------------------- #
@dataclass
class Session:
    provider: str               # wire enum: codex|claude|manual
    account_alias: str
    project_alias: str
    status: str                 # status enum
    task_label: str
    model: str = "unknown"
    session_id: str = ""        # HMAC label
    started_at: float = 0.0
    updated_at: float = 0.0
    needs_attention: bool = False
    error_label: str | None = None
    pinned: bool = False
    display_title: str | None = None   # sanitized session title (Claude --name/rename); None=use project

    def key(self) -> tuple[str, str, str]:
        return (self.provider, self.account_alias, self.session_id or self.project_alias)


@dataclass
class QuotaWindow:
    provider: str
    account_alias: str
    window_type: str            # "5h" | "week"
    used_ratio: float
    confidence: str = "unknown"
    is_estimated: bool = True
    plan: str = ""              # subscription tier (e.g. "max"/"pro"); "" when unknown
    reset_at: int | None = None  # epoch seconds this window resets; None when unknown
    updated_at: float = 0.0

    def key(self) -> tuple[str, str, str]:
        return (self.provider, self.account_alias, self.window_type)


@dataclass
class AccountQuota:
    """Per-(provider, account) quota for the frame: BOTH windows in one entry.

    The frame schema (device_frame_api.md → Frame Schema v1) shows a single quota
    object per account carrying both ``w5`` and ``week`` — the runtime stores the
    two windows as separate ``QuotaWindow`` records, so the frame generator merges
    them here. ``risk`` (max of the two window ratios) drives the top-2 selection
    and the quota-danger alert."""
    provider: str
    account_alias: str
    w5: float | None = None
    week: float | None = None
    confidence: str = "unknown"
    is_estimated: bool = True
    plan: str = ""
    w5_reset: int | None = None
    week_reset: int | None = None

    @property
    def risk(self) -> float:
        return max(self.w5 or 0.0, self.week or 0.0)


@dataclass
class Device:
    device_id: str
    token_hash: str
    bound_collectors: set[str] = field(default_factory=set)


def _now() -> float:
    return time.time()


def _hash_token(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class FrameState:
    """Thread-safe in-process state + frame generator for the local server."""

    def __init__(
        self,
        *,
        pepper: bytes | None = None,
        aliases: S.AliasMap | None = None,
        device_token: str = "dev-local-token",
        device_id: str = "orb-01",
    ) -> None:
        self._lock = threading.RLock()
        self.pepper = pepper or _gen_pepper()
        self.aliases = aliases or S.AliasMap()
        # Local single-owner lamp shows readable folder names (not HMAC hashes);
        # a future relay would set this 0 to force opaque labels.
        self.local_display = os.environ.get("AGENTLAMP_LOCAL_DISPLAY", "1") == "1"
        self.sessions: dict[tuple, Session] = {}
        self.quota: dict[tuple, QuotaWindow] = {}
        self.last_collector_heartbeat: float = _now()
        self._seq = 0
        self._last_signature: str | None = None
        # Overflow active-agent count from the last _fleet_block(); _enforce_byte_cap reads
        # it. Declared here so it always exists (no implicit _fleet_block-before-cap order
        # dependency / AttributeError if _enforce_byte_cap is ever called directly).
        self._pending_fleet_more = 0
        self.redaction_count = 0
        self.rejection_count = 0
        # Single-owner device record (pairing/auth). Token stored as hash only.
        self.devices: dict[str, Device] = {
            device_id: Device(
                device_id=device_id,
                token_hash=_hash_token(device_token),
                bound_collectors={"local"},
            )
        }
        # One-time pairing codes (≤ 10 min TTL): code → (device_id, issued_token, expiry)
        self._pairing_codes: dict[str, tuple[str, str, float]] = {}

    # -- device auth / pairing ------------------------------------------- #
    def verify_device_token(self, device_id: str, token: str) -> str:
        """Return ``ok`` / ``unknown_device`` / ``bad_token``."""
        with self._lock:
            dev = self.devices.get(device_id)
            if dev is None:
                return "unknown_device"
            if _hash_token(token) != dev.token_hash:
                return "bad_token"
            return "ok"

    def issue_pairing_code(self, device_id: str, token: str) -> str:
        """Issue a one-time pairing code for a device (local CLI/UI role)."""
        with self._lock:
            code = _secrets.token_hex(3).upper()  # 6 hex chars, e.g. 7F3A9C
            self.devices.setdefault(
                device_id,
                Device(device_id=device_id, token_hash=_hash_token(token),
                       bound_collectors={"local"}),
            )
            self._pairing_codes[code] = (device_id, token, _now() + 600)
            return code

    def redeem_pairing_code(self, device_id: str, code: str) -> str | None:
        """Exchange a one-time code for the device token; burn the code.
        Returns the token or ``None`` (bad/expired/mismatched code)."""
        with self._lock:
            rec = self._pairing_codes.get(code)
            if rec is None:
                return None
            dev_id, token, expiry = rec
            # Burn on use regardless of outcome.
            del self._pairing_codes[code]
            if dev_id != device_id or _now() > expiry:
                return None
            # Ensure a device record + token hash exist for subsequent frames.
            self.devices[device_id] = Device(
                device_id=device_id,
                token_hash=_hash_token(token),
                bound_collectors={"local"},
            )
            return token

    # -- event ingestion (drives state) ---------------------------------- #
    def apply_event(self, event: dict) -> dict:
        """Sanitize + apply a provider event envelope to materialized state.

        Returns ``{"applied": True, ...}`` or raises ``SanitizationError``.
        Records redaction/rejection counts (counts only, never the value).
        """
        with self._lock:
            try:
                clean = S.sanitize_event(
                    event, aliases=self.aliases, pepper=self.pepper,
                    local_display=self.local_display,
                )
            except S.SanitizationError:
                self.rejection_count += 1
                raise
            r = self._upsert_session(
                clean["provider"], clean["payload"],
                clean.get("provider_session_id", ""), event.get("started_at"),
            )
            self.redaction_count += 1
            return r

    def apply_validated_event(self, event: dict) -> dict:
        """Relay-mode CLOUD path (BUILD-SPEC I1, docs/devlog/16): VALIDATE an already-sanitized
        event — do NOT re-run the transforms — then apply. The collector is the only transformer;
        the cloud's independent second gate strictly validates the sanitized OUTPUT shape
        (``validate.py``: key allowlist + forbidden scan + enum membership + neutral-alias shape),
        mirrored byte-for-decision by the TS Worker/DO. Rejects (never coerces) a non-canonical
        event. Returns ``{"applied": True, ...}`` or raises ``SanitizationError``."""
        from . import validate as Vd

        with self._lock:
            try:
                clean = Vd.validate_sanitized_event(event)
            except S.SanitizationError:
                self.rejection_count += 1
                raise
            r = self._upsert_session(
                S.normalize_provider(clean.get("provider", "")),
                clean.get("payload") or {},
                clean.get("provider_session_id", ""), event.get("started_at"),
            )
            self.redaction_count += 1
            return r

    def _upsert_session(self, provider: str, payload: dict, psid: str, started_at_raw) -> dict:
        """Build + upsert a Session from an ALREADY-CLEAN payload — shared by the sanitize path
        (local / collector first gate) and the validate path (relay cloud gate), so the two can
        never drift in how a clean event becomes a Session. Caller must hold ``self._lock``."""
        p = payload
        now = _now()
        sess = Session(
            provider=provider,
            account_alias=p.get("account_alias", "main"),
            project_alias=p.get("project_alias", "—"),
            status=p.get("status", "UNKNOWN"),
            task_label=p.get("task_label", "unknown"),
            model=p.get("model", "unknown"),
            session_id=psid or "",
            started_at=float(started_at_raw or now),
            updated_at=now,
            needs_attention=bool(p.get("needs_attention", False)),
            error_label=p.get("error_label"),
            display_title=p.get("display_title"),
        )
        self.last_collector_heartbeat = now
        existing = self.sessions.get(sess.key())
        if existing is not None:
            # Late events must not resurrect / regress; keep started_at.
            sess.started_at = existing.started_at
            sess.pinned = existing.pinned
            # The title rides on SessionStart/UserPromptSubmit but NOT tool events, so a later
            # tool event would otherwise blank it — preserve the known title.
            if sess.display_title is None:
                sess.display_title = existing.display_title
        self.sessions[sess.key()] = sess
        return {"applied": True, "status": sess.status, "scene_key": sess.key()}

    def collector_heartbeat(self) -> None:
        with self._lock:
            self.last_collector_heartbeat = _now()

    # Quota windows the device renders (device_frame_api.md → Frame Schema v1).
    _QUOTA_WINDOW_TYPES = frozenset({"5h", "week"})

    def set_quota(
        self,
        provider: str,
        account_alias: str,
        window_type: str,
        used_ratio: float,
        confidence: str = "unknown",
        is_estimated: bool = True,
        plan: str = "",
        reset_at: int | None = None,
    ) -> None:
        """The SINGLE quota sink (DRY/SOLID chokepoint, 2026-06-03 hardening).

        ``account_alias`` + ``provider`` are written STRAIGHT into the materialized frame
        (``frame.quota[].account``) served to the device, so this sink VALIDATES + REJECTS
        (never coerces / clamps) a non-canonical value — BOTH ``/admin/quota`` and the relay
        path funnel through here, so neither can put ``/Users/.../secret`` or a plan tier on the
        device. The relay path additionally pre-validates via ``validate.validate_quota_event``
        (the cloud's independent gate), but THIS is the last-line backstop both paths share.

        Reject (raise ``S.SanitizationError``):
          * ``account_alias`` not positively neutral OR carrying a forbidden pattern,
          * ``provider`` not in :data:`sanitize.PROVIDER_ENUM`,
          * ``window_type`` not in ``{"5h", "week"}``,
          * ``used_ratio`` a bool / non-numeric / non-finite / outside ``0..1``,
          * ``confidence`` (when given) not in :data:`sanitize.CONFIDENCE_ENUM`.
        """
        import math

        # account_alias: forbidden-pattern clean AND positively neutral (no /Users/, /tmp/, ~,
        # plan tier, path/email/key smuggled in). Never coerce — a non-neutral value rejects.
        account_alias = str(account_alias)
        S.assert_clean(account_alias)
        if not S.looks_like_neutral_alias(account_alias):
            raise S.SanitizationError("alias_shape:account_alias")

        # provider enum (raises provider_not_in_enum).
        provider = S.normalize_provider(provider)

        # window_type enum.
        window_type = str(window_type)
        if window_type not in self._QUOTA_WINDOW_TYPES:
            raise S.SanitizationError("enum:window_type")

        # used_ratio: a finite float in [0, 1]. Reject bool BEFORE float() — float(True)==1.0
        # would otherwise silently coerce a boolean into a ratio (TS rejects bools → a parity
        # divergence). NaN / inf / out-of-range reject (never clamp — a raw out-of-range value
        # means a buggy/hostile collector).
        if isinstance(used_ratio, bool):
            raise S.SanitizationError("quota_used_ratio_not_float")
        try:
            ratio = float(used_ratio)
        except (TypeError, ValueError):
            raise S.SanitizationError("quota_used_ratio_not_float")
        if not math.isfinite(ratio) or ratio < 0.0 or ratio > 1.0:
            raise S.SanitizationError("quota_used_ratio_out_of_range")

        # confidence enum-if-present (normalize_confidence maps unknown → "unknown"; but a
        # caller-supplied non-enum string is a signal of a bad event — reject rather than
        # silently downgrade to "unknown", matching validate_quota_event's strict enum gate).
        conf = str(confidence).strip().lower()
        if conf not in S.CONFIDENCE_ENUM:
            raise S.SanitizationError("enum:confidence")

        # plan (optional display metadata): keep a recognized tier lowercased; drop anything else
        # (forgiving — never reject the quota over a plan string; this is the one field a plan tier
        # is intentionally allowed, separate from the alias neutrality gate above).
        plan_clean = ""
        if plan:
            pv = str(plan).strip().lower()
            if pv in S._PLAN_TIERS or pv in ("free", "unknown"):
                plan_clean = pv
        # reset_at (optional): finite epoch seconds > 0; else None (reject bool BEFORE float()).
        reset_clean = None
        if reset_at is not None and not isinstance(reset_at, bool):
            try:
                rn = float(reset_at)
            except (TypeError, ValueError):
                rn = None
            if rn is not None and math.isfinite(rn) and rn > 0:
                reset_clean = int(rn)

        with self._lock:
            q = QuotaWindow(
                provider=provider,
                account_alias=account_alias,
                window_type=window_type,
                used_ratio=ratio,
                confidence=conf,
                is_estimated=bool(is_estimated),
                plan=plan_clean,
                reset_at=reset_clean,
                updated_at=_now(),
            )
            self.quota[q.key()] = q

    def pin(self, session_key: tuple, pinned: bool = True) -> None:
        with self._lock:
            s = self.sessions.get(session_key)
            if s:
                s.pinned = pinned

    def reset(self) -> None:
        with self._lock:
            self.sessions.clear()
            self.quota.clear()
            self.last_collector_heartbeat = _now()

    # -- liveness ------------------------------------------------------- #
    def _effective_status(self, s: Session, now: float) -> str:
        """Apply TTL liveness so a dead ACTIVE session can't render as active.

        A cleanly-finished / idle session (DONE / IDLE) does NOT decay: it stays
        DONE / IDLE so the scene selector sleeps it (calm) rather than flipping to
        an alarming OFFLINE while the user reads or steps away between turns (each
        interactive turn ends with Stop -> DONE). Only an ACTIVE session that
        abruptly stops receiving events ages to STALE / OFFLINE — a real "the agent
        vanished mid-work" signal — and a dead COLLECTOR is caught separately by
        the heartbeat check in _select_scene."""
        if s.status in ("DONE", "IDLE"):
            return s.status
        age = now - s.updated_at
        if age > OFFLINE_AFTER_S:
            return "OFFLINE"
        if age > STALE_AFTER_S:
            return "STALE"
        return s.status

    # -- priority ------------------------------------------------------- #
    def _score(self, s: Session, eff_status: str, now: float) -> int:
        score = BASE_SCORE.get(eff_status, 0)
        # Low-quota modifier: if this account is in quota danger.
        if self._account_low_quota(s.provider, s.account_alias):
            score += MODIFIER_LOW_QUOTA
        if s.pinned:
            score += MODIFIER_PINNED
        if (now - s.updated_at) > 600:
            score += MODIFIER_STALE_10MIN
        return score

    def _account_low_quota(self, provider: str, account_alias: str) -> bool:
        for q in self.quota.values():
            if q.provider == provider and q.account_alias == account_alias:
                if q.used_ratio >= LOW_QUOTA_MODIFIER_RATIO:
                    return True
        return False

    def _ordered_sessions(self, now: float) -> list[tuple[Session, str, int]]:
        rows: list[tuple[Session, str, int]] = []
        for s in self.sessions.values():
            eff = self._effective_status(s, now)
            score = self._score(s, eff, now)
            rows.append((s, eff, score))
        # Highest score first; tie-break on most-recently-updated (stable focus).
        rows.sort(key=lambda r: (r[2], r[0].updated_at), reverse=True)
        return rows

    # -- frame generation ----------------------------------------------- #
    def build_frame(self, device_id: str, schema_version: int = FRAME_SCHEMA_VERSION, brand: str = "") -> dict:
        """Build the compact device frame for ``device_id`` (scene selection +
        priority + caps + 2 KB trim). Caller must have already authed the device.

        ``brand`` (optional, configurable via env BRAND_NAME — never hardcoded, I3) is shown by
        readers as the project-agnostic title; absent when unset so readers fall back."""
        with self._lock:
            now = _now()
            # Defensive: a non-int schema_version must surface as a SanitizationError
            # (caught + mapped to the contract error envelope), never a raw
            # ValueError that the caller could 500 on.
            try:
                requested = int(schema_version)
            except (TypeError, ValueError) as exc:
                raise S.SanitizationError("bad_schema_version") from exc
            v = min(FRAME_SCHEMA_VERSION, max(1, requested))

            ordered = self._ordered_sessions(now)
            quotas = self._top_quota(now)

            scene, top, accent, headline = self._select_scene(ordered, quotas, now)

            primary = self._primary_block(top, now) if top is not None else _empty_primary()
            fleet = self._fleet_block(ordered, now)
            quota_block = self._quota_block(quotas)

            frame = {
                "v": v,
                "device_id": device_id,
                "scene": scene,
                "headline": headline,
                "primary": primary,
                "fleet": fleet,
                "quota": quota_block,
                "accent": accent,
                "ttl": FRAME_TTL,
                "seq": 0,            # filled after signature compare
                "server_time": int(now),
            }
            if brand:
                frame["brand"] = brand

            # Sequence increases only when rendered content/scene changes.
            signature = _frame_signature(frame)
            if signature != self._last_signature:
                self._seq += 1
                self._last_signature = signature
            frame["seq"] = self._seq

            # Enforce the 2 KB hard cap (trim before send).
            frame = self._enforce_byte_cap(frame)
            return frame

    def _select_scene(
        self,
        ordered: list[tuple[Session, str, int]],
        quotas: list[AccountQuota],
        now: float,
    ) -> tuple[str, Session | None, str, str]:
        """Decide scene + accent + headline + focus session.

        Precedence (display_spec.md + cloud_contract.md → Frame Generation Rules):
          offline (collector dead) → alert (waiting/error/quota danger)
          → stale → sleep (all idle) → focus/fleet/quota rotation.

        The alert interrupt is **unconditional** (cloud_contract.md → Frame
        Generation Rules: "Alert scene interrupts normal rotation for
        waiting/error/quota danger/offline"). It is detected by scanning ALL
        sessions + quota windows, NOT just the top-scored session — a priority
        modifier (low-quota +30, pinned +50) on a CODING session must never
        outscore and thereby SUPPRESS a WAITING/ERROR alert elsewhere in the
        fleet. Quota danger likewise interrupts even with zero live sessions.
        """
        # Collector heartbeat lost → whole fleet offline.
        if (now - self.last_collector_heartbeat) > COLLECTOR_HEARTBEAT_STALE_S and ordered:
            return ("offline", None, STATUS_ACCENT["OFFLINE"], SCENE_HEADLINE["offline"])

        # Quota danger interrupts regardless of session presence (so a 95% burn
        # with no live session still raises the alert, not a silent sleep).
        quota_danger = any(q.risk >= QUOTA_DANGER_RATIO for q in quotas)

        # Alert interrupts: scan EVERY session for WAITING / ERROR (not ordered[0]).
        # The alert's focus session is the highest-priority WAITING/ERROR session.
        waiting = [(s, e, sc) for (s, e, sc) in ordered if e == "WAITING"]
        errors = [(s, e, sc) for (s, e, sc) in ordered if e == "ERROR"]
        if waiting:
            top_w = max(waiting, key=lambda r: (r[2], r[0].updated_at))[0]
            return ("alert", top_w, STATUS_ACCENT["WAITING"], SCENE_HEADLINE["alert"])
        if errors:
            top_e = max(errors, key=lambda r: (r[2], r[0].updated_at))[0]
            return ("alert", top_e, STATUS_ACCENT["ERROR"], SCENE_HEADLINE["alert"])
        if quota_danger:
            # Quota-danger alert: focus the top session if any, else None.
            focus = ordered[0][0] if ordered else None
            return ("alert", focus, "red", SCENE_HEADLINE["alert"])

        if not ordered:
            # Nothing known and no quota danger → sleep ambient (no activity).
            return ("sleep", None, "muted", SCENE_HEADLINE["sleep"])

        # The collector is ALIVE here (the heartbeat check at the top didn't fire).
        # "offline" means the COLLECTOR/daemon is down — full stop. Aged-out
        # sessions must therefore NEVER paint the orb offline; with a live collector
        # they are simply a calm idle. Partition into LIVE sessions (still fresh) and
        # the actively-working subset.
        live = [(s, e, sc) for (s, e, sc) in ordered if e not in ("OFFLINE", "STALE")]
        active = [(s, e, sc) for (s, e, sc) in ordered if _is_active(e)]

        # Several agents working at once → a STABLE fleet overview ("AGENTS",
        # grouped by project) instead of a single focus that flickers between
        # sessions the user can't tell apart. A lone worker keeps the focus scene.
        if len(active) >= 2:
            top = active[0][0]
            return ("fleet", top, STATUS_ACCENT.get(active[0][1], "blue"), SCENE_HEADLINE["fleet"])
        if active:
            s, e, _ = active[0]
            return ("focus", s, STATUS_ACCENT.get(e, "blue"), SCENE_HEADLINE["focus"])

        # Nobody actively working. Live-but-idle/done → calm sleep.
        if live:
            return ("sleep", live[0][0], "muted", SCENE_HEADLINE["sleep"])

        # No live sessions, collector alive. Recently-quiet → mild "stale"; long
        # gone → sleep. Neither is ever "offline" (reserved for a dead collector).
        top, top_eff, _top_score = ordered[0]
        if top_eff == "STALE":
            return ("stale", top, STATUS_ACCENT["STALE"], SCENE_HEADLINE["stale"])
        return ("sleep", top, "muted", SCENE_HEADLINE["sleep"])

    def _primary_block(self, s: Session, now: float) -> dict:
        eff = self._effective_status(s, now)
        return {
            "provider": PROVIDER_DISPLAY.get(s.provider, s.provider.title()),
            "account": s.account_alias,
            "status": eff,
            "project": _display_label(s),
            "task": s.task_label,
        }

    def _on_roster(self, s: "Session", eff: str, now: float) -> bool:
        """A session is on the fleet ROSTER if it is working now (active) OR it finished /
        went idle *recently* (DONE/IDLE within ``ROSTER_TTL_S``). This is what keeps a
        just-completed session visible for a while instead of vanishing the instant it
        stops (Boss 2026-06-09). Truly-gone sessions (OFFLINE/STALE, or DONE/IDLE older
        than the roster window) drop off."""
        if _is_active(eff):
            return True
        if eff in ("DONE", "IDLE"):
            return (now - s.updated_at) <= ROSTER_TTL_S
        return False

    def _fleet_block(self, ordered: list[tuple[Session, str, int]], now: float) -> list[dict]:
        """Aggregate **roster** sessions per project into fleet rows; ≤ 5,
        truncate lowest priority, overflow implied by ``fleet_more`` if present.

        The frame's ``fleet`` is a list of ``{provider, count, status}`` rows
        (device_frame_api.md schema example).

        Roster semantics (Boss 2026-06-09 "show all currently-running session names"):
        a row is kept when the session is on the roster (``_on_roster`` — working now,
        or finished/idle within ``ROSTER_TTL_S``), so a session COMPLETING lingers on
        the list briefly instead of disappearing the instant it stops. ``count`` is the
        number of roster sessions in the group; the row status is the highest-priority
        status among them (so a group with one CODING + two DONE shows CODING). A group
        with no roster sessions drops off entirely.

        Label (R3/TASK-011): ``provider`` is the CLEAN project label with NO baked
        ``xN`` suffix — the count rides in the structured ``count`` field and the
        device renders it as a separate badge. (Baking ``xN`` into the string both
        polluted the 16-byte device buffer for long names and double-printed the
        count in the simulator.)"""
        # Group by display label = session title when named, else project (R4/TASK-012):
        # named sessions surface as their own row, unnamed ones aggregate by project. For an
        # owner running many agents the useful axis is "which sessions, how many, doing what".
        groups: dict[str, dict] = {}
        for s, eff, score in ordered:
            if not self._on_roster(s, eff, now):
                continue
            key = _display_label(s)
            g = groups.get(key)
            if g is None:
                groups[key] = {"project": key, "status": eff, "count": 1, "score": score}
            else:
                g["count"] += 1
                if score > g["score"]:
                    g["score"], g["status"] = score, eff
        # Cap at 5 — the device renders exactly 5 fleet rows, so the WIRE cap equals the
        # render cap (no row is ever transmitted-but-invisible, and the "5" lives in one
        # place rather than drifting against a separate server "6").
        rows = sorted(groups.values(), key=lambda r: r["score"], reverse=True)
        capped = rows[:FLEET_MAX_ROWS]
        overflow = sum(r["count"] for r in rows[FLEET_MAX_ROWS:])
        out = [
            {"provider": r["project"], "count": r["count"], "status": r["status"]}
            for r in capped
        ]
        # Surface the overflow as the top-level ``fleet_more`` count (a documented
        # optional v1 frame key — device_frame_api.md → Array Caps + Frame Schema).
        # build_frame()/_enforce_byte_cap() attach it from this pending value.
        self._pending_fleet_more = overflow if overflow else 0
        return out

    def _top_quota(self, now: float) -> list[AccountQuota]:
        """Merge the per-window ``QuotaWindow`` records into per-(provider,
        account) ``AccountQuota`` entries (both ``w5`` + ``week`` in one entry,
        matching the frame schema), then return the top-2 by ``risk``."""
        merged: dict[tuple[str, str], AccountQuota] = {}
        for q in self.quota.values():
            k = (q.provider, q.account_alias)
            aq = merged.get(k)
            if aq is None:
                # Seed confidence high so the first window lowers it (we keep the
                # most conservative / lowest confidence across the account).
                aq = AccountQuota(
                    provider=q.provider,
                    account_alias=q.account_alias,
                    confidence="high",
                    is_estimated=False,
                )
                merged[k] = aq
            if q.window_type == "5h":
                aq.w5 = q.used_ratio
                aq.w5_reset = q.reset_at
            elif q.window_type == "week":
                aq.week = q.used_ratio
                aq.week_reset = q.reset_at
            if not aq.plan and q.plan:
                aq.plan = q.plan
            # Worst-case across the account's windows: lowest confidence; estimated
            # if ANY window is estimated.
            if S.CONFIDENCE_INT.get(q.confidence, 0) < S.CONFIDENCE_INT.get(aq.confidence, 0):
                aq.confidence = q.confidence
            aq.is_estimated = aq.is_estimated or bool(q.is_estimated)
        rows = sorted(merged.values(), key=lambda a: a.risk, reverse=True)
        return rows[:2]

    def _quota_block(self, quotas: list[AccountQuota]) -> list[dict]:
        """Render the canonical quota shape: one entry per (provider, account)
        with both ``w5`` and ``week`` when present (device_frame_api.md schema).
        A window absent from the runtime data is omitted (compactness), not
        emitted as ``null``."""
        out = []
        for q in quotas:
            entry = {
                "provider": PROVIDER_DISPLAY.get(q.provider, q.provider.title()),
                "account": q.account_alias,
                "confidence": S.CONFIDENCE_INT.get(q.confidence, 0),
                "estimated": bool(q.is_estimated),
            }
            if q.w5 is not None:
                entry["w5"] = round(q.w5, 2)
            if q.week is not None:
                entry["week"] = round(q.week, 2)
            if q.plan:
                entry["plan"] = q.plan
            if q.w5_reset is not None:
                entry["w5_reset"] = q.w5_reset
            if q.week_reset is not None:
                entry["week_reset"] = q.week_reset
            out.append(entry)
        return out

    def _enforce_byte_cap(self, frame: dict) -> dict:
        """Make the serialized JSON body *provably* < 2 KB (hard cap).

        Order of trimming (least → most critical): quota → fleet rows → primary
        string fields. The final clamp of the ``primary`` strings guarantees the
        cap even for a single oversized session (a long project/account alias):
        the device contract says it rejects an oversized frame, so the server
        must never emit one.
        """
        def size(f: dict) -> int:
            return len(json.dumps(f, separators=(",", ":")).encode("utf-8"))

        # Attach fleet_more only if non-zero and it fits.
        more = getattr(self, "_pending_fleet_more", 0)
        if more:
            frame["fleet_more"] = more

        if size(frame) < FRAME_BYTE_CAP:
            return frame
        # Trim quota first (least critical), then fleet rows from the tail.
        while frame.get("quota") and size(frame) >= FRAME_BYTE_CAP:
            frame["quota"].pop()
        while frame.get("fleet") and size(frame) >= FRAME_BYTE_CAP:
            dropped = frame["fleet"].pop()
            frame["fleet_more"] = frame.get("fleet_more", 0) + dropped.get("count", 1)
        # Last-resort: clamp the primary string fields. A single session with an
        # over-long alias still cannot blow the cap. We shrink the longest field
        # repeatedly until the body fits, leaving a truncation marker so the
        # device shows a bounded label rather than the server emitting > 2 KB.
        primary = frame.get("primary")
        if isinstance(primary, dict):
            clampable = ("project", "account", "task", "status", "provider")
            while size(frame) >= FRAME_BYTE_CAP:
                # Pick the currently-longest clampable string field.
                target = max(
                    clampable,
                    key=lambda k: len(str(primary.get(k, ""))),
                )
                cur = str(primary.get(target, ""))
                if len(cur) <= 1:
                    break  # nothing left to shave on the longest field
                # Halve toward a floor; keep a marker so it stays a valid label.
                keep = max(1, len(cur) // 2)
                primary[target] = (cur[:keep] + "…") if keep < len(cur) else cur
        return frame


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #
def _empty_primary() -> dict:
    return {
        "provider": "Manual",
        "account": "main",
        "status": "IDLE",
        "project": "—",
        "task": "idle",
    }


def _frame_signature(frame: dict) -> str:
    """Content signature for seq-increment: everything that affects rendering,
    excluding the volatile ``server_time``/``seq``."""
    import hashlib

    view = {k: v for k, v in frame.items() if k not in ("server_time", "seq")}
    return hashlib.sha256(json.dumps(view, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _gen_pepper() -> bytes:
    """Per-process pepper for the local server when no collector keyring pepper
    is supplied. Stable for the process lifetime so HMAC labels are consistent
    within a run; persisted in env if present (AGENTLAMP_PEPPER_HEX)."""
    env = os.environ.get("AGENTLAMP_PEPPER_HEX")
    if env:
        try:
            return bytes.fromhex(env)
        except ValueError:
            pass
    return _secrets.token_bytes(32)
