"""Required Fixtures from docs/security/sanitization_policy.md (+ fixtures md).

These assert the alias/hash MECHANISM — the product's entire trust claim — not
just pattern rejection:

  * unmapped_cwd      → HMAC project-xxxxxx, basename NEVER appears
  * low_entropy_branch→ keyed HMAC label, plain sha256("main") NOT produced
  * plan_tier_account → "Claude Max" → generic alias, never "Max"
  * real_model_id     → claude-opus-4-… / ft:gpt-4o:acme:… → collapses to enum
  * error_with_path   → error message with a path → error_label "unknown"
  * free_text_task    → arbitrary prompt fragment → controlled task_label/unknown
  * unknown_field     → extra key → WHOLE event rejected
  * stable_label      → same input twice → identical HMAC label
"""
from __future__ import annotations

import pytest

from agentlamp_server import sanitize as S
from .conftest import TEST_PEPPER


# --------------------------------------------------------------------------- #
# unmapped_cwd — HMAC label, never the basename.
# --------------------------------------------------------------------------- #
def test_unmapped_cwd_emits_hmac_not_basename(aliases, pepper):
    raw_cwd = "/Users/hulu/secret/client-acme-prod"
    label = S.project_alias(raw_cwd, aliases, pepper)

    assert label.startswith("project-"), label
    hexpart = label.split("-", 1)[1]
    assert len(hexpart) == 6 and all(c in "0123456789abcdef" for c in hexpart)

    # The basename and every path segment must NOT leak anywhere in the label.
    for segment in ("client-acme-prod", "client", "acme", "prod", "secret", "hulu"):
        assert segment not in label
    # Hard invariant: the label equals the keyed HMAC of the raw cwd.
    assert label == f"project-{S.hmac_label(pepper, raw_cwd)}"


def test_mapped_cwd_uses_alias(aliases, pepper):
    assert S.project_alias("/Users/hulu/work/acme", aliases, pepper) == "project-a"


# --------------------------------------------------------------------------- #
# low_entropy_branch — keyed HMAC, NOT plain sha256("main").
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("branch", ["main", "feature/login", "develop"])
def test_low_entropy_branch_uses_hmac_not_plain_sha256(branch, pepper):
    label = S.branch_label(branch, pepper)
    hexpart = label.split("-", 1)[1]

    # Must be the keyed HMAC...
    assert hexpart == S.hmac_label(pepper, branch)
    # ...and must NOT equal the brute-forceable plain sha256 prefix.
    assert hexpart != S.plain_sha256(branch, n=len(hexpart))
    # Full sha256 prefix at common lengths must not appear either.
    import hashlib

    full = hashlib.sha256(branch.encode()).hexdigest()
    assert hexpart not in full[: len(hexpart) + 4] or hexpart != full[: len(hexpart)]


def test_session_label_is_keyed(pepper):
    sid = "abc123"  # the low-entropy session id from the fixtures doc
    label = S.session_label(sid, pepper)
    assert label.startswith("hmac:")
    body = label.split(":", 1)[1]
    assert body == S.hmac_label(pepper, sid, n=12)
    assert body != S.plain_sha256(sid, n=12)


# --------------------------------------------------------------------------- #
# plan_tier_account — never echo the tier.
# --------------------------------------------------------------------------- #
def test_plan_tier_account_mapped_to_generic(pepper):
    # User maps the raw account key to a neutral alias; "Max" never appears.
    aliases = S.AliasMap(accounts={"claude-max-key": "work"})
    out = S.account_alias("claude-max-key", aliases, pepper)
    assert out == "work"
    assert "Max" not in out and "max" not in out


def test_plan_tier_unmapped_account_hmac_never_tier(pepper):
    # Even if the RAW input literally is "Claude Max" with no mapping, the output
    # is an HMAC label, never the tier string.
    out = S.account_alias("Claude Max", S.AliasMap(), pepper)
    assert out.startswith("account-")
    assert "Max" not in out and "Claude" not in out


def test_plan_tier_as_alias_value_is_rejected(pepper):
    # The mapping VALUE itself may not be a plan tier — default-deny.
    bad = S.AliasMap(accounts={"k": "Claude Max"})
    with pytest.raises(S.SanitizationError):
        S.account_alias("k", bad, pepper)


def test_event_with_plan_tier_leaf_rejected(aliases, pepper):
    ev = _envelope(payload={"status": "CODING", "account_alias": "Pro"})
    with pytest.raises(S.SanitizationError):
        S.sanitize_event(ev, aliases=aliases, pepper=pepper)


