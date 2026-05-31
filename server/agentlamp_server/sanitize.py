"""Default-deny sanitizer for AgentLamp.

Source of truth: ``docs/security/sanitization_policy.md`` and
``docs/security/provider_sanitization_fixtures.md``.

The product's entire trust claim is this module. It does not "best-effort
redact" — it turns a raw signal into one of three safe shapes:

1. a controlled **enum** (status / task_label / error_label / model / provider /
   tool_category / status_detail), or
2. a **user-controlled alias** from the local alias map, or
3. a **keyed-HMAC label** (per-collector pepper, never uploaded) when there is no
   alias and the value is otherwise low-entropy.

It NEVER:

* guesses a human-readable label from a path (no directory basename, no segment),
* emits plain ``sha256("main")`` for a low-entropy id (brute-forceable),
* echoes a plan tier, a real model id, free text, or any forbidden pattern.

An unconfigured machine emits nothing human-readable: an unmapped ``cwd`` becomes
``project-<hmac6>``.

All public helpers are pure / stdlib-only (``hmac``, ``hashlib``, ``re``). The
pepper is supplied by the caller (the collector loads it from the OS keyring; in
local-server-only mode a process-local pepper is generated — see ``state.py``).
"""
from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Policy constant.
# --------------------------------------------------------------------------- #
POLICY_VERSION = 1

# --------------------------------------------------------------------------- #
# Controlled vocabularies (sanitization_policy.md → Controlled Vocabularies).
# These are the ONLY values these fields may take. Anything else → fallback.
# --------------------------------------------------------------------------- #
PROVIDER_ENUM = ("codex", "claude", "manual")
MODEL_ENUM = ("codex", "claude", "manual", "unknown")

STATUS_ENUM = (
    "IDLE",
    "THINKING",
    "CODING",
    "READING",
    "TESTING",
    "WAITING",
    "DONE",
    "ERROR",
    "OFFLINE",
    "STALE",
    "UNKNOWN",
)

STATUS_DETAIL_ENUM = ("compacting", "tool_running", "subagent", "unknown")

TOOL_CATEGORY_ENUM = ("read", "edit", "test", "shell", "mcp", "approval", "error")

TASK_LABEL_ENUM = (
    "implementing",
    "debugging",
    "testing",
    "reviewing",
    "refactoring",
    "reading",
    "planning",
    "waiting",
    "idle",
    "unknown",
)

ERROR_LABEL_ENUM = (
    "rate_limit",
    "timeout",
    "permission",
    "api_error",
    "tool_error",
    "network",
    "unknown",
)

CONFIDENCE_ENUM = ("high", "medium", "low", "unknown")
CONFIDENCE_INT = {"high": 3, "medium": 2, "low": 1, "unknown": 0}

# Tool category → status (provider_normalization.md → Tool Category Mapping).
TOOL_CATEGORY_STATUS = {
    "read": "READING",
    "edit": "CODING",
    "test": "TESTING",
    "shell": "CODING",
    "mcp": "CODING",
    "approval": "WAITING",
    "error": "ERROR",
}

# Default keyword set for the local `test` classifier (locally extensible).
DEFAULT_TEST_KEYWORDS = (
    "test",
    "spec",
    "check",
    "lint",
    "build",
    "ci",
    "verify",
    "validate",
)

# --------------------------------------------------------------------------- #
# Forbidden patterns (sanitization_policy.md → Forbidden Patterns + fixtures).
# A value matching any of these rejects the WHOLE event (default-deny).
# --------------------------------------------------------------------------- #
_PLAN_TIERS = ("max", "team", "pro", "plus", "enterprise")

