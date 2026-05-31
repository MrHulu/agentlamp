"""Frame schema + 2 KB byte-cap + priority/scene + HTTP API tests.

Contract: docs/api/device_frame_api.md, docs/cloud/cloud_contract.md.
"""
from __future__ import annotations

import json

import pytest

from agentlamp_server import sanitize as S
from agentlamp_server.state import (
    FRAME_BYTE_CAP,
    FRAME_SCHEMA_VERSION,
    FrameState,
    STATUS_ACCENT,
)
from .conftest import TEST_PEPPER

SCENE_ENUM = {
    "boot", "pairing", "fleet", "focus", "quota",
    "alert", "offline", "stale", "diagnostics", "sleep",
}
STATUS_ENUM = set(S.STATUS_ENUM)
ACCENT_ENUM = {"blue", "cyan", "purple", "yellow", "green", "red", "white", "muted"}
REQUIRED_KEYS = {
    "v", "device_id", "scene", "headline", "primary",
    "fleet", "quota", "accent", "ttl", "seq", "server_time",
}
PRIMARY_KEYS = {"provider", "account", "status", "project", "task"}


def _state() -> FrameState:
    return FrameState(pepper=TEST_PEPPER, device_token="dev-local-token", device_id="orb-01")


def _inject(st: FrameState, **kw) -> None:
    payload = {
        "status": kw.get("status", "CODING"),
        "task_label": kw.get("task", "implementing"),
        "project_alias": kw.get("project", "project-a"),
        "account_alias": kw.get("account", "main"),
    }
    if "error_label" in kw:
        payload["error_label"] = kw["error_label"]
    ev = {
        "schema_version": 1,
        "provider": kw.get("provider", "claude"),
        "provider_event_name": "manual.inject",
        "provider_session_id": f"hmac:{kw.get('provider','claude')}-{kw.get('account','main')}-{kw.get('project','project-a')}",
        "event_time": 1716900398,
        "payload": payload,
    }
    st.apply_event(ev)
    st.collector_heartbeat()


# --------------------------------------------------------------------------- #
# Frame schema v1 — exact key set + enum membership + types.
# --------------------------------------------------------------------------- #
def test_frame_schema_v1_shape():
    st = _state()
    _inject(st, provider="claude", account="work", status="CODING", project="project-a", task="implementing")
    frame = st.build_frame("orb-01")

    # Exact top-level key set (schema v1). A single session has no fleet overflow,
    # so `fleet_more` must NOT be present — the key set is EXACTLY REQUIRED_KEYS.
    # (No `- {"fleet_more"}` escape hatch: exactness is enforced.)
    assert set(frame.keys()) == REQUIRED_KEYS

    assert frame["v"] == 1
    assert frame["device_id"] == "orb-01"
    assert frame["scene"] in SCENE_ENUM
    assert isinstance(frame["headline"], str) and frame["headline"]
    assert frame["accent"] in ACCENT_ENUM
    assert isinstance(frame["ttl"], int) and frame["ttl"] > 0
    assert isinstance(frame["seq"], int)
    assert isinstance(frame["server_time"], int)

    # primary block.
    assert set(frame["primary"].keys()) == PRIMARY_KEYS
    assert frame["primary"]["status"] in STATUS_ENUM
    # provider is the Title-case display label.
    assert frame["primary"]["provider"] == "Claude"
    # account / project carry the lowercase sanitized alias verbatim.
    assert frame["primary"]["account"] == "work"
    assert frame["primary"]["project"] == "project-a"

    # fleet entries shape.
    for row in frame["fleet"]:
        assert set(row.keys()) == {"provider", "count", "status"}
        assert row["status"] in STATUS_ENUM
        assert isinstance(row["count"], int)

    # quota entries shape (confidence is the integer mapping).
    for q in frame["quota"]:
        assert q["confidence"] in (0, 1, 2, 3)


def test_frame_serializes_to_json():
    st = _state()
    _inject(st)
    frame = st.build_frame("orb-01")
    # Round-trips cleanly (no non-serializable values).
    assert json.loads(json.dumps(frame)) == frame


def test_schema_negotiation_min():
    st = _state()
    _inject(st)
    # Device requests v5; server supports v1 → min == 1.
    assert st.build_frame("orb-01", schema_version=5)["v"] == 1
    # Device requests v1 → 1.
    assert st.build_frame("orb-01", schema_version=1)["v"] == 1


