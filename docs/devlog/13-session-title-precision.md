# 13 — R4/TASK-012: per-session precision via session titles

> 2026-06-02. Operator asked "why do several sessions just show `ai-center`? can't it be
> more precise?" Answer + chosen direction (a 3-lens design spike, then Boss picked the
> session-title option): **name a session and the lamp shows the name instead of the folder**,
> so same-folder sessions become distinguishable.

## Why it collapsed before

The fleet groups by **project** (folder basename) — TASK-005's "which project, how many,
doing what" axis. N sessions in one folder → one `ai-center ×N` row. The only per-session id
kept is a privacy-safe HMAC (meaningless to a human). The lamp also fundamentally can't map a
row to a specific terminal window (no external reference) — so the achievable precision is
"name your work," not "address terminal #3."

## What shipped (collector + server only — NO firmware change / no reflash)

A session **title** is the one per-session human-meaningful field that's both available and
sanitizable. Verified before building (Boss's "先验证"):
- **Official Claude doc + empirical capture**: a real `claude --name "x"` session delivers
  `session_title: "x"` on its SessionStart (and UserPromptSubmit) hook. (Codex carries no
  title — confirmed; it falls back to the project label.)
- **Security**: that same UserPromptSubmit payload also carries a raw `prompt` — normalize
  reads **only** `session_title`, never `prompt` (test guards this).

Data flow:
- `normalize.py` → reads `hook['session_title']` into `base['session_title']` (raw; ignores `prompt`).
- `app.py::_to_envelope` → maps it into `payload['session_title']`.
- `sanitize.py` → `session_title` added to `_KNOWN_PAYLOAD_KEYS`; **exempted from the raw
  `_scan_leaves`** (like `model`) because the new **`safe_title()`** is its dedicated gate:
  drops (→ None) any title with a forbidden pattern (path/email/secret/token — never "cleaned"),
  else normalizes to a bounded (28-char) lowercase kebab; **local mode → readable verbatim**,
  **relay mode → `title-<hmac>`** (a real title never reaches a relay). Emits `sp['display_title']`.
- `state.py` → `Session.display_title`; **preserved across events that omit it** (the title
  rides on SessionStart/UserPromptSubmit, not tool events, so a later tool event must not blank
  it); `_display_label(s) = display_title or project_alias` drives the fleet **row label** and
  the focus **project** field. Named sessions surface individually; unnamed aggregate by project.

No firmware change: the device already renders whatever label string the server sends (41-byte
buffer + ellipsize from devlog 12), so titles "just render."

## Operator usage

```
claude --name "rag-pipeline"     # or /rename inside a session
```
→ the lamp shows `rag-pipeline` instead of the folder. Two named sessions in the same folder
become two distinct rows. Unnamed sessions still aggregate as `<folder> ×N`.

## Verification

- 50 collector + 121 server tests green (new: title extracted-not-prompt, Codex-no-title;
  title-replaces-label, same-folder-split, preserved-across-events, unsafe-title-dropped,
  relay-HMAC).
- `safe_title` adversarial self-test: path / email / `sk-…` token / `../../etc/passwd` /
  overlong / blank → all dropped or bounded; no leak in any output.
- **Live end-to-end**: a real `claude --name "live-title-test"` session rendered on the orb
  frame as `primary.project = live-title-test` + a `live-title-test` fleet row (folder was
  `agentlamp-title-demo`), distinct from the concurrent `ai-center` session.

## Privacy leak-review (2-lens adversarial, before sign-off)

Because the title is the one free-text field crossing the sanitizer (and the one exempted
from the raw leaf scan), a dedicated leak-hunt review ran. It found a **real HIGH leak my own
self-test missed**: `safe_title` scanned `contains_forbidden(raw)` *before* normalization, but
the forbidden patterns rely on character adjacency — injecting a separator (`sk​-KEY`
zero-width, or `sk -KEY` / `sk.KEY` / `sk_KEY`) bypassed the scan, then `_TITLE_CLEAN_RE`
collapsed the separator back into `-`, **reconstructing** the token → leaked verbatim in local
mode (verified end-to-end: `primary.project = sk-livekey…`). Plus a MEDIUM: relative paths /
emails (`Users/bob/x`, `john@x`) whose `/` `@` adjacency normalization erases.

Fixed at two layers (systemic, not symptom):
- `contains_forbidden` now scans the raw value AND an **invisibles-stripped (NFKC + drop
  Cc/Cf) copy** — closes the zero-width class for ALL fields (also the LOW finding that
  `provider_session_id` could carry `/Use​rs/…` past the leaf scan).
- `safe_title` now: NFKC-folds; **rejects** any title with a zero-width/control/format char;
  **hard-rejects** `/` `\` `@`; scans the raw; normalizes; **re-scans the normalized label**
  (catches benign-separator reconstruction); then local-readable / relay-HMAC.

Verified: all 11 confirmed attack vectors → dropped (0 leaks); legit titles still readable;
26 new regression tests (injection-drops-local / legit-keeps / never-readable-in-relay).

LOW findings accepted as residual (documented, not blocking): empty `provider_session_id` key
collapse (the daemon never emits empty; needs a malicious loopback POST); high-cardinality
titles fragment the fleet (bounded by the 5-row cap + `fleet_more`); a set title can't be
cleared by an empty `/rename` (minor UX — rename to a new name instead).

## Limits (honest)

- Codex sessions have no title → still show the folder (its only extra per-session signal is
  `permission_mode`; not surfaced).
- Relay mode shows `title-<hmac>` (opaque) by design — readable titles are local-mode only.
- The lamp still can't map a row to a specific terminal; titles are the operator's lever.
- Rejected for now (per spike): short-HMAC discriminator (meaningless), auto-rotating focus
  (reintroduces flicker). R5 status-mix (`ai-center ×5 · 3C 2R`) is the complementary next step.
