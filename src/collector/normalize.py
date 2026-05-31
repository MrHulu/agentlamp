"""Normalize a raw provider hook record into the server's neutral shorthand.

Input  : ``{"provider": "claude"|"codex", "received_at": float, "hook": {<raw>}}``
Output : an action + (optionally) a sanitized shorthand body for ``/admin/event``:
         ``{provider, account, status, project, provider_session_id, model,
            tool_category?, error_label?, needs_attention?}``

It REUSES the server sanitizer (``agentlamp_server.sanitize`` via ``config.S``) for
the one security-load-bearing step — turning ``cwd`` into a neutral alias
(mapped value, else keyed ``project-<hmac6>``, NEVER a basename). Raw commands /
prompts / file paths are read ONLY locally to pick an enum, then discarded; they
never appear in the output.

Hook names are not a stable API: an unrecognized event is mapped to ``heartbeat``
(keep the session alive, do not clobber its real status) with the event name kept
for diagnostics — never a hard-fail (``docs/providers/*_adapter.md``).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from . import config
from .config import S

# A readable display label is lowercase alnum with single - or _ separators.
_LABEL_CLEAN = re.compile(r"[^a-z0-9_-]+")


def _readable_label(cwd: str) -> str | None:
    """cwd -> the real folder name as a readable, bounded display label (local mode
    only). e.g. ``/Users/hulu/huluman/ai-center`` -> ``ai-center``. Returns None if
    the basename can't be made into a clean label (caller falls back to HMAC)."""
    base = os.path.basename(cwd.rstrip("/"))
    if not base:
        return None
    lab = _LABEL_CLEAN.sub("-", base.lower()).strip("-_")
    lab = re.sub(r"-{2,}", "-", lab)[:40]
    # Folder names are normally clean; if one somehow carries a forbidden pattern
    # (path/secret/email), don't emit it — let the caller HMAC instead.
    if not lab or S.contains_forbidden(lab) is not None:
        return None
    return lab

# --------------------------------------------------------------------------- #
# Tool name -> category. Covers Claude + Codex tool surfaces.
# --------------------------------------------------------------------------- #
_READ_TOOLS = {
    "read", "notebookread", "glob", "grep", "ls", "webfetch", "websearch",
    "read_file", "view", "list_dir", "file_search", "grep_search", "codebase_search",
}
_EDIT_TOOLS = {
    "write", "edit", "multiedit", "notebookedit", "apply_patch", "applypatch",
    "str_replace", "str_replace_editor", "create_file", "edit_file", "patch",
}
_SHELL_TOOLS = {"bash", "shell", "local_shell", "exec", "run_terminal_cmd", "terminal"}
_SUBAGENT_TOOLS = {"task", "agent", "dispatch_agent"}

# Read-ish shell commands (first token) — so Codex "shell cat foo" shows READING,
# not CODING. Local classification only; the command itself is discarded.
_READ_CMD_TOKENS = {
    "cat", "ls", "less", "more", "head", "tail", "grep", "rg", "find", "fd",
    "pwd", "echo", "wc", "which", "file", "stat", "tree", "bat", "sed", "awk",
    "cut", "sort", "uniq", "diff", "realpath", "dirname", "basename", "env",
}
_READ_GIT_SUBCMDS = {"status", "diff", "log", "show", "blame", "branch", "remote"}


def _tool_category(tool_name: str | None, command: str | None) -> str | None:
    """Map a tool name (+ optional shell command) to a server tool_category enum
    (read|edit|test|shell|mcp) — or ``None`` for a non-tool/subagent event."""
    if not tool_name:
        return None
    t = tool_name.strip().lower()
    if t.startswith("mcp__") or t.startswith("mcp."):
        # MCP web tools read; everything else counts as active work.
        if any(w in t for w in ("fetch", "search", "read", "get", "list")):
            return "read"
        return "mcp"
    if t in _SUBAGENT_TOOLS:
        return None  # handled as THINKING/subagent at the event layer
    if t in _READ_TOOLS:
        return "read"
    if t in _EDIT_TOOLS:
        return "edit"
    if t in _SHELL_TOOLS:
        return _classify_command(command)
    # Unknown tool: treat as active shell work (provider_normalization: "Other").
    return "shell"


def _classify_command(command: str | None) -> str:
    """Shell command -> test|read|shell (LOCAL ONLY; command is then discarded)."""
    if not command:
        return "shell"
    low = command.lower()
    # Test/build/lint category (reuse the server's keyword set).
    toks = _tokenize(low)
    if any(kw in toks for kw in S.DEFAULT_TEST_KEYWORDS):
        return "test"
    # Common test-runner invocations even without the bare keyword.
    if any(r in low for r in ("pytest", "jest", "vitest", "go test", "cargo test", "npm t ", "tox")):
        return "test"
    # Pure-read commands.
    first = toks[0] if toks else ""
    if first == "git" and len(toks) > 1 and toks[1] in _READ_GIT_SUBCMDS:
        return "read"
    if first in _READ_CMD_TOKENS:
        return "read"
    return "shell"