# --------------------------------------------------------------------------- #
# 2 KB hard cap — even with overflowing fleet + quota, body < 2 KB.
# --------------------------------------------------------------------------- #
def test_frame_under_2kb_normal():
    st = _state()
    _inject(st, provider="claude", account="work", status="WAITING")
    frame = st.build_frame("orb-01")
    body = json.dumps(frame, separators=(",", ":")).encode()
    assert len(body) < FRAME_BYTE_CAP, f"{len(body)} >= {FRAME_BYTE_CAP}"


def test_frame_under_2kb_with_overflow():
    st = _state()
    # 12 distinct sessions across providers/accounts/statuses + 4 quota windows.
    statuses = ["CODING", "READING", "TESTING", "THINKING", "WAITING", "ERROR"]
    for i in range(12):
        _inject(
            st,
            provider=("claude" if i % 2 else "codex"),
            account=f"account-{i:02d}",
            status=statuses[i % len(statuses)],
            project=f"project-{i:02d}",
        )
    for i in range(4):
        st.set_quota(
            "codex" if i % 2 else "claude",
            f"account-{i:02d}",
            "5h" if i % 2 else "week",
            0.5 + i * 0.1,
            "medium",
        )
    frame = st.build_frame("orb-01")
    body = json.dumps(frame, separators=(",", ":")).encode()
    assert len(body) < FRAME_BYTE_CAP, f"{len(body)} >= {FRAME_BYTE_CAP}: {frame}"
    # Array caps respected.
    assert len(frame["fleet"]) <= 6
    assert len(frame["quota"]) <= 2


def test_frame_under_2kb_with_oversize_primary_alias():
    """Regression: a single session whose alias fields are pathologically long
    must still yield a frame body < 2 KB. The byte cap clamps the primary string
    fields as a last resort, so the server never emits an oversized frame even if
    state was populated by a path that bypassed the sanitizer's alias gate."""
    from agentlamp_server.state import Session, _now

    st = _state()
    s = Session(
        provider="claude",
        account_alias="x" * 5000,
        project_alias="y" * 5000,
        status="CODING",
        task_label="z" * 5000,
        session_id="hmac:abc",
        started_at=_now(),
        updated_at=_now(),
    )
    st.sessions[s.key()] = s
    st.collector_heartbeat()
    frame = st.build_frame("orb-01")
    body = json.dumps(frame, separators=(",", ":")).encode()
    assert len(body) < FRAME_BYTE_CAP, f"{len(body)} >= {FRAME_BYTE_CAP}"
    # Still a structurally-valid frame (round-trips, keeps required keys).
    assert json.loads(json.dumps(frame)) == frame
    assert set(frame["primary"].keys()) == PRIMARY_KEYS


def test_fleet_capped_to_6_with_overflow_count():
    st = _state()
    # 8 distinct provider/status groups → > 6 rows → overflow.
    for i in range(8):
        _inject(
            st,
            provider=("claude" if i % 2 else "codex"),
            account=f"account-{i:02d}",
            status=["CODING", "READING", "TESTING", "THINKING", "DONE", "IDLE", "WAITING", "ERROR"][i],
            project=f"project-{i:02d}",
        )
    frame = st.build_frame("orb-01")
    assert len(frame["fleet"]) <= 6
    # Overflow surfaces the documented optional top-level `fleet_more` count, and
    # that is the ONLY key beyond the required set (schema exactness, P1).
    assert "fleet_more" in frame and isinstance(frame["fleet_more"], int) and frame["fleet_more"] > 0
    assert set(frame.keys()) == REQUIRED_KEYS | {"fleet_more"}


def test_quota_capped_to_2_top_risk():
    st = _state()
    _inject(st)
    st.set_quota("codex", "main", "5h", 0.30, "medium")
    st.set_quota("claude", "work", "week", 0.85, "medium")
    st.set_quota("codex", "work", "5h", 0.60, "low")
    frame = st.build_frame("orb-01")
    assert len(frame["quota"]) == 2
    # Top-2 by risk: 0.85 and 0.60 (the 0.30 is dropped).
    ratios = sorted(q.get("w5", q.get("week")) for q in frame["quota"])
    assert ratios == [0.60, 0.85]


