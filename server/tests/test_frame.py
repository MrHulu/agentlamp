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


def test_fleet_capped_to_5_with_overflow_count():
    st = _state()
    # 8 distinct ACTIVE projects (all working) → > 5 rows → overflow. Use only active
    # statuses (no DONE/IDLE, which now drop off; no WAITING/ERROR, which would flip the
    # scene to alert) so this isolates the fleet cap behaviour.
    statuses = ["CODING", "READING", "TESTING", "THINKING", "CODING", "READING", "TESTING", "THINKING"]
    for i in range(8):
        _inject(
            st,
            provider=("claude" if i % 2 else "codex"),
            account=f"account-{i:02d}",
            status=statuses[i],
            project=f"project-{i:02d}",
        )
    frame = st.build_frame("orb-01")
    # The device renders 5 rows, so the wire cap is 5; the other 3 active agents fold
    # into fleet_more (exact value — guards against an over/under-count regression).
    assert len(frame["fleet"]) == 5
    assert "fleet_more" in frame and frame["fleet_more"] == 3
    # `fleet_more` is the ONLY key beyond the required set (schema exactness, P1).
    assert set(frame.keys()) == REQUIRED_KEYS | {"fleet_more"}


def test_fleet_label_not_truncated_server_side():
    """R3/TASK-011: the server never clamps a fleet row's project label (only primary
    string fields get clamped under the 2 KB cap). A long-but-valid label must reach the
    device verbatim so the firmware can shrink/ellipsize it — the device buffer is sized
    for ALIAS_MAX_LEN (40), so a server-side cut would silently drop characters first."""
    st = _state()
    long_label = "a" * 40  # ALIAS_MAX_LEN — longest a label can legitimately be
    _inject_sid(st, "hmac:long", project=long_label, status="CODING")
    _inject_sid(st, "hmac:other", project="other", status="CODING")  # ≥2 active → fleet
    rows = {r["provider"]: r for r in st.build_frame("orb-01")["fleet"]}
    assert long_label in rows and len(rows[long_label]["provider"]) == 40


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


def test_aged_active_session_sleeps_when_collector_alive(monkeypatch):
    """THOROUGH offline fix: an ACTIVE session that goes silent for >600s must NOT
    paint the orb 'offline' while the collector is alive — 'offline' is reserved
    for a DEAD collector. A silent-but-alive system sleeps (calm)."""
    import agentlamp_server.state as state_mod

    st = _state()
    _inject(st, provider="claude", account="work", status="CODING")
    base = state_mod._now()
    monkeypatch.setattr(state_mod, "_now", lambda: base + 700)
    st.last_collector_heartbeat = base + 700  # collector ALIVE
    frame = st.build_frame("orb-01")
    assert frame["scene"] == "sleep"


def test_offline_only_from_dead_collector(monkeypatch):
    """The ONLY path to the offline scene is a stale collector heartbeat — never
    aged sessions, no matter how many or how old (regression for the Boss's
    'frequently shows offline' report)."""
    import agentlamp_server.state as state_mod

    st = _state()
    _inject(st, provider="claude", account="a", status="CODING")
    _inject(st, provider="codex", account="b", status="TESTING")
    base = state_mod._now()
    monkeypatch.setattr(state_mod, "_now", lambda: base + 700)
    # Collector ALIVE, every session aged 700s → sleep, NOT offline.
    st.last_collector_heartbeat = base + 700
    assert st.build_frame("orb-01")["scene"] == "sleep"
    # Collector DEAD (heartbeat 700s stale) → the one true offline.
    st.last_collector_heartbeat = base
    assert st.build_frame("orb-01")["scene"] == "offline"


def test_collector_heartbeat_lost_is_offline(monkeypatch):
    import agentlamp_server.state as state_mod

    st = _state()
    _inject(st, provider="claude", account="work", status="CODING")
    base = state_mod._now()
    monkeypatch.setattr(state_mod, "_now", lambda: base + 100)  # > 90s heartbeat
    frame = st.build_frame("orb-01")
    assert frame["scene"] == "offline"


def test_done_session_sleeps_not_offline_when_aged(monkeypatch):
    """Regression: a cleanly-finished DONE session sitting idle between turns must
    SLEEP, not flip to an alarming OFFLINE while the user reads/steps away (an
    interactive session flapped offline ~10 min after each turn's Stop->DONE).
    Only an ACTIVE session that vanishes mid-work should go offline."""
    import agentlamp_server.state as state_mod

    st = _state()
    _inject(st, provider="claude", account="work", status="DONE")
    base = state_mod._now()
    monkeypatch.setattr(state_mod, "_now", lambda: base + 700)  # well past OFFLINE window
    st.last_collector_heartbeat = base + 700  # collector alive (daemon heart-beating)
    frame = st.build_frame("orb-01")
    assert frame["scene"] == "sleep"


