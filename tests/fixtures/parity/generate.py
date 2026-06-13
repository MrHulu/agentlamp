#!/usr/bin/env python3
"""Generate the cross-language parity corpora from the Python reference impl.

🚨 BUILD-SPEC invariant I2 (docs/devlog/16): these files are the single source of truth.
Both the Python suite (server/tests/test_parity.py) AND the TS vitest (src/cloud/test/
parity.test.ts) assert against them. Regenerate after ANY policy/logic change:

    python3 tests/fixtures/parity/generate.py

Outputs (this dir): policy.json, hmac_vectors.json, sanitize_corpus.json, quota_corpus.json,
frame_vectors.json.
Pure-deterministic: aliases are pre-neutral (no HMAC), so frames are pepper-independent; the
volatile frame fields (server_time, seq) are stripped from the golden.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "server"))

from agentlamp_server import ingest as I  # noqa: E402
from agentlamp_server import sanitize as S  # noqa: E402
from agentlamp_server import validate as V  # noqa: E402
from agentlamp_server.state import FrameState  # noqa: E402

OUT = Path(__file__).resolve().parent


def _write(name: str, obj) -> None:
    (OUT / name).write_text(json.dumps(obj, indent=2) + "\n")
    print(f"  wrote {name}")


# --------------------------------------------------------------------------- #
# 1. policy.json — portable data both languages load (no hand-retyped enums).
# --------------------------------------------------------------------------- #
def gen_policy() -> dict:
    return {
        "policy_version": S.POLICY_VERSION,
        "provider_enum": list(S.PROVIDER_ENUM),
        "model_enum": list(S.MODEL_ENUM),
        "status_enum": list(S.STATUS_ENUM),
        "status_detail_enum": list(S.STATUS_DETAIL_ENUM),
        "tool_category_enum": list(S.TOOL_CATEGORY_ENUM),
        "task_label_enum": list(S.TASK_LABEL_ENUM),
        "error_label_enum": list(S.ERROR_LABEL_ENUM),
        "confidence_enum": list(S.CONFIDENCE_ENUM),
        "validate_envelope_keys": sorted(V.VALIDATE_ENVELOPE_KEYS),
        "validate_payload_keys": sorted(V.VALIDATE_PAYLOAD_KEYS),
        "forbidden_keys": sorted(S._FORBIDDEN_KEYS),
        "plan_tiers": list(S._PLAN_TIERS),
        "alias_max_len": S.ALIAS_MAX_LEN,
        "title_max_len": S.TITLE_MAX_LEN,
        "alias_shape_regex": S._ALIAS_SHAPE_RE.pattern,
        "display_label_regex": S._DISPLAY_LABEL_RE.pattern,
        "display_label_max_len": S.DISPLAY_LABEL_MAX_LEN,
        "model_id_regex": S._MODEL_ID_RE.pattern,
        "model_id_ignorecase": bool(S._MODEL_ID_RE.flags & re.IGNORECASE),
        # provider_session_id shape gate (2026-06-03 hardening). validate.py step 7 requires the
        # stored session KEY to be a canonical opaque id (hmac:<hex> OR a high-entropy token with
        # a digit, no whitespace) so forbidden-pattern-clean free text ("please fix auth now")
        # can't survive as the session key. Emitted as DATA (not hand-retyped) so the TS port
        # mirrors looks_like_session_id byte-for-byte — same I2 contract as alias_shape_regex.
        "session_id_max_len": S.SESSION_ID_MAX_LEN,
        "session_hmac_regex": S._SESSION_HMAC_RE.pattern,
        "session_token_regex": S._SESSION_TOKEN_RE.pattern,
        "session_has_digit_regex": S._HAS_DIGIT_RE.pattern,
        "session_whitespace_regex": S._WHITESPACE_RE.pattern,
        # Each forbidden pattern carries its case-sensitivity so the TS port can rebuild it
        # with the SAME flag (dropping re.IGNORECASE silently under-redacts lowercase
        # `bearer`/`cookie:` leaks — the I1 failure the parity gate exists to forbid).
        "forbidden_patterns": [
            {"pattern": p.pattern, "ignorecase": bool(p.flags & re.IGNORECASE)}
            for p in S._FORBIDDEN_PATTERNS
        ],
    }


# --------------------------------------------------------------------------- #
# 2. hmac_vectors.json — the frozen canonical-string byte spec + HMAC signatures.
# --------------------------------------------------------------------------- #
def gen_hmac() -> list:
    cases = [
        ("k1", "test-collector-secret", "collector-mac-main", 1_780_000_000, "ab" * 16, '{"hello":"world"}'),
        ("kA", "another-secret-0123456789", "laptop-2", 1_780_000_123, "cd" * 16, '{"events":[]}'),
    ]
    out = []
    for kid, secret, cid, ts, nonce, body in cases:
        path = f"/api/v1/collectors/{cid}/events"
        sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        canon = I.canonical_string("POST", path, kid, str(ts), nonce, sha)
        sig = hmac.new(secret.encode("utf-8"), canon.encode("utf-8"), hashlib.sha256).hexdigest()
        out.append({
            "kid": kid, "secret_utf8": secret, "collector_id": cid, "method": "POST",
            "path": path, "timestamp": ts, "nonce": nonce, "body_utf8": body,
            "payload_sha256": sha, "canonical_string": canon, "signature": sig,
        })
    return out


# --------------------------------------------------------------------------- #
# 3. sanitize_corpus.json — validate-only accept/reject decisions.
# --------------------------------------------------------------------------- #
def _env(payload: dict, *, provider="claude", psid="hmac:abc123def456", pen="SessionStart") -> dict:
    return {
        "schema_version": 1, "provider": provider, "provider_event_name": pen,
        "provider_session_id": psid, "event_time": 1_780_000_000,
        "payload": payload, "sanitization": {"policy_version": 1},
    }


def gen_sanitize_corpus() -> list:
    raw_cases = [
        ("accept_clean_coding", _env({"status": "CODING", "task_label": "implementing",
                                      "model": "claude", "project_alias": "project-a", "account_alias": "main"})),
        ("accept_waiting_attention", _env({"status": "WAITING", "task_label": "waiting",
                                           "needs_attention": True, "project_alias": "project-b", "account_alias": "work"})),
        ("accept_error_label", _env({"status": "ERROR", "task_label": "debugging", "error_label": "rate_limit",
                                     "project_alias": "project-7f3a9c", "account_alias": "account-1a2b"})),
        ("accept_hmac_aliases", _env({"status": "READING", "task_label": "reading", "model": "codex",
                                      "project_alias": "project-7f3a9c", "account_alias": "account-7f3a"}, provider="codex")),
        ("accept_display_title", _env({"status": "CODING", "task_label": "implementing",
                                       "project_alias": "project-a", "display_title": "title-9f8e7d"})),
        ("reject_path_in_project", _env({"status": "CODING", "task_label": "implementing",
                                         "project_alias": "/Users/hulu/secret/path"})),
        ("reject_unknown_payload_key", _env({"status": "CODING", "task_label": "implementing", "secrets": "x"})),
        ("reject_forbidden_key_cwd", _env({"status": "CODING", "cwd": "/Users/hulu"})),
        ("reject_plan_tier_account", _env({"status": "CODING", "task_label": "implementing", "account_alias": "Max"})),
        ("reject_real_model_id", _env({"status": "CODING", "task_label": "implementing", "model": "claude-opus-4-20250101"})),
        ("reject_nonenum_status", _env({"status": "coding-hard", "task_label": "implementing"})),
        ("reject_bearer_in_title", _env({"status": "CODING", "task_label": "implementing",
                                         "project_alias": "project-a", "display_title": "Bearer sk-abc123"})),
        ("reject_provider_not_enum", _env({"status": "CODING", "task_label": "implementing"}, provider="gpt5")),
        ("reject_prompt_like_project", _env({"status": "CODING", "task_label": "implementing",
                                             "project_alias": "please refactor the auth module now"})),
        ("reject_unknown_envelope_key", {**_env({"status": "CODING", "task_label": "implementing"}), "extra": "x"}),
        # Case-insensitive forbidden patterns in a free-form envelope field (provider_event_name has
        # no enum/charset gate — only the leaf forbidden-scan guards it). These isolate the
        # re.IGNORECASE flag so a TS port that drops it fails the parity gate instead of silently
        # under-redacting a lowercase `bearer <token>` / `cookie:` leak.
        ("reject_lowercase_bearer_event_name",
         _env({"status": "CODING", "task_label": "implementing", "project_alias": "project-a"}, pen="bearer abc123")),
        ("reject_lowercase_cookie_event_name",
         _env({"status": "CODING", "task_label": "implementing", "project_alias": "project-a"}, pen="cookie: secret")),
        # Generic forbidden-path scan (2026-06-03 hardening): the original scan only caught
        # /Users/ /home/ C:\ ./ ../ — NOT /tmp/, ~, /etc/. These put a real FS path in a
        # leaf-scanned field (provider_event_name has no enum gate; only the leaf forbidden-scan
        # guards it) and lock the new _ABS_POSIX_PATH_RE + _TILDE_HOME_RE patterns into the TS gate.
        ("reject_tmp_path_event_name",
         _env({"status": "CODING", "task_label": "implementing", "project_alias": "project-a"}, pen="/tmp/secret")),
        ("reject_tilde_home_event_name",
         _env({"status": "CODING", "task_label": "implementing", "project_alias": "project-a"}, pen="~/secret")),
        ("reject_etc_passwd_event_name",
         _env({"status": "CODING", "task_label": "implementing", "project_alias": "project-a"}, pen="/etc/passwd")),
        # provider_session_id shape gate (2026-06-03 hardening): it is leaf-scanned but was not
        # shape-gated, so forbidden-pattern-clean free text survived as the stored session KEY.
        # Accept the canonical opaque `hmac:<hex>`; reject free text.
        ("accept_hmac_session_id",
         _env({"status": "CODING", "task_label": "implementing", "project_alias": "project-a"},
              psid="hmac:abc123def456")),
        ("reject_freetext_session_id",
         _env({"status": "CODING", "task_label": "implementing", "project_alias": "project-a"},
              psid="please fix auth now")),
        # Owner display labels (multi-segment kebab) transit the relay verbatim in owner-labels
        # mode — e.g. an auto session title derived from Claude's own transcript ai-title
        # ("fix-orphan-chrome-processes") or a multi-word folder name. The strict 2-segment
        # neutral shape alone used to reject these (alias_shape parity gap, fixed 2026-06-11);
        # the display shape still bans / \ @ . : and uppercase, and is length-capped.
        ("accept_owner_multisegment_title",
         _env({"status": "CODING", "task_label": "implementing", "project_alias": "project-a",
               "display_title": "fix-orphan-chrome-processes"})),
        ("accept_owner_multisegment_project",
         _env({"status": "CODING", "task_label": "implementing",
               "project_alias": "my-cool-side-project"})),
        ("reject_overlong_display_title",
         _env({"status": "CODING", "task_label": "implementing", "project_alias": "project-a",
               "display_title": "a-" * 21 + "end"})),
    ]
    out = []
    for name, event in raw_cases:
        try:
            V.validate_sanitized_event(event)
            out.append({"name": name, "event": event, "expect": "accepted"})
        except S.SanitizationError as exc:
            out.append({"name": name, "event": event, "expect": "rejected", "reason": exc.reason})
    return out


# --------------------------------------------------------------------------- #
# 3b. quota_corpus.json — validate_quota_event accept/reject decisions.
#     CRITICAL (docs/devlog/16 I1): the quota.window ingest branch must pass the SAME
#     default-deny gate as session.* before set_quota writes account_alias/provider into the
#     frame served to the device. Each case is a `quota.window` ingest event (the shape app.py /
#     relay_do.ts hand to validate_quota_event). NaN/"x"/1.5 isolate the non-finite/out-of-range
#     REJECT (the Python float() raise ↔ TS Number.isFinite parity, the previously-divergent path).
# --------------------------------------------------------------------------- #
def _quota_ev(*, provider="claude", account_alias="main", window_type="5h",
              used_ratio=0.5, confidence=None, is_estimated=True) -> dict:
    payload: dict = {"window_type": window_type, "used_ratio": used_ratio,
                     "is_estimated": is_estimated}
    if confidence is not None:
        payload["confidence"] = confidence
    return {"event_id": "q1", "event_type": "quota.window", "provider": provider,
            "account_alias": account_alias, "payload": payload}


def gen_quota_corpus() -> list:
    # NaN is not JSON-representable as a literal, so we model the "non-finite used_ratio" reject
    # cases with the string "x" (Number("x")→NaN / float("x")→raise) AND a JSON-encodable
    # out-of-range 1.5; the literal float('nan') case is asserted CORPUS-INDEPENDENTLY in both
    # test suites (it can't round-trip through json.dumps).
    raw_cases = [
        ("accept_neutral_5h", _quota_ev(account_alias="main", window_type="5h", used_ratio=0.95)),
        ("accept_neutral_week", _quota_ev(account_alias="work", window_type="week", used_ratio=0.3,
                                          confidence="medium")),
        ("accept_hmac_account", _quota_ev(account_alias="account-7f3a", used_ratio=0.0, provider="codex")),
        ("accept_ratio_one", _quota_ev(account_alias="main", used_ratio=1.0)),
        ("reject_path_account", _quota_ev(account_alias="/Users/hulu/secret-project", used_ratio=0.5)),
        # Generic forbidden-path account aliases (2026-06-03 hardening): /tmp/, ~, /etc/ now
        # rejected in this leaf-scanned field (account_alias flows to frame.quota[].account).
        ("reject_tmp_path_account", _quota_ev(account_alias="/tmp/secret", used_ratio=0.5)),
        ("reject_tilde_home_account", _quota_ev(account_alias="~/secret", used_ratio=0.5)),
        ("reject_etc_passwd_account", _quota_ev(account_alias="/etc/passwd", used_ratio=0.5)),
        ("reject_plan_tier_account", _quota_ev(account_alias="Max", used_ratio=0.5)),
        ("reject_nonenum_provider", _quota_ev(provider="gpt5", used_ratio=0.5)),
        ("reject_used_ratio_string", _quota_ev(used_ratio="x")),
        ("reject_used_ratio_out_of_range", _quota_ev(used_ratio=1.5)),
        # Boolean used_ratio (2026-06-03 hardening): Python float(True)==1.0 would silently
        # coerce a bool into a 100%-burn ratio; TS rejects typeof boolean. Reject before float()
        # so both languages agree — this case locks the parity.
        ("reject_used_ratio_bool", _quota_ev(used_ratio=True)),
        ("reject_bad_window_type", _quota_ev(window_type="daily", used_ratio=0.5)),
        ("reject_nonenum_confidence", _quota_ev(used_ratio=0.5, confidence="super")),
    ]
    out = []
    for name, ev in raw_cases:
        try:
            V.validate_quota_event(ev)
            out.append({"name": name, "event": ev, "expect": "accepted"})
        except S.SanitizationError as exc:
            out.append({"name": name, "event": ev, "expect": "rejected", "reason": exc.reason})
    return out


# --------------------------------------------------------------------------- #
# 4. frame_vectors.json — golden frames from state.build_frame (volatile fields stripped).
# --------------------------------------------------------------------------- #
def _ev_session(provider, account, project, status, task, sid, **extra) -> dict:
    payload = {"status": status, "task_label": task, "project_alias": project,
               "account_alias": account, "model": provider}
    payload.update(extra)
    return _env(payload, provider=provider, psid=f"hmac:{sid}")


def gen_frame_vectors() -> list:
    # Each scenario is a list of (kind, event): kind="session" → validate_sanitized_event + apply;
    # kind="quota" → validate_quota_event + set_quota. The quota vector proves the CRITICAL fix
    # end-to-end: a validated quota.window flows through set_quota into the device frame (the
    # quota[].account the device renders), and both languages reproduce that frame byte-for-byte.
    scenarios = [
        ("focus_single_coding", [("session", _ev_session("claude", "main", "project-a", "CODING", "implementing", "s1"))]),
        ("fleet_two_active", [("session", _ev_session("claude", "main", "project-a", "CODING", "implementing", "s1")),
                              ("session", _ev_session("codex", "main", "project-b", "TESTING", "testing", "s2"))]),
        ("alert_waiting", [("session", _ev_session("claude", "main", "project-a", "WAITING", "waiting", "s1",
                                                   needs_attention=True))]),
        ("sleep_all_idle", [("session", _ev_session("claude", "main", "project-a", "IDLE", "idle", "s1"))]),
        # Quota-danger alert: a 95% 5h-window burn on a neutral account, validated through the gate,
        # raises the quota alert and surfaces quota[].account="main" on the frame.
        ("quota_danger_alert", [
            ("session", _ev_session("claude", "main", "project-a", "CODING", "implementing", "s1")),
            ("quota", _quota_ev(provider="claude", account_alias="main", window_type="5h", used_ratio=0.95,
                                confidence="medium")),
        ]),
    ]
    out = []
    for name, events in scenarios:
        st = FrameState(device_token="t", device_id="orb-01")
        st.local_display = False  # relay mode: opaque labels (pre-neutral here → unchanged)
        for kind, e in events:
            if kind == "quota":
                q = V.validate_quota_event(e)
                st.set_quota(provider=q["provider"], account_alias=q["account_alias"],
                             window_type=q["window_type"], used_ratio=q["used_ratio"],
                             confidence=q["confidence"], is_estimated=q["is_estimated"])
            else:
                st.apply_event(e)
        frame = st.build_frame("orb-01")
        frame.pop("server_time", None)
        frame.pop("seq", None)
        # Emit events as {kind, event} so the TS port replays the same sequence.
        out.append({"name": name,
                    "events": [{"kind": kind, "event": e} for kind, e in events],
                    "expect_frame": frame})
    return out


def main() -> None:
    print("Generating parity corpora from the Python reference:")
    _write("policy.json", gen_policy())
    _write("hmac_vectors.json", gen_hmac())
    _write("sanitize_corpus.json", gen_sanitize_corpus())
    _write("quota_corpus.json", gen_quota_corpus())
    _write("frame_vectors.json", gen_frame_vectors())
    print("Done.")


if __name__ == "__main__":
    main()