# --------------------------------------------------------------------------- #
# real_model_id — collapse to the provider enum, never echo the id.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("claude-opus-4-20250514", "claude"),
        ("claude-3-5-sonnet", "claude"),
        ("ft:gpt-4o:acme:custom:abc", "codex"),
        ("gpt-4o-mini", "codex"),
        ("o3-preview", "codex"),
        ("claude", "claude"),
        ("codex", "codex"),
        ("manual", "manual"),
        (None, "unknown"),
        ("llama-70b", "unknown"),
    ],
)
def test_real_model_id_collapses(raw, expected):
    assert S.normalize_model(raw) == expected
    if raw and raw not in S.MODEL_ENUM:
        # The collapsed value never contains the real id.
        assert S.normalize_model(raw) != raw


def test_event_with_real_model_id_in_model_field_collapses(aliases, pepper):
    ev = _envelope(payload={"status": "CODING", "model": "claude-opus-4-20250514"})
    out = S.sanitize_event(ev, aliases=aliases, pepper=pepper)
    assert out["payload"]["model"] == "claude"
    assert "opus" not in str(out)


# --------------------------------------------------------------------------- #
# error_with_path — drop to error_label "unknown", never echo the path.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw",
    [
        "ENOENT: /Users/hulu/work/client/src/auth.ts not found",
        "failed at ./src/index.js:42",
        "auth token sk-deadbeef rejected",
        "user@example.com lookup failed",
    ],
)
def test_error_with_path_drops_to_unknown(raw):
    assert S.normalize_error_label(raw) == "unknown"


def test_error_label_keeps_clean_category():
    assert S.normalize_error_label("rate_limit") == "rate_limit"
    assert S.normalize_error_label("timeout") == "timeout"
    # A short clean-ish description maps to a category, not echoed verbatim.
    assert S.normalize_error_label("429 too many requests") == "rate_limit"


def test_event_with_path_in_error_label_rejected_or_dropped(aliases, pepper):
    # A path anywhere in the event rejects the whole event (forbidden pattern).
    ev = _envelope(payload={"status": "ERROR", "error_label": "/Users/hulu/x"})
    with pytest.raises(S.SanitizationError):
        S.sanitize_event(ev, aliases=aliases, pepper=pepper)


# --------------------------------------------------------------------------- #
# free_text_task — controlled task_label, fragment never survives.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,allowed",
    [
        ("Implement the secret customer auth flow", {"implementing"}),
        ("fix the broken login bug", {"debugging"}),
        ("run the test suite and lint", {"testing"}),
        ("just some unrelated chatter here", {"unknown"}),
        ("refactor the parser module", {"refactoring"}),
    ],
)
def test_free_text_task_maps_to_controlled_label(raw, allowed):
    label = S.normalize_task_label(raw)
    assert label in S.TASK_LABEL_ENUM
    assert label in allowed
    # The raw fragment never appears in the returned label.
    assert raw.lower() not in label


def test_task_label_derived_from_tool_category():
    assert S.normalize_task_label(None, tool_category="edit") == "implementing"
    assert S.normalize_task_label(None, tool_category="read") == "reading"
    assert S.normalize_task_label(None, tool_category="test") == "testing"
    assert S.normalize_task_label(None, tool_category="approval") == "waiting"


def test_event_free_text_task_collapsed(aliases, pepper):
    # task_label arriving as free text collapses; a prompt fragment with a path
    # would be rejected, but a benign fragment collapses to a controlled label.
    ev = _envelope(payload={"status": "CODING", "task_label": "implementing"})
    out = S.sanitize_event(ev, aliases=aliases, pepper=pepper)
    assert out["payload"]["task_label"] in S.TASK_LABEL_ENUM


# --------------------------------------------------------------------------- #
# unknown_field — recursive default-deny rejects the WHOLE event.
# --------------------------------------------------------------------------- #
def test_unknown_top_level_field_rejected(aliases, pepper):
    ev = _envelope(payload={"status": "CODING"})
    ev["surprise"] = "anything"
    with pytest.raises(S.SanitizationError) as exc:
        S.sanitize_event(ev, aliases=aliases, pepper=pepper)
    assert "unknown_field" in exc.value.reason


def test_unknown_payload_field_rejected(aliases, pepper):
    ev = _envelope(payload={"status": "CODING", "mystery": 1})
    with pytest.raises(S.SanitizationError) as exc:
        S.sanitize_event(ev, aliases=aliases, pepper=pepper)
    assert "unknown_field" in exc.value.reason


def test_forbidden_raw_key_rejected(aliases, pepper):
    # An explicit raw-leak key name (cwd / transcript_path / prompt …) rejects.
    for key in ("cwd", "transcript_path", "prompt", "tool_input", "command"):
        ev = _envelope(payload={"status": "CODING"})
        ev[key] = "x"
        with pytest.raises(S.SanitizationError) as exc:
            S.sanitize_event(ev, aliases=aliases, pepper=pepper)
        assert "forbidden_key" in exc.value.reason


