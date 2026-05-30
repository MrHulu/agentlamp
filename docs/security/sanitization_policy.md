# Sanitization Policy

## Principle

Default deny. The collector may emit only fields explicitly allowed by this document,
and only in the **shapes** allowed here. "Useful but risky" fields are not truncated —
they are turned into an enum, a user-controlled alias, or a keyed hash.

> This policy is the product's entire trust claim. It is not enough to list what is
> forbidden; this document specifies the **mechanism** that produces a safe value from a
> raw one. An unconfigured machine must emit nothing human-readable. See `fixtures` below.

## The Alias Mechanism (how raw → safe actually happens)

A hook fires from an arbitrary `cwd` the collector has never seen. The collector MUST NOT
guess a human-readable label from it (no directory basename, no path segment). Instead:

1. **Local alias map (user-maintained, never uploaded).** A local config file maps
   raw signals to display aliases:
   ```toml
   # ~/.config/agentlamp/aliases.toml  (LOCAL ONLY — never leaves the machine)
   [projects]
   "/Users/hulu/work/acme"   = "project-a"
   "/Users/hulu/side/blog"   = "project-b"
   [accounts]
   "default"                 = "main"          # keyed by collector-local account key
   "work"                    = "work"
   ```
2. **No match → keyed opaque label, never a guess.** If a `cwd`/account/branch has no
   mapping, the collector emits a deterministic opaque label derived by **keyed HMAC**
   (see below): `project-<first6(hmac)>` e.g. `project-7f3a9c`. It is stable across
   sessions (same input → same label) but reveals nothing.
3. **The basename rule (hard invariant).** The collector MUST NEVER emit a directory
   basename, repo slug, or any substring of a path as an alias. Fixture `unmapped_cwd`
   proves an unmapped `cwd` emits an HMAC label, not its basename.

## Keyed Hashing (plain SHA256 is reversible for low-entropy values)

Branch names, project names, and many session/account ids are **low-entropy**. A relay
operator (the explicit attacker in relay mode) brute-forces `sha256("main")` or
`sha256("feature/login")` from a tiny wordlist instantly. Therefore:

- Any hashed identifier uses **HMAC-SHA256 with a per-collector secret pepper** stored in
  the OS keyring and **never uploaded**: `label = first_n(HMAC_SHA256(pepper, raw))`.
- Plain SHA256 is allowed **only** for already-high-entropy opaque ids (e.g. a 128-bit
  provider session token) where brute force is infeasible — and even then HMAC is preferred.
- The pepper is generated locally on first run. Rotating it re-labels everything (acceptable).

## Allowed Field Classes

| Class | Shape | Rule |
|-------|-------|------|
| Provider identity | enum `codex` \| `claude` \| `manual` | controlled enum, nothing else |
| Account alias | user-mapped alias or HMAC label | generic only (`main`, `work`, `account-7f3a`); **never** the plan tier |
| Session id | HMAC label or high-entropy opaque id | no path, no email, no org id |
| Project alias | user-mapped alias or HMAC label | never a path segment / basename |
| Status | enum (see `device_frame_api.md`) | controlled enum |
| Status detail | enum `compacting`\|`tool_running`\|`subagent`\|`unknown` | optional; controlled enum, **not** free text |
| Tool category | enum `read`\|`edit`\|`test`\|`shell`\|`mcp`\|`approval`\|`error` | controlled enum |
| Task label | enum from controlled vocabulary (below) | **not** free text |
| Error label | enum category (below) | **not** a raw message |
| Model | enum `codex` \| `claude` \| `manual` \| `unknown` | **never** the real model id |
| Quota ratio | float 0..1 | no raw billing HTML |
| Confidence | enum `high`\|`medium`\|`low`\|`unknown` | required for inferred quota |
| Timestamps | Unix seconds | — |
| Event hashes | HMAC/SHA256 hashes | dedupe/audit only |

**Non-sensitive transport/envelope metadata** is also allowed (it carries no user content):
`schema_version`, `event_id`, `event_type`, `provider_event_name`, `adapter`,
`adapter_version`, `source_seq`, `batch_id`, `dedupe_key`, `turn_id` (HMAC label),
`needs_attention` (bool), `event_time`/`updated_at`/`started_at` (timestamps). The recursive
unknown-field rejection treats these as known; **any field not in this table or this list
rejects the event.**

## Controlled Vocabularies (free text is a leak channel — eliminate it)

A 160-char limit does not close a leak: a 159-char prompt fragment still leaks. These
fields are **enums**, not truncated strings.