def test_quota_entry_merges_both_windows_per_account():
    """Canonical quota shape (P1): both `w5` and `week` for ONE (provider,
    account) appear in a SINGLE entry, matching device_frame_api.md's schema
    example (not split into two single-window entries)."""
    st = _state()
    _inject(st)
    st.set_quota("codex", "main", "5h", 0.72, "medium")
    st.set_quota("codex", "main", "week", 0.41, "medium")
    frame = st.build_frame("orb-01")
    quota = frame["quota"]
    assert len(quota) == 1  # one (provider, account), not two single-window rows
    entry = quota[0]
    assert entry["provider"] == "Codex"
    assert entry["account"] == "main"
    assert entry["w5"] == 0.72
    assert entry["week"] == 0.41
    # Exact key set for a both-windows entry.
    assert set(entry.keys()) == {"provider", "account", "w5", "week", "confidence", "estimated"}


def test_quota_entry_omits_absent_window():
    """A (provider, account) with only one window omits the absent key (compact),
    never emits it as null."""
    st = _state()
    _inject(st)
    st.set_quota("claude", "work", "5h", 0.50, "medium")
    frame = st.build_frame("orb-01")
    entry = frame["quota"][0]
    assert "w5" in entry and entry["w5"] == 0.50
    assert "week" not in entry  # omitted, not null
    assert None not in entry.values()


# --------------------------------------------------------------------------- #
# Priority + scene selection (cloud_contract.md → Priority Rules).
# --------------------------------------------------------------------------- #
def test_waiting_interrupts_to_alert():
    st = _state()
    _inject(st, provider="claude", account="work", status="CODING", project="project-a")
    _inject(st, provider="codex", account="main", status="WAITING", project="project-a")
    frame = st.build_frame("orb-01")
    assert frame["scene"] == "alert"
    assert frame["primary"]["status"] == "WAITING"
    assert frame["headline"] == "ACTION REQUIRED"
    assert frame["accent"] == "yellow"  # WAITING → yellow


def test_error_alert_is_red():
    st = _state()
    _inject(st, provider="codex", account="main", status="ERROR", project="project-b", error_label="tool_error")
    frame = st.build_frame("orb-01")
    assert frame["scene"] == "alert"
    assert frame["primary"]["status"] == "ERROR"
    assert frame["accent"] == "red"


def test_coding_focus_is_purple():
    st = _state()
    _inject(st, provider="claude", account="work", status="CODING", project="project-a")
    frame = st.build_frame("orb-01")
    assert frame["scene"] == "focus"
    assert frame["accent"] == "purple"  # coding → purple per mockup


def test_priority_waiting_beats_coding():
    st = _state()
    _inject(st, provider="claude", account="work", status="CODING")
    _inject(st, provider="codex", account="main", status="WAITING")
    frame = st.build_frame("orb-01")
    # WAITING (+100) > CODING (+70) → waiting wins focus.
    assert frame["primary"]["status"] == "WAITING"


def test_quota_danger_forces_alert_red():
    st = _state()
    _inject(st, provider="claude", account="work", status="CODING")
    st.set_quota("claude", "work", "5h", 0.95, "medium")
    frame = st.build_frame("orb-01")
    assert frame["scene"] == "alert"
    assert frame["accent"] == "red"


def test_waiting_alert_not_suppressed_by_low_quota_modifier():
    """Regression (P1): a CODING session with the low-quota +30 modifier must not
    win the top slot and thereby suppress a WAITING alert. The alert interrupt
    scans ALL sessions, not just ordered[0]."""
    st = _state()
    # WAITING (score 100). CODING+lowquota = 70+30 = 100, injected LATER so the
    # recency tie-break would put it at ordered[0] under the old top-only logic.
    _inject(st, provider="codex", account="main", status="WAITING", project="project-a")
    _inject(st, provider="claude", account="work", status="CODING", project="project-b")
    st.set_quota("claude", "work", "5h", 0.85, "medium")  # +30 to the CODING session
    frame = st.build_frame("orb-01")
    assert frame["scene"] == "alert"
    assert frame["primary"]["status"] == "WAITING"