def test_idle_session_sleeps_not_offline_when_aged(monkeypatch):
    import agentlamp_server.state as state_mod

    st = _state()
    _inject(st, provider="claude", account="work", status="IDLE")
    base = state_mod._now()
    monkeypatch.setattr(state_mod, "_now", lambda: base + 700)
    st.last_collector_heartbeat = base + 700
    assert st.build_frame("orb-01")["scene"] == "sleep"


# --------------------------------------------------------------------------- #
# Readable local-display labels + multi-session fleet (Boss 2026: orb unreadable —
# opaque hash + flickering single focus across same-project sessions).
# --------------------------------------------------------------------------- #
def _inject_sid(st, sid, **kw):
    payload = {
        "status": kw.get("status", "CODING"),
        "task_label": "implementing",
        "project_alias": kw.get("project", "project-a"),
        "account_alias": "main",
    }
    if "session_title" in kw:
        payload["session_title"] = kw["session_title"]
    ev = {
        "schema_version": 1,
        "provider": kw.get("provider", "claude"),
        "provider_session_id": sid,
        "event_time": 1716900398,
        "payload": payload,
    }
    st.apply_event(ev)
    st.collector_heartbeat()


def test_local_display_keeps_readable_multi_segment_label():
    """Local frame server shows a readable multi-segment folder name verbatim
    (moza-perception-analysis), not an opaque HMAC hash."""
    st = _state()  # local_display defaults ON
    _inject_sid(st, "hmac:s1", project="moza-perception-analysis", status="CODING")
    assert st.build_frame("orb-01")["primary"]["project"] == "moza-perception-analysis"


# --------------------------------------------------------------------------- #
# Per-session titles (R4/TASK-012): a named session (claude --name / /rename →
# session_title) surfaces by its title so same-folder sessions are distinguishable.
# --------------------------------------------------------------------------- #
def test_named_session_title_replaces_project_label():
    st = _state()
    _inject_sid(st, "hmac:n1", project="ai-center", status="CODING", session_title="auth refactor")
    assert st.build_frame("orb-01")["primary"]["project"] == "auth-refactor"


def test_named_sessions_split_same_folder_into_distinct_fleet_rows():
    """Two NAMED sessions in the same folder → two distinct rows (by title); an unnamed
    session in that folder still aggregates under the bare project label."""
    st = _state()
    _inject_sid(st, "hmac:a", project="ai-center", status="CODING", session_title="rag pipeline")
    _inject_sid(st, "hmac:b", project="ai-center", status="TESTING", session_title="fleet fix")
    _inject_sid(st, "hmac:c", project="ai-center", status="READING")  # unnamed
    labels = {r["provider"] for r in st.build_frame("orb-01")["fleet"]}
    assert {"rag-pipeline", "fleet-fix", "ai-center"} <= labels


def test_title_preserved_across_events_that_omit_it():
    """Title rides on SessionStart/UserPromptSubmit but NOT tool events — a later tool
    event without a title must not blank the session's known title."""
    st = _state()
    _inject_sid(st, "hmac:p", project="ai-center", status="THINKING", session_title="my task")
    _inject_sid(st, "hmac:p", project="ai-center", status="CODING")  # tool event, no title
    assert st.build_frame("orb-01")["primary"]["project"] == "my-task"


def test_unsafe_title_is_dropped_not_cleaned():
    """A title carrying a path/secret is DROPPED (we never try to 'clean' a leak) → the
    label falls back to the project. The leak must not survive in any form."""
    st = _state()
    _inject_sid(st, "hmac:u", project="ai-center", status="CODING",
                session_title="/Users/hulu/secrets/key.pem")
    label = st.build_frame("orb-01")["primary"]["project"]
    assert label == "ai-center"
    assert "Users" not in label and "secrets" not in label and "key" not in label


def test_title_hmac_collapsed_in_relay_mode():
    """Relay mode never emits a readable title — it HMAC-collapses to title-<hmac>."""
    st = _state()
    st.local_display = False
    _inject_sid(st, "hmac:r", project="proj", status="CODING", session_title="secret plan name")
    label = st.build_frame("orb-01")["primary"]["project"]
    assert label.startswith("title-") and "secret" not in label and "plan" not in label


def test_multiple_active_sessions_show_fleet_not_focus():
    """>= 2 agents working at once → a STABLE 'AGENTS' overview, not a single focus
    that flickers between sessions the user can't tell apart."""
    st = _state()
    _inject_sid(st, "hmac:a", project="ai-center", status="CODING")
    _inject_sid(st, "hmac:b", project="ai-center", status="READING")
    f = st.build_frame("orb-01")
    assert f["scene"] == "fleet" and f["headline"] == "AGENTS"