**`task_label`** — one of, derived from tool category + status, or manual selection only:
`implementing` \| `debugging` \| `testing` \| `reviewing` \| `refactoring` \| `reading` \|
`planning` \| `waiting` \| `idle` \| `unknown`.
There is **no** free-text task summary in v1. A human-typed label is allowed only if the
user selects from this list.

**`error_label`** — category only, never a message:
`rate_limit` \| `timeout` \| `permission` \| `api_error` \| `tool_error` \| `network` \| `unknown`.
Any candidate error string containing `/`, `\`, `:`, `@`, `sk-`, or a 6+ char run that
looks like a path/identifier is dropped to `unknown`.

**`model`** — never the provider's real model id (`claude-opus-4-…`, `ft:gpt-4o:acme:…`
reveals plan tier, fine-tuning, and client). Collapse to the provider enum above.

**`account_alias`** — never echo the plan name. `Claude Max`/`Claude Team`/`Pro` reveal
billing tier (identifying). Use `main`/`work`/`claude-1` or an HMAC label.

## Forbidden Patterns (cloud also re-checks, see Cloud Requirements)

Reject the **whole event** (do not "best-effort redact") if any field matches:

- Absolute or relative paths: `/Users/...`, `/home/...`, `C:\...`, `./src/...`, `../`.
- URLs, git remotes, hostnames carrying org/user/repo names (unless pre-aliased).
- API keys, tokens, cookies, bearer strings, JWT-like strings, `sk-…`.
- SSH/private key blocks.
- Email addresses, account ids, org ids, workspace ids, ticket ids.
- Real model identifiers (anything outside the `model` enum).
- Plan/tier names (`Max`, `Team`, `Pro`, `Enterprise`, `Plus`).
- Prompt/transcript text of any length (not just >160 chars).
- Source snippets (code-like density).
- Provider hook fields named `cwd`, `transcript_path`, `prompt`, `tool_response`,
  `content`, `old_string`, `new_string`, `tool_input`, or raw `command`.
- **Unknown fields** — recursive default-deny: any key not in the schema rejects the event.

## Cloud-Visible Data Inventory (what the relay actually sees)

Even fully sanitized, relay mode uploads behavioral metadata. This is the complete list
of what a relay operator (or a DB dump) can observe — it is the input to `threat_model.md`:

| Visible | Inference an attacker can draw |
|---------|--------------------------------|
| event timestamps | your daily work schedule, active hours |
| count of distinct `project_alias` | how many projects/clients you juggle |
| count of distinct `account_alias` | how many accounts you run |
| session start/stop cadence | productivity rhythm, session lengths |
| status distribution | how much time coding vs waiting |
| quota ratios over time | per-account burn rate |

Mitigations and the explicit honest-but-curious-operator model live in `threat_model.md`
(upload jitter/batching, retention purge, local-mode-by-default). Local mode exposes
**none** of this to any third party.

## Collector Requirements

- Sanitize before signing / before serving a frame.
- Attach `sanitization.policy_version`.
- Record redaction count locally (counts only, never the redacted value).
- Refuse to emit an event if a mandatory field cannot be safely produced.
- Codex and Claude adapters must pass `provider_sanitization_fixtures.md` before enable.

## Cloud Requirements (relay mode)

- Run a **second, independent** sanitization gate identical to the collector's
  (same forbidden-pattern table + enum validation + recursive unknown-field rejection).
- Validate schema, enums, and field lengths.
- Store only the sanitized payload.
- Keep raw rejected payloads out of persistent logs; log rejection metadata + payload hash.
- Reject any event still containing `transcript_path`, `cwd`, raw prompt/command text,
  paths, model ids, plan tiers, credentials, or unknown fields.

## Required Fixtures (proof the mechanism works)

`provider_sanitization_fixtures.md` must include, at minimum:

- `unmapped_cwd` → emits HMAC `project-xxxxxx`, **asserts basename never appears**.
- `low_entropy_branch` → emits HMAC label, **asserts plain `sha256("main")` is not produced**.
- `plan_tier_account` → `Claude Max` input → emits generic alias, never `Max`.
- `real_model_id` → `claude-opus-4-…` input → collapses to `claude`.
- `error_with_path` → error message containing a path → drops to `error_label: unknown`.
- `free_text_task` → arbitrary prompt fragment → maps to a controlled `task_label` or `unknown`.
- `unknown_field` → event with an extra key → whole event rejected.