# Real model id shapes that must never escape the `model` enum. Anchored on
# actual model-family tokens (opus/sonnet/haiku/gpt/gemini/llama/ft:) so generic
# aliases like `claude-account-01` or `project-7f3a` do NOT false-positive.
_MODEL_ID_RE = re.compile(
    r"("
    r"claude-(?:opus|sonnet|haiku|instant)"   # claude-opus-4-…
    r"|(?:opus|sonnet|haiku)-\d"               # opus-4-…
    r"|gpt-[0-9o]"                             # gpt-4o, gpt-3.5
    r"|\bft:"                                  # ft:gpt-4o:acme:…
    r"|gemini-\d"
    r"|llama-?\d"
    r"|\bo[134]-(?:mini|preview)\b"            # o1-preview, o3-mini
    r")",
    re.IGNORECASE,
)

# Forbidden substrings / regexes scanned over every leaf string value.
_FORBIDDEN_PATTERNS = (
    re.compile(r"/Users/"),
    re.compile(r"/home/"),
    re.compile(r"\b[A-Za-z]:\\"),                 # C:\  D:\
    re.compile(r"(?:\./|\.\./)"),                 # ./src  ../
    re.compile(r"https?://"),
    re.compile(r"\bgit@"),
    re.compile(r"ssh://"),
    re.compile(r"sk-[A-Za-z0-9]"),                # OpenAI-style keys
    re.compile(r"\bBearer\s", re.IGNORECASE),
    re.compile(r"\bCookie:", re.IGNORECASE),
    re.compile(r"eyJ[A-Za-z0-9_-]{6,}\."),        # JWT
    re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),  # email
    re.compile(r"\bsha256[(:]", re.IGNORECASE),   # never echo a plain sha256(...) call
)

# A "looks like an identifier / path fragment" run: a 6+ char token that mixes a
# path separator, @, or a long opaque run. Used to defensively scrub error labels.
_IDENTIFIER_RUN_RE = re.compile(r"[/\\:@]|[A-Za-z0-9_-]{6,}")

# Code-density heuristic: multiple braces / semicolons / newlines = a snippet.
_CODE_DENSITY_RE = re.compile(r"[{};]")

# --------------------------------------------------------------------------- #
# Positive alias shape gate (sanitization_policy.md → Allowed Field Classes).
# An alias arriving on the event-pipeline emit path is DEFAULT-DENY: it must
# *positively* match a tiny neutral display shape, or it is HMAC-collapsed to an
# opaque keyed label. Forbidden-pattern + prompt heuristics are necessary but not
# sufficient — `client-acme-prod` and `a`*3000 pass those yet are not neutral.
# --------------------------------------------------------------------------- #
# Max length of any emitted alias (neutral labels are short: `project-a`,
# `account-7f3a`, `main`, `work`). Anything longer is collapsed.
ALIAS_MAX_LEN = 40

# Allowlist: lowercase ASCII neutral label, ≤ 2 hyphen segments, 1..ALIAS_MAX_LEN.
# Accepts `main`, `work`, `project-a`, `project-7f3a9c`, `account-7f3a`,
# `claude-1`, `branch-7f3a9c`, `hmac:abc123def456`, the em-dash placeholder `—`.
# Rejects: spaces, paths, multi-word prompts, uppercase plan tiers (`Max`,`Pro`),
# 3000-char blobs, AND 3+-segment path-basename-like kebabs (`client-acme-prod`)
# — the basename rule (policy §The Alias Mechanism) means a raw multi-segment
# directory name must never survive as an alias; it is HMAC-collapsed instead.
_ALIAS_SHAPE_RE = re.compile(
    r"^(?:"
    r"—"                                  # em-dash empty-primary placeholder
    r"|hmac:[a-z0-9]+"                     # session-style keyed label
    r"|[a-z0-9]+(?:-[a-z0-9]+)?"           # word or word-suffix (≤ 2 segments)
    r")$"
)


def looks_like_neutral_alias(value: str) -> bool:
    """True iff ``value`` positively matches the neutral display-alias shape
    (max-len + allowlist regex). Used by the event pipeline as a default-deny
    gate before emitting ``project_alias`` / ``account_alias`` verbatim."""
    if not value or len(value) > ALIAS_MAX_LEN:
        return False
    return _ALIAS_SHAPE_RE.match(value) is not None