# --------------------------------------------------------------------------- #
# stable_label — same input twice → identical HMAC label.
# --------------------------------------------------------------------------- #
def test_stable_label_deterministic(pepper):
    a = S.hmac_label(pepper, "/Users/hulu/work/x")
    b = S.hmac_label(pepper, "/Users/hulu/work/x")
    assert a == b
    # Different pepper → different label (re-labels everything on rotation).
    other = S.hmac_label(b"a-different-pepper-value-here-xx", "/Users/hulu/work/x")
    assert other != a


def test_hmac_requires_pepper():
    with pytest.raises(ValueError):
        S.hmac_label(b"", "main")


# --------------------------------------------------------------------------- #
# Global rejection cases from the fixtures doc.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "leaf",
    [
        "/Users/hulu/work/client",
        "C:\\Users\\hulu",
        "./src/auth.ts",
        "../secrets",
        "https://github.com/acme/private",
        "git@github.com:acme/private.git",
        "sk-deadbeefcafe",
        "Bearer abc.def.ghi",
        "Cookie: session=xyz",
        "eyJhbGciOiJIUzI1.payload.sig",
        "user@example.com",
    ],
)
def test_global_rejection_patterns(leaf, aliases, pepper):
    ev = _envelope(payload={"status": "CODING", "project_alias": leaf})
    with pytest.raises(S.SanitizationError):
        S.sanitize_event(ev, aliases=aliases, pepper=pepper)


def test_codebody_density_rejected(aliases, pepper):
    ev = _envelope(payload={"status": "CODING", "task_label": "const x = {a:1}; b();"})
    with pytest.raises(S.SanitizationError):
        S.sanitize_event(ev, aliases=aliases, pepper=pepper)


# --------------------------------------------------------------------------- #
# Alias default-deny on the EVENT-PIPELINE emit path (not just project_alias()).
# Regression for the review finding: sanitize_event() previously emitted
# project_alias/account_alias verbatim after only forbidden-pattern + weak
# looks_like_prompt checks, so a raw directory-basename-like value
# (`client-acme-prod`) and a 3000-char blob passed unchanged.
# --------------------------------------------------------------------------- #
def test_pipeline_basename_alias_is_hmac_collapsed(aliases, pepper):
    """A multi-segment, basename-like alias never survives verbatim on the emit
    path — it is HMAC-collapsed to an opaque keyed label (default-deny)."""
    ev = _envelope(payload={"status": "CODING", "project_alias": "client-acme-prod"})
    out = S.sanitize_event(ev, aliases=aliases, pepper=pepper)
    pa = out["payload"]["project_alias"]
    assert pa != "client-acme-prod"
    assert pa == f"project-{S.hmac_label(pepper, 'client-acme-prod')}"
    # No path segment leaks into the collapsed label.
    for seg in ("client", "acme", "prod"):
        assert seg not in pa
    # Same for account_alias.
    ev2 = _envelope(payload={"status": "CODING", "account_alias": "client-acme-prod"})
    aa = S.sanitize_event(ev2, aliases=aliases, pepper=pepper)["payload"]["account_alias"]
    assert aa != "client-acme-prod"
    assert aa == f"account-{S.hmac_label(pepper, 'client-acme-prod', n=4)}"


def test_pipeline_oversize_alias_is_collapsed(aliases, pepper):
    """A 3000-char alias is collapsed to a short opaque label (bounds the field
    so it can never blow the frame byte cap or smuggle content)."""
    big = "a" * 3000
    ev = _envelope(payload={"status": "CODING", "project_alias": big})
    pa = S.sanitize_event(ev, aliases=aliases, pepper=pepper)["payload"]["project_alias"]
    assert pa == f"project-{S.hmac_label(pepper, big)}"
    assert len(pa) <= len("project-") + 6


@pytest.mark.parametrize("alias", ["main", "work", "project-a", "project-7f3a9c", "account-7f3a", "claude-1"])
def test_pipeline_neutral_alias_survives_verbatim(alias, aliases, pepper):
    """A genuinely-neutral short alias must pass through unchanged (no false
    positives that would re-label every legitimate project)."""
    ev = _envelope(payload={"status": "CODING", "project_alias": alias})
    out = S.sanitize_event(ev, aliases=aliases, pepper=pepper)
    assert out["payload"]["project_alias"] == alias


# --------------------------------------------------------------------------- #
# Helper.
# --------------------------------------------------------------------------- #
def _envelope(payload: dict) -> dict:
    return {
        "schema_version": 1,
        "provider": "claude",
        "adapter": "claude_hooks",
        "event_type": "session.status",
        "provider_event_name": "PreToolUse",
        "provider_session_id": "hmac:7f3a9c",
        "event_time": 1716900398,
        "payload": dict(payload),
    }