def _tokenize(s: str) -> list[str]:
    out, cur = [], []
    for ch in s:
        if ch.isalnum() or ch in "_-./":
            cur.append(ch)
        else:
            if cur:
                out.append("".join(cur))
                cur = []
    if cur:
        out.append("".join(cur))
    return out


# --------------------------------------------------------------------------- #
# Event name -> status. Keys are lowercased event names (case-tolerant).
# --------------------------------------------------------------------------- #
# SubagentStop/TaskCompleted -> DONE (doc-faithful). A late SubagentStop after a
# parent Stop keeps the session DONE (no resurrection); a brief DONE mid-parent-work
# is self-corrected by the parent's next event within seconds.
_DONE_EVENTS = {"stop", "sessionend", "subagentstop", "taskcompleted"}
_WAIT_EVENTS = {"permissionrequest", "permissiondenied"}
_THINK_EVENTS = {
    "userpromptsubmit", "precompact", "postcompact", "postcompaction",
    "subagentstart", "taskcreated",
}
_IDLE_EVENTS = {"sessionstart"}
_TOOL_EVENTS = {"pretooluse", "posttooluse", "posttoolbatch"}
_FAIL_EVENTS = {"posttoolusefailure", "stopfailure"}
_COMPACT_EVENTS = {"precompact", "postcompact", "postcompaction"}
# Only a notice that the agent is genuinely BLOCKED on the user lights WAITING
# (amber "ACTION REQUIRED"). `idle_prompt` = "finished, waiting for your next
# message" — that is NOT an approval request, so it stays calm (IDLE → sleep),
# never WAITING. (Boss 2026: several idle sessions falsely lit the orb amber.)
_WAIT_NOTIFICATION_TYPES = {"permission_prompt", "elicitation_dialog"}
_IDLE_NOTIFICATION_TYPES = {"idle_prompt"}


def _tool_failed(hook: dict) -> bool:
    """True iff a PostToolUse hook signals a tool failure. Codex has no separate
    PostToolUseFailure event, so PostToolUse is its ONLY failure signal."""
    for key in ("tool_result", "tool_response", "output", "result"):
        v = hook.get(key)
        if isinstance(v, dict):
            ec = v.get("exit_code", v.get("exitCode"))
            if isinstance(ec, (int, float)) and not isinstance(ec, bool) and ec != 0:
                return True
            if v.get("success") is False or v.get("is_error") is True or v.get("isError") is True:
                return True
            if isinstance(v.get("error"), str) and v.get("error").strip():
                return True
    if isinstance(hook.get("error"), str) and hook.get("error").strip():
        return True
    if hook.get("success") is False:
        return True
    return False


def _error_label_from_hook(hook: dict) -> str:
    """Derive a SANITIZED error_label category from a failed hook (raw text never
    survives — normalize_error_label drops anything path/secret-bearing to unknown)."""
    for src in (hook.get("error"),
                (hook.get("tool_result") or {}).get("error") if isinstance(hook.get("tool_result"), dict) else None,
                (hook.get("tool_response") or {}).get("error") if isinstance(hook.get("tool_response"), dict) else None):
        if isinstance(src, str) and src.strip():
            label = S.normalize_error_label(src)
            if label != "unknown":
                return label
    return "tool_error"


@dataclass
class NormalizeResult:
    action: str               # "post" | "heartbeat" | "drop"
    event: dict | None = None  # shorthand body for /admin/event when action == post
    diag: str = ""            # diagnostic label (event name etc.), never raw content


def _command_from_tool_input(tool_input) -> str | None:
    """Pull a shell command out of tool_input for LOCAL classification only.
    Handles Claude ({command: "..."}) and Codex ({command: ["bash","-lc","..."]})."""
    if not isinstance(tool_input, dict):
        return None
    cmd = tool_input.get("command")
    if isinstance(cmd, str):
        return cmd
    if isinstance(cmd, list):
        return " ".join(str(x) for x in cmd)
    return None


def _safe_account(raw: str, aliases, pepper: bytes) -> str:
    """Resolve the account label through the sanitizer: honor a neutral mapped
    value, else keep a neutral short label verbatim (``main``/``work``), else
    HMAC-collapse anything forbidden (email/plan-tier/path) to ``account-<hmac4>``.
    Mirrors the cwd path so the account can never be emitted raw."""
    mapped = aliases.account(raw)
    if mapped is not None and S.contains_forbidden(mapped) is None:
        return mapped
    if S.contains_forbidden(raw) is not None:
        return f"account-{S.hmac_label(pepper, raw, n=4)}"
    return S.coerce_alias(raw, pepper, prefix="account", n=4)


