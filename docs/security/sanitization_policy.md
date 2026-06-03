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

- Absolute or relative paths: `/Users/...`, `/home/...`, `C:\...`, `./src/...`, `../`, **any
  generic absolute POSIX path** (`/tmp/secret`, `/etc/passwd`, `/var/...`, `/opt/...` — a
  leading `/segment/` run), and a **leading `~` (tilde home)** (`~/secret`, `~root/x`). (The
  2026-06-03 hardening added the generic `/segment/` + `^~` patterns; the original scan only
  caught the home roots, Windows drives, and `./` `../`, so `/tmp/` `~` `/etc/` slipped through
  into leaf-scanned fields such as the quota `account_alias`.)
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

> ## 🚨 INVARIANT I1 — the cloud VALIDATES, it NEVER re-sanitizes
>
> The relay's "second, independent gate" is **VALIDATE-ONLY of the already-sanitized
> output**. The Python collector is the **only** place raw → safe heuristic redaction
> happens (NFKC normalization, zero-width / control-char stripping, HMAC aliasing,
> path / secret scrubbing). The Cloudflare Worker / Durable Object **must NOT re-run any
> of those transforms.** It only checks that the event the collector already produced is
> well-formed and free of leaks, then **rejects — never coerces** — anything that is not.
>
> Concretely, the cloud gate is exactly these six checks (all on the *post-sanitize*
> shape, all language-equivalent across Python and TypeScript; the data they read is the
> single source of truth in `../../tests/fixtures/parity/policy.json`):
>
> 1. **Key allowlist** — every envelope key ∈ `validate_envelope_keys`, every payload key
>    ∈ `validate_payload_keys`; recursive default-deny on any unknown key.
> 2. **Forbidden-key reject** — any key in `forbidden_keys`
>    (`cwd`, `prompt`, `transcript_path`, `command`, `content`, `tool_input`,
>    `tool_response`, `old_string`, `new_string`) rejects the whole event.
> 3. **Enum membership** — `provider` / `model` / `status` / `status_detail` /
>    `tool_category` / `task_label` / `error_label` / `confidence` must each be a literal
>    member of its enum. No normalization, no case-folding — a non-member is a reject.
> 4. **Neutral-alias shape** — `account_alias` / `project_alias` must match
>    `alias_shape_regex`; `display_title` must respect `title_max_len` and the
>    display-label / title regex. A prompt-like or path-like alias fails the *shape*
>    check (it does not get rewritten).
> 5. **Forbidden-pattern reject scan** — every string value is scanned against
>    `forbidden_patterns` + `model_id_regex` + `plan_tiers`; a hit rejects the whole
>    event (paths — incl. generic `/segment/` absolute POSIX paths and leading `~` —, URLs,
>    `sk-…`, Bearer / Cookie / JWT, private keys, emails, real model ids, plan-tier names).
> 6. **`provider_session_id` opaque-shape gate** — the session id becomes the materialized
>    session KEY, so a forbidden-pattern-clean free-text string (`please fix auth now`) must
>    not survive as that key. It must be the canonical opaque shape: the collector's `hmac:`
>    keyed label (`hmac:<alnum>`) or a high-entropy token (≥ 16 url-safe chars containing a
>    digit). Reject — never coerce. (The collector's sanitizer also HMAC-labels any
>    non-canonical session id, so the relay only ever sees canonical ids; this is the
>    backstop.)
>
> **Rationale (why validate-only, not re-sanitize):** re-deriving the collector's ~800
> lines of redaction heuristics in TypeScript would be **silently wrong**. JavaScript
> `RegExp` and JS Unicode handling are **not** equivalent to Python `re` +
> `unicodedata` — NFKC folding, `\p{...}` property classes, zero-width handling, and
> greedy/lazy edge cases all differ. A re-implementation that looks right would
> **under-redact in cases the Python tests never see**, and the leak would be invisible.
> Validation of an **enum-only, allowlisted** output, by contrast, *is* language-equivalent:
> "is this string exactly `claude`?" and "does this key appear in a fixed set?" behave
> identically in both runtimes. So the cloud's job is to be a strict bouncer for the
> collector's output — **never** a second redactor. It is a **NO-GO** if the relay ever
> accepts a raw `cwd` / `prompt` / `model` / path and tries to sanitize it in TS.
>
> Cross-language parity is enforced as a release blocker (invariant I2): both the Python
> tests and the TypeScript vitest assert against the identical corpora in
> `../../tests/fixtures/parity/` (`policy.json`, `hmac_vectors.json`,
> `sanitize_corpus.json`, `frame_vectors.json`). A mismatch fails the build; no deploy
> until both are green.

Operational requirements that follow from I1:

- **Validate-only**, against the shared `policy.json` — do **not** re-run the transforms.
- Reject (do not best-effort redact) any event that fails any of the five checks above.
- Store only the sanitized payload that passed validation.
- Keep raw rejected payloads out of persistent logs; log rejection metadata + payload hash.
- Reject any event still containing `transcript_path`, `cwd`, raw prompt/command text,
  paths, model ids, plan tiers, credentials, or unknown fields.

## Local-server `/admin/*` access control (LAN exposure)

The **local** FastAPI frame server binds `0.0.0.0` (the ESP32 device polls `/frame` across the
LAN), which also exposes the operator-only `/admin/*` routes (event injection, `set_quota`,
pairing-code issuance) to every host on the WiFi. Those routes are gated (2026-06-03 hardening):

- **Allow** a request whose client is loopback (`127.0.0.1` / `::1` / `::ffff:127.0.0.1`), **or**
- **allow** a request presenting a configured `AGENTLAMP_LOCAL_ADMIN_TOKEN` as a `Bearer` (a
  shared token an operator can provision to drive `/admin/*` from another box; constant-time
  compared), **else 403** `{"error":"admin_forbidden","retry":false}`.
- The **device path is unaffected**: `/api/v1/device/{id}/frame` + `/pair` keep their own bearer /
  pairing-code auth and are never subject to the loopback gate.
- An in-process ASGI test client (synthetic `testclient` host) is treated as loopback so tests and
  same-process tooling work without a token.

**Choice / scope:** this is a coarse *network* gate for the LAN-only local server, **not**
per-user auth. The strong `/admin` authentication (Cloudflare Access / MFA / TOTP, per the relay
build-spec §Auth model) lives at the **relay edge**, where `/admin` + enroll are reachable from
outside a trusted LAN. The local server's threat model is "anyone already on my home WiFi"; a
loopback-or-shared-token gate closes the unauthenticated-LAN-peer hole without forcing an ESP32
secret rotation onto the device contract.

## Required Fixtures (proof the mechanism works)

`provider_sanitization_fixtures.md` must include, at minimum:

- `unmapped_cwd` → emits HMAC `project-xxxxxx`, **asserts basename never appears**.
- `low_entropy_branch` → emits HMAC label, **asserts plain `sha256("main")` is not produced**.
- `plan_tier_account` → `Claude Max` input → emits generic alias, never `Max`.
- `real_model_id` → `claude-opus-4-…` input → collapses to `claude`.
- `error_with_path` → error message containing a path → drops to `error_label: unknown`.
- `free_text_task` → arbitrary prompt fragment → maps to a controlled `task_label` or `unknown`.
- `unknown_field` → event with an extra key → whole event rejected.