# Readable display label for LOCAL single-owner mode: lowercase alnum with single
# ``-``/``_`` separators, any number of segments, bounded length (``ai-center``,
# ``moza-perception-analysis``). Allowed verbatim ONLY when the caller passes
# ``display=True`` — i.e. the local frame server, whose only viewer is the owner at
# their own desk. Relay mode keeps the strict 2-segment neutral shape so a real cwd
# basename can never reach a cloud relay operator verbatim.
_DISPLAY_LABEL_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")


def looks_like_display_label(value: str) -> bool:
    return bool(value) and len(value) <= 40 and _DISPLAY_LABEL_RE.match(value) is not None


def coerce_alias(value: str, pepper: bytes, *, prefix: str, n: int = 6, display: bool = False) -> str:
    """Return ``value`` unchanged iff it is a neutral alias shape; otherwise
    HMAC-collapse it to an opaque keyed label ``<prefix>-<hmac_n>``.

    This is the positive-shape gate for the emit path: a non-neutral alias never
    survives verbatim. It is keyed (pepper) so a relay operator cannot brute-force
    the collapsed value, and deterministic so the device sees a stable label.

    ``display=True`` (local single-owner mode only) additionally lets a readable
    multi-segment folder name through verbatim — see ``looks_like_display_label``."""
    if looks_like_neutral_alias(value):
        return value
    if display and looks_like_display_label(value):
        return value
    return f"{prefix}-{hmac_label(pepper, value, n=n)}"


