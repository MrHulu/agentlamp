"""Parity-corpus faithfulness tests (BUILD-SPEC I2, docs/devlog/16).

These prove the generated corpora in ``tests/fixtures/parity/`` match the LIVE Python
reference (validate.py + ingest.canonical_string + state.build_frame). The TypeScript
Worker/DO (src/cloud) asserts against the SAME files — so if Python logic drifts, this fails
here AND the TS parity test fails there, and the build is blocked until both are regenerated.

Run ``python3 tests/fixtures/parity/generate.py`` to regenerate after any policy/logic change.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from agentlamp_server import ingest as I
from agentlamp_server import sanitize as S
from agentlamp_server import validate as V
from agentlamp_server.state import FrameState

FX = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "parity"


def _load(name: str):
    return json.loads((FX / name).read_text())


def test_corpora_exist():
    for f in ("policy.json", "hmac_vectors.json", "sanitize_corpus.json", "frame_vectors.json"):
        assert (FX / f).is_file(), f"missing parity corpus {f} — run generate.py"


@pytest.mark.parametrize("vec", _load("hmac_vectors.json"))
def test_hmac_vector_roundtrips(vec):
    """The canonical string + HMAC signature recompute byte-for-byte (the frozen wire spec)."""
    sha = hashlib.sha256(vec["body_utf8"].encode("utf-8")).hexdigest()
    assert sha == vec["payload_sha256"]
    canon = I.canonical_string("POST", vec["path"], vec["kid"], str(vec["timestamp"]),
                               vec["nonce"], sha)
    assert canon == vec["canonical_string"]
    sig = hmac.new(vec["secret_utf8"].encode("utf-8"), canon.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    assert sig == vec["signature"]


@pytest.mark.parametrize("case", _load("sanitize_corpus.json"), ids=lambda c: c["name"])
def test_validate_decision_matches_corpus(case):
    """validate_sanitized_event reproduces the recorded accept/reject decision."""
    if case["expect"] == "accepted":
        V.validate_sanitized_event(case["event"])  # must not raise
    else:
        with pytest.raises(S.SanitizationError):
            V.validate_sanitized_event(case["event"])


@pytest.mark.parametrize("case", _load("quota_corpus.json"), ids=lambda c: c["name"])
def test_validate_quota_decision_matches_corpus(case):
    """validate_quota_event reproduces the recorded accept/reject decision + reason (the CRITICAL
    second gate the quota.window ingest branch previously bypassed, docs/devlog/16 I1)."""
    if case["expect"] == "accepted":
        V.validate_quota_event(case["event"])  # must not raise
    else:
        with pytest.raises(S.SanitizationError) as exc:
            V.validate_quota_event(case["event"])
        if case.get("reason") is not None:
            assert exc.value.reason == case["reason"]


def test_validate_quota_rejects_nan_corpus_independent():
    """A literal NaN used_ratio can't round-trip through json.dumps, so assert it here directly:
    a non-finite used_ratio REJECTS (mirrors Python float() raising / TS Number.isFinite gate).
    This is the NaN divergence the parity fix closes — TS previously fed Number(...)→NaN straight
    into setQuota where Python rejected."""
    ev = {"event_id": "q", "event_type": "quota.window", "provider": "claude",
          "account_alias": "main",
          "payload": {"window_type": "5h", "used_ratio": float("nan")}}
    with pytest.raises(S.SanitizationError):
        V.validate_quota_event(ev)


@pytest.mark.parametrize("vec", _load("frame_vectors.json"), ids=lambda v: v["name"])
def test_frame_vector_matches(vec):
    """state.build_frame reproduces the golden frame (volatile fields excluded).

    Events are {kind, event}: kind="session" → validate_sanitized_event + apply; kind="quota" →
    validate_quota_event + set_quota (proves the validated quota path lands on the frame)."""
    st = FrameState(device_token="t", device_id="orb-01")
    st.local_display = False
    for item in vec["events"]:
        kind = item["kind"]
        ev = item["event"]
        if kind == "quota":
            q = V.validate_quota_event(ev)
            st.set_quota(provider=q["provider"], account_alias=q["account_alias"],
                         window_type=q["window_type"], used_ratio=q["used_ratio"],
                         confidence=q["confidence"], is_estimated=q["is_estimated"])
        else:
            V.validate_sanitized_event(ev)
            st.apply_event(ev)
    frame = st.build_frame("orb-01")
    frame.pop("server_time", None)
    frame.pop("seq", None)
    assert frame == vec["expect_frame"]


def test_policy_matches_live_constants():
    """policy.json was generated from the live sanitize.py constants (no hand-retyped drift)."""
    pol = _load("policy.json")
    assert pol["status_enum"] == list(S.STATUS_ENUM)
    assert pol["task_label_enum"] == list(S.TASK_LABEL_ENUM)
    assert pol["model_enum"] == list(S.MODEL_ENUM)
    assert set(pol["forbidden_keys"]) == S._FORBIDDEN_KEYS
    assert set(pol["validate_payload_keys"]) == V.VALIDATE_PAYLOAD_KEYS
    assert set(pol["validate_envelope_keys"]) == V.VALIDATE_ENVELOPE_KEYS