def test_single_active_session_is_focus():
    st = _state()
    _inject_sid(st, "hmac:a", project="ai-center", status="CODING")
    assert st.build_frame("orb-01")["scene"] == "focus"


def test_fleet_groups_by_project_with_count():
    """Fleet rows group by project. The label is the CLEAN project name (no baked
    'xN') and the count rides in the structured ``count`` field — so a glance maps to
    which project + how many busy agents (the core fix for 5 same-folder sessions),
    without polluting the device's 16-byte label buffer (R3/TASK-011)."""
    st = _state()
    for i in range(5):
        _inject_sid(st, f"hmac:c{i}", project="ai-center", status="CODING")
    _inject_sid(st, "hmac:d", project="agentlamp", status="READING")
    rows = {row["provider"]: row for row in st.build_frame("orb-01")["fleet"]}
    assert "ai-center" in rows and rows["ai-center"]["count"] == 5  # clean label
    assert " x" not in rows["ai-center"]["provider"]                # NO baked suffix
    assert "agentlamp" in rows and rows["agentlamp"]["count"] == 1


def test_fleet_count_includes_recent_done_on_roster():
    """Roster semantics (Boss 2026-06-09): the row count = sessions on the ROSTER —
    working now OR finished/idle within ROSTER_TTL_S — so a just-completed session does
    not vanish. 5 fresh ai-center sessions (3 CODING + 2 DONE) → count 5, and the row
    status is the highest-priority one (CODING)."""
    st = _state()
    for i in range(3):
        _inject_sid(st, f"hmac:a{i}", project="ai-center", status="CODING")
    for i in range(2):
        _inject_sid(st, f"hmac:d{i}", project="ai-center", status="DONE")
    # A second active project so the scene is the fleet overview (≥2 active).
    _inject_sid(st, "hmac:b", project="other", status="CODING")
    rows = {r["provider"]: r for r in st.build_frame("orb-01")["fleet"]}
    assert rows["ai-center"]["count"] == 5   # 2 recent DONE now kept on the roster
    assert rows["ai-center"]["status"] == "CODING"  # top-priority status wins the row


def test_fleet_keeps_recent_idle_or_done_project():
    """Roster semantics: a project where every session just finished/idled stays on the
    fleet briefly (within ROSTER_TTL_S) instead of disappearing the instant it stops —
    the whole point of the roster (Boss: 'session 完成后你就不显示了')."""
    st = _state()
    _inject_sid(st, "hmac:c1", project="active-proj", status="CODING")
    _inject_sid(st, "hmac:c2", project="active-proj", status="TESTING")
    _inject_sid(st, "hmac:d1", project="finished-proj", status="DONE")
    _inject_sid(st, "hmac:d2", project="finished-proj", status="IDLE")
    rows = {r["provider"]: r for r in st.build_frame("orb-01")["fleet"]}
    assert "active-proj" in rows
    assert "finished-proj" in rows          # recently finished/idle → still on the roster
    assert rows["finished-proj"]["count"] == 2
    assert rows["finished-proj"]["status"] in ("DONE", "IDLE")


def test_fleet_drops_session_past_roster_ttl(monkeypatch):
    """The roster is time-bounded: a session whose last event is older than ROSTER_TTL_S
    ages off the fleet (so the list shows *recent* sessions, not an ever-growing history)."""
    import agentlamp_server.state as state_mod

    st = _state()
    base = state_mod._now()
    # A session that finished long ago (its updated_at stays at `base`).
    _inject_sid(st, "hmac:old", project="finished-proj", status="DONE")
    # Jump now past the roster window; re-touch the collector heartbeat so the scene
    # isn't forced offline, then add a session that is working *now* (fresh).
    monkeypatch.setattr(state_mod, "_now", lambda: base + state_mod.ROSTER_TTL_S + 200)
    st.last_collector_heartbeat = base + state_mod.ROSTER_TTL_S + 200
    _inject_sid(st, "hmac:fresh", project="active-proj", status="CODING")
    labels = [r["provider"] for r in st.build_frame("orb-01")["fleet"]]
    assert "active-proj" in labels          # fresh worker on the roster
    assert "finished-proj" not in labels    # finished > ROSTER_TTL_S ago → dropped


def test_waiting_still_interrupts_busy_fleet():
    """A genuine WAITING agent still raises the alert even amid a busy fleet — the
    one actionable signal is never buried."""
    st = _state()
    _inject_sid(st, "hmac:a", project="ai-center", status="CODING")
    _inject_sid(st, "hmac:b", project="ai-center", status="CODING")
    _inject_sid(st, "hmac:c", project="agentlamp", status="WAITING")
    assert st.build_frame("orb-01")["scene"] == "alert"


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