def normalize_record(record: dict, *, pepper: bytes, aliases) -> NormalizeResult:
    """Convert one queued hook record into a post/heartbeat/drop decision."""
    if not isinstance(record, dict):
        return NormalizeResult("drop", diag="non_object_record")
    provider_raw = str(record.get("provider", "")).strip().lower()
    provider = provider_raw if provider_raw in ("claude", "codex") else "manual"
    hook = record.get("hook")
    if not isinstance(hook, dict):
        return NormalizeResult("heartbeat", diag="non_object_hook")

    event_name = str(hook.get("hook_event_name") or hook.get("hookEventName") or "").strip()
    ekey = event_name.lower()

    # --- session id -> keyed HMAC label (the per-session fleet key) --------- #
    raw_sid = str(hook.get("session_id") or hook.get("sessionId") or "")
    sid_label = S.session_label(raw_sid or f"{provider}:nosession", pepper)

    # --- cwd -> project label ---------------------------------------------- #
    # Precedence: user alias map -> readable folder name (local mode) -> HMAC.
    raw_cwd = hook.get("cwd")
    if isinstance(raw_cwd, str) and raw_cwd:
        mapped = aliases.project(raw_cwd)
        if mapped is not None and S.contains_forbidden(mapped) is None:
            project = mapped
        elif config.LOCAL_LABELS and (_lab := _readable_label(raw_cwd)):
            project = _lab
        else:
            project = S.project_alias(raw_cwd, aliases, pepper)
    else:
        project = "—"

    # --- tool category (read tool_input locally, then discard) -------------- #
    # Defensive: a hostile/garbage hook may put a non-string in tool_name — coerce
    # so .strip() never raises and stalls the daemon (poison-record guard).
    raw_tool_name = hook.get("tool_name") or hook.get("toolName")
    tool_name = raw_tool_name if isinstance(raw_tool_name, str) else None
    command = _command_from_tool_input(hook.get("tool_input") or hook.get("toolInput"))
    tool_category = None

    base = {
        "provider": provider,
        # account resolved through the sanitizer too — a careless AGENTLAMP_ACCOUNT
        # (email / plan tier) collapses to a keyed label locally, never emitted raw.
        "account": _safe_account(config.ACCOUNT, aliases, pepper),
        "project": project,
        "provider_session_id": sid_label,
        "model": provider,
    }

    # --- map event -> status ----------------------------------------------- #
    if ekey in _FAIL_EVENTS:
        err = hook.get("error")
        error_label = S.normalize_error_label(err if isinstance(err, str) else None)
        base.update(status="ERROR", error_label=error_label, needs_attention=True)
        return NormalizeResult("post", base, diag=f"{provider}:{event_name}")

    if ekey in _WAIT_EVENTS:
        base.update(status="WAITING", needs_attention=True)
        return NormalizeResult("post", base, diag=f"{provider}:{event_name}")

    if ekey == "notification":
        ntype = str(hook.get("notification_type") or hook.get("notificationType") or "").strip().lower()
        if ntype in _WAIT_NOTIFICATION_TYPES:
            base.update(status="WAITING", needs_attention=True)
            return NormalizeResult("post", base, diag=f"{provider}:Notification:{ntype}")
        if ntype in _IDLE_NOTIFICATION_TYPES:
            # Finished, waiting for the user's next message → calm idle, not WAITING.
            base.update(status="IDLE")
            return NormalizeResult("post", base, diag=f"{provider}:Notification:{ntype}")
        # auth_success / elicitation_complete / unknown notice -> just heartbeat.
        return NormalizeResult("heartbeat", diag=f"{provider}:Notification:{ntype or 'unknown'}")

    if ekey in _TOOL_EVENTS:
        tl = (tool_name or "").strip().lower()
        # Codex PostToolUse is the ONLY failure signal (no PostToolUseFailure event).
        if ekey == "posttooluse" and _tool_failed(hook):
            base.update(status="ERROR", error_label=_error_label_from_hook(hook),
                        needs_attention=True)
            return NormalizeResult("post", base, diag=f"{provider}:PostToolUse:failed")
        if tl in _SUBAGENT_TOOLS:
            base.update(status="THINKING", status_detail="subagent")
            return NormalizeResult("post", base, diag=f"{provider}:{event_name}:subagent")
        tool_category = _tool_category(tool_name, command)
        status = S.TOOL_CATEGORY_STATUS.get(tool_category or "shell", "CODING")
        base.update(status=status)
        if tool_category:
            base["tool_category"] = tool_category
        if status in ("WAITING", "ERROR"):
            base["needs_attention"] = True
        return NormalizeResult("post", base, diag=f"{provider}:{event_name}:{tool_category}")

    if ekey in _DONE_EVENTS:
        base.update(status="DONE")
        return NormalizeResult("post", base, diag=f"{provider}:{event_name}")

    if ekey in _THINK_EVENTS:
        base.update(status="THINKING")
        if ekey in _COMPACT_EVENTS:
            base["status_detail"] = "compacting"
        elif ekey == "subagentstart":
            base["status_detail"] = "subagent"
        return NormalizeResult("post", base, diag=f"{provider}:{event_name}")

    if ekey in _IDLE_EVENTS:
        base.update(status="IDLE")
        return NormalizeResult("post", base, diag=f"{provider}:{event_name}")

    # Unknown / unhandled event: keep the session alive but do NOT clobber its
    # current status with UNKNOWN. Preserve the name for diagnostics.
    return NormalizeResult("heartbeat", diag=f"{provider}:UNHANDLED:{event_name or 'noname'}")
