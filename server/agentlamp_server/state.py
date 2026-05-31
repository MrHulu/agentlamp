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
  * ``fleet`` ≤ 6 (truncate lowest priority; ``fleet_more`` overflow count),
  * ``quota`` ≤ 2 (top-2 risk),
  * provider in ``primary``/``fleet`` is the Title-case display label.
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
STALE_AFTER_S = 120
OFFLINE_AFTER_S = 600
COLLECTOR_HEARTBEAT_STALE_S = 90

# Frame TTL (poll interval is 3-5 s; ttl is the firmware's grace window).
FRAME_TTL = 5
FRAME_SCHEMA_VERSION = 1
FRAME_BYTE_CAP = 2048

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
        self.sessions: dict[tuple, Session] = {}
        self.quota: dict[tuple, QuotaWindow] = {}
        self.last_collector_heartbeat: float = _now()
        self._seq = 0
        self._last_signature: str | None = None
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
                    event, aliases=self.aliases, pepper=self.pepper
                )
            except S.SanitizationError as exc:
                self.rejection_count += 1
                raise
            p = clean["payload"]
            now = _now()
            sess = Session(
                provider=clean["provider"],
                account_alias=p.get("account_alias", "main"),
                project_alias=p.get("project_alias", "—"),
                status=p.get("status", "UNKNOWN"),
                task_label=p.get("task_label", "unknown"),
                model=p.get("model", "unknown"),
                session_id=clean.get("provider_session_id", "") or "",
                started_at=float(event.get("started_at") or now),
                updated_at=now,
                needs_attention=bool(p.get("needs_attention", False)),
                error_label=p.get("error_label"),
            )
            self.last_collector_heartbeat = now
            existing = self.sessions.get(sess.key())
            if existing is not None:
                # Late events must not resurrect / regress; keep started_at.
                sess.started_at = existing.started_at
                sess.pinned = existing.pinned
            self.sessions[sess.key()] = sess
            self.redaction_count += 1
            return {"applied": True, "status": sess.status, "scene_key": sess.key()}

    def collector_heartbeat(self) -> None:
        with self._lock:
            self.last_collector_heartbeat = _now()

    def set_quota(
        self,
        provider: str,
        account_alias: str,
        window_type: str,
        used_ratio: float,
        confidence: str = "unknown",
        is_estimated: bool = True,
    ) -> None:
        with self._lock:
            q = QuotaWindow(
                provider=S.normalize_provider(provider),
                account_alias=account_alias,
                window_type=window_type,
                used_ratio=max(0.0, min(1.0, float(used_ratio))),
                confidence=S.normalize_confidence(confidence),
                is_estimated=bool(is_estimated),
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
        """Apply TTL liveness: a session past STALE/OFFLINE windows is downgraded
        so a dead session can never render as active."""
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
    def build_frame(self, device_id: str, schema_version: int = FRAME_SCHEMA_VERSION) -> dict:
        """Build the compact device frame for ``device_id`` (scene selection +
        priority + caps + 2 KB trim). Caller must have already authed the device."""
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
            fleet = self._fleet_block(ordered)
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

        top, top_eff, _top_score = ordered[0]

        # All sessions effectively offline → offline scene.
        if all(e == "OFFLINE" for _s, e, _sc in ordered):
            return ("offline", None, STATUS_ACCENT["OFFLINE"], SCENE_HEADLINE["offline"])

        # Top session stale → stale scene (show cached focus).
        if top_eff == "STALE":
            return ("stale", top, STATUS_ACCENT["STALE"], SCENE_HEADLINE["stale"])

        # All idle/done → sleep ambient.
        if all(e in ("IDLE", "DONE", "UNKNOWN") for _s, e, _sc in ordered):
            return ("sleep", top, "muted", SCENE_HEADLINE["sleep"])

        # Active highest-priority session → focus.
        return ("focus", top, STATUS_ACCENT.get(top_eff, "blue"), SCENE_HEADLINE["focus"])

    def _primary_block(self, s: Session, now: float) -> dict:
        eff = self._effective_status(s, now)
        return {
            "provider": PROVIDER_DISPLAY.get(s.provider, s.provider.title()),
            "account": s.account_alias,
            "status": eff,
            "project": s.project_alias,
            "task": s.task_label,
        }

    def _fleet_block(self, ordered: list[tuple[Session, str, int]]) -> list[dict]:
        """Aggregate sessions per (provider, status) into fleet rows; ≤ 6,
        truncate lowest priority, overflow implied by ``fleet_more`` if present.

        The frame's ``fleet`` is a list of ``{provider, count, status}`` rows
        (device_frame_api.md schema example)."""
        # Group by (provider display, effective status), summing counts; keep
        # the max score per group to order rows by priority.
        groups: dict[tuple[str, str], dict] = {}
        for s, eff, score in ordered:
            disp = PROVIDER_DISPLAY.get(s.provider, s.provider.title())
            k = (disp, eff)
            g = groups.setdefault(k, {"provider": disp, "status": eff, "count": 0, "score": score})
            g["count"] += 1
            g["score"] = max(g["score"], score)
        rows = sorted(groups.values(), key=lambda r: r["score"], reverse=True)
        capped = rows[:6]
        overflow = sum(r["count"] for r in rows[6:])
        out = [{"provider": r["provider"], "count": r["count"], "status": r["status"]} for r in capped]
        # Surface the overflow as the top-level ``fleet_more`` count (a documented
        # optional v1 frame key — device_frame_api.md → Array Caps + Frame Schema).
        # build_frame()/_enforce_byte_cap() attach it from this pending value.
        self._pending_fleet_more = overflow if overflow else 0  # type: ignore[attr-defined]
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
            elif q.window_type == "week":
                aq.week = q.used_ratio
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