def test_waiting_alert_not_suppressed_by_pinned_modifier():
    """Regression (P1): a pinned CODING session (+50) must not suppress a WAITING
    alert elsewhere in the fleet."""
    st = _state()
    _inject(st, provider="codex", account="main", status="WAITING", project="project-a")
    _inject(st, provider="claude", account="work", status="CODING", project="project-b")
    for k, s in list(st.sessions.items()):
        if s.status == "CODING":
            st.pin(k, True)
    frame = st.build_frame("orb-01")
    assert frame["scene"] == "alert"
    assert frame["primary"]["status"] == "WAITING"


def test_error_alert_not_suppressed_by_pinned_coding():
    """Regression (P1): a pinned CODING session must not suppress an ERROR alert."""
    st = _state()
    _inject(st, provider="codex", account="main", status="ERROR", project="project-a", error_label="tool_error")
    _inject(st, provider="claude", account="work", status="CODING", project="project-b")
    for k, s in list(st.sessions.items()):
        if s.status == "CODING":
            st.pin(k, True)
    frame = st.build_frame("orb-01")
    assert frame["scene"] == "alert"
    assert frame["primary"]["status"] == "ERROR"


def test_quota_danger_alert_with_no_sessions():
    """Regression (P1): quota danger (95% burn) with ZERO sessions must still
    raise the alert, not fall through to sleep."""
    st = _state()
    st.collector_heartbeat()
    st.set_quota("claude", "work", "5h", 0.95, "medium")
    frame = st.build_frame("orb-01")
    assert frame["scene"] == "alert"
    assert frame["accent"] == "red"


def test_all_idle_is_sleep():
    st = _state()
    _inject(st, provider="claude", account="work", status="IDLE")
    _inject(st, provider="codex", account="main", status="DONE")
    frame = st.build_frame("orb-01")
    assert frame["scene"] == "sleep"


def test_empty_state_is_sleep():
    st = _state()
    st.collector_heartbeat()
    frame = st.build_frame("orb-01")
    assert frame["scene"] == "sleep"


# --------------------------------------------------------------------------- #
# Liveness: STALE 120s / OFFLINE 600s.
# --------------------------------------------------------------------------- #
def test_stale_after_120s(monkeypatch):
    import agentlamp_server.state as state_mod

    st = _state()
    _inject(st, provider="claude", account="work", status="CODING", project="project-a")
    # Advance time +130s past the session's updated_at; collector heartbeat also
    # ages, but stays under 90s vs frame time so we only test session staleness.
    base = state_mod._now()
    monkeypatch.setattr(state_mod, "_now", lambda: base + 130)
    # Re-touch the collector heartbeat at the new "now" so the fleet isn't offline.
    st.last_collector_heartbeat = base + 130
    frame = st.build_frame("orb-01")
    # Session is now STALE.
    assert frame["scene"] == "stale"
    assert frame["accent"] == "white"


def test_offline_after_600s(monkeypatch):
    import agentlamp_server.state as state_mod

    st = _state()
    _inject(st, provider="claude", account="work", status="CODING")
    base = state_mod._now()
    monkeypatch.setattr(state_mod, "_now", lambda: base + 700)
    st.last_collector_heartbeat = base + 700  # collector alive, session dead
    frame = st.build_frame("orb-01")
    assert frame["scene"] == "offline"
    assert frame["accent"] == "muted"


def test_collector_heartbeat_lost_is_offline(monkeypatch):
    import agentlamp_server.state as state_mod

    st = _state()
    _inject(st, provider="claude", account="work", status="CODING")
    base = state_mod._now()
    monkeypatch.setattr(state_mod, "_now", lambda: base + 100)  # > 90s heartbeat
    frame = st.build_frame("orb-01")
    assert frame["scene"] == "offline"


# --------------------------------------------------------------------------- #
# seq increments only on content change.
# --------------------------------------------------------------------------- #
def test_seq_stable_when_content_unchanged():
    st = _state()
    _inject(st, status="CODING")
    f1 = st.build_frame("orb-01")
    f2 = st.build_frame("orb-01")
    assert f1["seq"] == f2["seq"]  # same content → same seq


def test_seq_increments_on_scene_change():
    st = _state()
    _inject(st, provider="claude", account="work", status="CODING")
    s1 = st.build_frame("orb-01")["seq"]
    _inject(st, provider="codex", account="main", status="WAITING")
    s2 = st.build_frame("orb-01")["seq"]
    assert s2 > s1