class SanitizationError(Exception):
    """Raised when an event must be rejected (default-deny).

    Carries only metadata (reason + payload hash), never the offending value —
    so a caller can log a rejection without persisting the raw leak.
    """

    def __init__(self, reason: str, payload_hash: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.payload_hash = payload_hash


# --------------------------------------------------------------------------- #
# Keyed hashing (sanitization_policy.md → Keyed Hashing).
# --------------------------------------------------------------------------- #
def hmac_label(pepper: bytes, raw: str, n: int = 6) -> str:
    """Return ``first_n`` hex chars of HMAC-SHA256(pepper, raw).

    Keyed: a relay operator without the pepper cannot brute-force a low-entropy
    value (``main``, ``feature/login``). Deterministic: same input → same label,
    so the device sees a stable opaque id across sessions.
    """
    if not isinstance(pepper, (bytes, bytearray)) or len(pepper) == 0:
        raise ValueError("hmac_label requires a non-empty pepper")
    digest = hmac.new(pepper, raw.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:n]


def plain_sha256(raw: str, n: int = 6) -> str:
    """Plain (un-keyed) SHA256 — for tests proving we do NOT emit this for
    low-entropy values. Not used in the production path for low-entropy ids."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:n]


def payload_hash(obj: object) -> str:
    """Stable hash of a payload for rejection audit (counts, never the value)."""
    return hashlib.sha256(repr(obj).encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Alias map (sanitization_policy.md → The Alias Mechanism).
# --------------------------------------------------------------------------- #
@dataclass
class AliasMap:
    """User-maintained local map: raw signal → neutral display alias.

    Loaded from ``~/.config/agentlamp/aliases.toml`` (LOCAL ONLY). Values must
    themselves be neutral (``project-a``, ``main``) — the mapping cannot be used
    to smuggle a plan tier / path / email past the policy (validated on lookup).
    """

    projects: dict[str, str] = field(default_factory=dict)
    accounts: dict[str, str] = field(default_factory=dict)

    def project(self, raw: str) -> str | None:
        return self.projects.get(raw)

    def account(self, raw: str) -> str | None:
        return self.accounts.get(raw)


def load_alias_map(path: str) -> AliasMap:
    """Load aliases.toml (stdlib ``tomllib``). Missing file → empty map."""
    import os
    import tomllib

    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        return AliasMap()
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    projects = {str(k): str(v) for k, v in (data.get("projects") or {}).items()}
    accounts = {str(k): str(v) for k, v in (data.get("accounts") or {}).items()}
    return AliasMap(projects=projects, accounts=accounts)


# --------------------------------------------------------------------------- #
# Leaf-value forbidden-pattern scan.
# --------------------------------------------------------------------------- #
def contains_forbidden(value: str) -> str | None:
    """Return the name of the first forbidden pattern matched, else None."""
    for pat in _FORBIDDEN_PATTERNS:
        if pat.search(value):
            return f"forbidden:{pat.pattern[:24]}"
    low = value.lower()
    # Plan/tier names as standalone words.
    for tier in _PLAN_TIERS:
        if re.search(rf"\b{tier}\b", low):
            return f"plan_tier:{tier}"
    # Real model id shapes.
    if _MODEL_ID_RE.search(value):
        return "model_id"
    # Code-density: 2+ of { } ; or a newline = a source snippet (leak channel).
    if len(_CODE_DENSITY_RE.findall(value)) >= 2 or "\n" in value:
        return "code_density"
    return None


def assert_clean(value: str, *, ph: str = "") -> None:
    """Reject the whole event if ``value`` matches any forbidden pattern."""
    hit = contains_forbidden(value)
    if hit is not None:
        raise SanitizationError(hit, ph)


def looks_like_prompt(value: str) -> bool:
    """Prompt/transcript/source heuristic: any length (a 159-char fragment still
    leaks). We treat anything with whitespace-separated multi-word natural text
    OR code density as prompt-like when it appears in a non-enum field."""
    if _CODE_DENSITY_RE.search(value):
        return True
    # 4+ whitespace-separated tokens that are not a known short alias shape.
    return len([t for t in value.split() if t]) >= 4


# --------------------------------------------------------------------------- #
# Enum coercion helpers — collapse to a safe enum or a defined fallback.
# --------------------------------------------------------------------------- #
def normalize_provider(raw: str) -> str:
    """Provider wire enum. Unknown → SanitizationError (mandatory field)."""
    v = (raw or "").strip().lower()
    if v in PROVIDER_ENUM:
        return v
    raise SanitizationError("provider_not_in_enum")


def normalize_model(raw: str | None) -> str:
    """Collapse any model string to the provider enum. A real model id
    (``claude-opus-4-…`` / ``ft:gpt-4o:acme:…``) collapses, never escapes."""
    if not raw:
        return "unknown"
    v = raw.strip().lower()
    if v in MODEL_ENUM:
        return v
    # Map real ids to their family, never echoing the id.
    if v.startswith("claude") or "claude" in v:
        return "claude"
    if v.startswith(("gpt", "o1", "o3", "o4", "codex", "ft:")) or "gpt" in v:
        return "codex"
    return "unknown"


def normalize_status(raw: str | None) -> str:
    v = (raw or "").strip().upper()
    return v if v in STATUS_ENUM else "UNKNOWN"


def normalize_status_detail(raw: str | None) -> str:
    v = (raw or "").strip().lower()
    return v if v in STATUS_DETAIL_ENUM else "unknown"


def normalize_tool_category(raw: str | None) -> str:
    v = (raw or "").strip().lower()
    return v if v in TOOL_CATEGORY_ENUM else "shell"


def normalize_confidence(raw: str | None) -> str:
    v = (raw or "").strip().lower()
    return v if v in CONFIDENCE_ENUM else "unknown"


# Free-text → controlled task_label. The fragment NEVER survives: we only ever
# return a member of TASK_LABEL_ENUM. Mapping is by tool category / status first,
# then a tiny keyword sniff, else `unknown`.
_TASK_KEYWORDS = {
    "implementing": ("implement", "add", "build", "create", "write", "feature"),
    "debugging": ("debug", "fix", "bug", "error", "trace"),
    "testing": ("test", "spec", "lint", "ci", "verify"),
    "reviewing": ("review", "audit", "inspect"),
    "refactoring": ("refactor", "rename", "cleanup", "restructure"),
    "reading": ("read", "search", "explore", "look", "find"),
    "planning": ("plan", "design", "spec", "outline"),
}


def normalize_task_label(
    raw: str | None,
    *,
    tool_category: str | None = None,
    status: str | None = None,
) -> str:
    """Always return a controlled ``task_label``. A free-text prompt fragment is
    NEVER echoed — it is mapped to a vocabulary member or ``unknown``."""
    # 1. Direct enum value (e.g. a manual selection from the list).
    if raw:
        cand = raw.strip().lower()
        if cand in TASK_LABEL_ENUM:
            return cand
    # 2. Derive from tool category + status (the policy's primary derivation).
    tc = (tool_category or "").lower()
    st = (status or "").upper()
    cat_map = {
        "read": "reading",
        "edit": "implementing",
        "test": "testing",
        "approval": "waiting",
        "error": "debugging",
    }
    if tc in cat_map:
        return cat_map[tc]
    status_map = {
        "WAITING": "waiting",
        "IDLE": "idle",
        "READING": "reading",
        "TESTING": "testing",
        "CODING": "implementing",
        "ERROR": "debugging",
        "THINKING": "planning",
    }
    if st in status_map:
        return status_map[st]
    # 3. Last-resort keyword sniff on free text — but ONLY to pick an enum member,
    #    the raw text is discarded either way.
    if raw:
        low = raw.lower()
        for label, kws in _TASK_KEYWORDS.items():
            if any(k in low for k in kws):
                return label
    return "unknown"


def normalize_error_label(raw: str | None) -> str:
    """Collapse an error candidate to a category enum. Anything containing a
    path / identifier-run / forbidden pattern drops to ``unknown`` — the raw
    message is never emitted."""
    if not raw:
        return "unknown"
    cand = raw.strip().lower()
    if cand in ERROR_LABEL_ENUM:
        return cand
    # Hard drop: anything carrying a path / separator / secret / forbidden
    # pattern collapses to unknown — the raw message is NEVER emitted.
    if contains_forbidden(raw) is not None:
        return "unknown"
    if any(ch in raw for ch in ("/", "\\", ":", "@")) or "sk-" in raw:
        return "unknown"
    # Tiny keyword sniff to a category (runs on the cleaned candidate).
    kw = {
        "rate_limit": ("rate", "429", "limit", "quota"),
        "timeout": ("timeout", "timed out", "deadline"),
        "permission": ("permission", "denied", "forbidden", "403", "401"),
        "api_error": ("api", "500", "502", "503", "server error"),
        "network": ("network", "connection", "dns", "unreachable", "econn"),
        "tool_error": ("tool", "exec", "command failed", "non-zero"),
    }
    for label, kws in kw.items():
        if any(k in cand for k in kws):
            return label
    # No category matched and it looks like an opaque identifier → unknown.
    if _IDENTIFIER_RUN_RE.search(raw) and len(raw) > 12:
        return "unknown"
    return "unknown"


# --------------------------------------------------------------------------- #
# Alias resolution: cwd / account / branch → safe alias.
# These are the three places where the *mechanism* is proven by fixtures.
# --------------------------------------------------------------------------- #
def project_alias(raw_cwd: str, aliases: AliasMap, pepper: bytes) -> str:
    """cwd → project alias. Mapped value if present (validated neutral), else a
    keyed-HMAC ``project-<hmac6>`` label. NEVER the directory basename."""
    mapped = aliases.project(raw_cwd)
    if mapped is not None:
        # The mapping value itself must be neutral (can't smuggle a leak).
        if contains_forbidden(mapped) is not None:
            raise SanitizationError("alias_value_not_neutral")
        return mapped
    # No match → keyed opaque label. Never a guess from the path.
    return f"project-{hmac_label(pepper, raw_cwd)}"


def account_alias(raw_account: str, aliases: AliasMap, pepper: bytes) -> str:
    """account key → account alias. Mapped neutral value, else ``account-<hmac4>``.
    Never the plan tier (``Claude Max`` → generic alias, never ``Max``)."""
    # If the *input* is a plan tier and there is no neutral mapping, we still
    # never echo it — we HMAC the raw key. A mapping is the user's chance to pick
    # a neutral label; its value is validated neutral.
    mapped = aliases.account(raw_account)
    if mapped is not None:
        if contains_forbidden(mapped) is not None:
            raise SanitizationError("alias_value_not_neutral")
        return mapped
    return f"account-{hmac_label(pepper, raw_account, n=4)}"


def session_label(raw_session_id: str, pepper: bytes) -> str:
    """Low-entropy/opaque session id → keyed-HMAC label ``hmac:<hmac…>``.

    Uses HMAC, not plain sha256, because many session ids are low-entropy."""
    return f"hmac:{hmac_label(pepper, raw_session_id, n=12)}"


def branch_label(raw_branch: str, pepper: bytes) -> str:
    """Low-entropy branch (``main`` / ``feature/login``) → keyed-HMAC label.
    Asserted by ``low_entropy_branch`` to never equal plain ``sha256(branch)``."""
    return f"branch-{hmac_label(pepper, raw_branch)}"


# --------------------------------------------------------------------------- #
# Event-level sanitizer (provider envelope → normalized sanitized event).
# Recursive default-deny: any unknown key rejects the whole event.
# --------------------------------------------------------------------------- #

# Known top-level envelope keys (provider_normalization.md → Event Envelope +
# sanitization_policy.md → Non-sensitive transport/envelope metadata).
_KNOWN_ENVELOPE_KEYS = {
    "schema_version",
    "provider",
    "adapter",
    "adapter_version",
    "event_type",
    "event_id",
    "provider_event_name",
    "provider_session_id",
    "event_time",
    "updated_at",
    "started_at",
    "source_seq",
    "batch_id",
    "dedupe_key",
    "turn_id",
    "needs_attention",
    "payload",
    "sanitization",
}

# Known payload keys (already-sanitized, enum-only shapes).
_KNOWN_PAYLOAD_KEYS = {
    "status",
    "status_detail",
    "tool_category",
    "task_label",
    "project_alias",
    "account_alias",
    "model",
    "error_label",
    "confidence",
    "needs_attention",
}

# Provider hook field names that must NEVER appear (raw leak channels).
_FORBIDDEN_KEYS = {
    "cwd",
    "transcript_path",
    "prompt",
    "tool_response",
    "content",
    "old_string",
    "new_string",
    "tool_input",
    "command",
}


@dataclass
class SanitizeStats:
    redactions: int = 0
    rejections: int = 0


def reject_unknown_fields(obj: dict, known: set[str], *, where: str) -> None:
    """Recursive default-deny: any key not in ``known`` rejects the event.
    Also hard-rejects the explicit forbidden raw-leak key names."""
    for key in obj:
        if key in _FORBIDDEN_KEYS:
            raise SanitizationError(f"forbidden_key:{where}.{key}")
        if key not in known:
            raise SanitizationError(f"unknown_field:{where}.{key}")


def sanitize_event(
    event: dict,
    *,
    aliases: AliasMap,
    pepper: bytes,
    stats: SanitizeStats | None = None,
    local_display: bool = False,
) -> dict:
    """Validate + normalize a provider event envelope into a safe sanitized event.

    Default-deny:
      * unknown top-level / payload key → reject whole event,
      * forbidden raw-leak key name → reject,
      * any leaf string matching a forbidden pattern → reject,
      * enums coerced to their controlled values; free text collapsed.

    Returns the sanitized event with ``sanitization.policy_version`` attached.
    Raises ``SanitizationError`` (metadata only) on rejection.
    """
    if stats is None:
        stats = SanitizeStats()
    ph = payload_hash(event)

    if not isinstance(event, dict):
        raise SanitizationError("event_not_object", ph)

    # 1. Recursive unknown-field rejection at the envelope level.
    reject_unknown_fields(event, _KNOWN_ENVELOPE_KEYS, where="event")

    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        raise SanitizationError("payload_not_object", ph)
    reject_unknown_fields(payload, _KNOWN_PAYLOAD_KEYS, where="payload")

    # 2. Scan every leaf string for forbidden patterns BEFORE we trust any value.
    #    (Catches a leak smuggled inside an otherwise-known field.)
    #    Exception: `payload.model` legitimately COLLAPSES a real model id to the
    #    provider enum (policy: "Collapse to the provider enum"), so a model id
    #    there must not reject the whole event. Every other field is scanned.
    scan_view = {k: v for k, v in event.items() if k != "payload"}
    scan_payload = {k: v for k, v in payload.items() if k != "model"}
    _scan_leaves(scan_view, ph)
    _scan_leaves(scan_payload, ph)

    # 3. Provider (mandatory, enum-only).
    provider = normalize_provider(event.get("provider", ""))

    # 4. Build the sanitized payload from enums only.
    sp: dict = {}
    status = normalize_status(payload.get("status"))
    tool_category = (
        normalize_tool_category(payload.get("tool_category"))
        if payload.get("tool_category") is not None
        else None
    )
    # If status absent but tool category present, derive status from category.
    if status == "UNKNOWN" and tool_category:
        status = TOOL_CATEGORY_STATUS.get(tool_category, "UNKNOWN")
    sp["status"] = status
    if tool_category is not None:
        sp["tool_category"] = tool_category
    if payload.get("status_detail") is not None:
        sp["status_detail"] = normalize_status_detail(payload.get("status_detail"))

    sp["task_label"] = normalize_task_label(
        payload.get("task_label"),
        tool_category=tool_category,
        status=status,
    )
    sp["model"] = normalize_model(payload.get("model"))
    if payload.get("error_label") is not None:
        sp["error_label"] = normalize_error_label(payload.get("error_label"))
    if payload.get("confidence") is not None:
        sp["confidence"] = normalize_confidence(payload.get("confidence"))
    if "needs_attention" in payload:
        sp["needs_attention"] = bool(payload.get("needs_attention"))

    # project_alias / account_alias arrive already-aliased from the adapter (the
    # adapter resolves cwd/account via the alias map before this envelope). We
    # DEFAULT-DENY on the emit path: the alias must POSITIVELY match a neutral
    # display shape (max-len + allowlist regex), else it is HMAC-collapsed to an
    # opaque keyed label. Forbidden-pattern + prompt heuristics alone are not
    # sufficient (`client-acme-prod` / `a`*3000 pass them yet are not neutral).
    if payload.get("project_alias") is not None:
        pa = str(payload["project_alias"])
        assert_clean(pa, ph=ph)  # still hard-reject an embedded leak (path/key/…)
        if looks_like_prompt(pa):
            raise SanitizationError("project_alias_prompt_like", ph)
        sp["project_alias"] = coerce_alias(pa, pepper, prefix="project", n=6, display=local_display)
    if payload.get("account_alias") is not None:
        aa = str(payload["account_alias"])
        assert_clean(aa, ph=ph)
        if looks_like_prompt(aa):
            raise SanitizationError("account_alias_prompt_like", ph)
        sp["account_alias"] = coerce_alias(aa, pepper, prefix="account", n=4)

    out = {
        "schema_version": int(event.get("schema_version", 1)),
        "provider": provider,
        "provider_event_name": event.get("provider_event_name"),
        "provider_session_id": event.get("provider_session_id"),
        "event_time": event.get("event_time"),
        "payload": sp,
        "sanitization": {"policy_version": POLICY_VERSION},
    }
    # Strip None envelope values for compactness.
    return {k: v for k, v in out.items() if v is not None}


def _scan_leaves(obj: object, ph: str) -> None:
    """Walk every string leaf; reject the event on any forbidden pattern or
    prompt/transcript text. Recursion implements the 'any field, any depth' rule."""
    if isinstance(obj, str):
        assert_clean(obj, ph=ph)
        return
    if isinstance(obj, dict):
        for v in obj.values():
            _scan_leaves(v, ph)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _scan_leaves(v, ph)
