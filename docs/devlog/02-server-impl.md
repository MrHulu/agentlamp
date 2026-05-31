# Devlog 02 — Collector + Local LAN Frame Server (local mode, no cloud)

**Date:** 2026-05-30
**Agent role:** Server implementer (AgentLamp)
**Goal:** Build the collector + **local LAN frame server** as a runnable Python package
under `server/agentlamp_server/`, serving the device frame API contract over the LAN with
**no cloud** — plus the default-deny sanitizer mechanism, aggregation/priority/frame
generation, a live browser simulator, and the Required Fixture tests.
**Outcome:** SUCCESS — `python -m agentlamp_server` serves valid schema-v1 frames; 85/85
tests pass; live curl captured below; nothing committed.

> Builds on Devlog 01 (toolchain + scaffold). The scaffold `app.py` (auth + schema
> negotiation only, static frame) is replaced here by the real generator.

---

## 1. Architecture

Local mode means the collector itself owns aggregation + display priority + frame
generation and serves the frame over the LAN — **no domain, no public TLS, no cloud
account, no HMAC ingest hop** (`docs/architecture/architecture.md` → Local mode /
Ownership Boundaries). The cloud `cloud_contract.md` priority + frame-generation rules are
the single source of truth and are reused verbatim here.

```
provider event envelope  (admin/event inject, or future Codex/Claude hook adapter)
        │
        ▼
  sanitize.py   ── default-deny: enum | user-alias | keyed-HMAC; recursive unknown-field reject
        │            (rejects whole event on any forbidden pattern; counts-only audit)
        ▼
  state.py      ── materialized sessions + quota; liveness TTL (STALE 120s / OFFLINE 600s);
        │            priority scoring (cloud_contract.md); scene selection; 2 KB trim
        ▼
  app.py        ── FastAPI: GET /frame (Bearer auth) · POST /pair · POST /admin/* · GET /preview
        │
        ▼
  preview.py    ── live 172×320 simulator, renders the EXACT frame JSON, polls every 3 s
```

**Module boundaries (single-responsibility):**

| Module | Owns | Does NOT own |
|--------|------|--------------|
| `sanitize.py` | enum coercion, alias map, keyed HMAC, forbidden-pattern scan, recursive unknown-field rejection | HTTP, state, scoring |
| `state.py` | sessions/quota materialization, liveness TTL, priority, scene selection, frame build, 2 KB cap, device-token hash + pairing-code store | sanitization rules (delegates to `sanitize`), HTTP |
| `app.py` | FastAPI routes, Bearer auth, schema negotiation, error envelope, admin injection → envelope wrap | scoring/scene logic (delegates to `state`) |
| `preview.py` | self-contained simulator HTML (no external CDNs) | server logic |

**Why in-memory state (not the full `architecture.md` storage model):** the MVP build
order (`architecture.md` → MVP Build Order #2) is "local frame server + mock state +
browser simulator (no cloud)". Persistence (`collector_events`, retention purge,
`device_feed`) is a documented later step; the materialized model here is rebuildable from
events exactly as the State Rules require, so swapping the in-memory dicts for a store is a
drop-in later.

---

## 2. Every file created / changed

| File | Lines | Purpose |
|------|------:|---------|
| `server/agentlamp_server/__init__.py` | 12 | package marker; bumped `__version__` `0.0.1` → `0.1.0` |
| `server/agentlamp_server/__main__.py` | 7 | **new** — `python -m agentlamp_server` entry → `app.main()` |
| `server/agentlamp_server/sanitize.py` | 648 | **new** — default-deny sanitizer mechanism (alias map + keyed HMAC + enums + recursive reject) |
| `server/agentlamp_server/state.py` | 562 | **new** — aggregation, liveness TTL, priority, scene selection, frame build, 2 KB cap, device auth + pairing |
| `server/agentlamp_server/app.py` | 260 | **rewritten** — full FastAPI app (frame / pair / admin / preview) wired to `state` + `sanitize` |
| `server/agentlamp_server/preview.py` | 176 | **new** — live 172×320 simulator HTML (design borrowed from `docs/ui/mockups/scenes.html`) |
| `server/pytest.ini` | 5 | **new** — `pythonpath=.`, `testpaths=tests` |
| `server/tests/__init__.py` | 0 | **new** — makes `tests` a package (relative import of `conftest`) |
| `server/tests/conftest.py` | 46 | **new** — `sys.path` bootstrap, fixed test pepper, alias map, TestClient fixture |
| `server/tests/test_sanitization_fixtures.py` | 292 | **new** — all Required Fixtures + global rejection cases |
| `server/tests/test_frame.py` | 307 | **new** — frame schema v1, 2 KB cap, caps, priority/scene, liveness, seq |
| `server/tests/test_api.py` | 133 | **new** — HTTP auth/errors/pairing/admin/preview |

Total ~2,443 lines. **Nothing committed** (`git status` still shows `server/` untracked).

---

## 3. Contract conformance — how each clause was honored

### 3.1 `device_frame_api.md` — Frame Schema v1

The served frame has **exactly** the v1 keys: `v, device_id, scene, headline,
primary{provider,account,status,project,task}, fleet[], quota[], accent, ttl, seq,
server_time` (a `fleet_more` overflow count is added only when fleet > 6, which the device
ignores as an unknown field — per "device MUST ignore unknown fields").

- **Bearer auth, token never in URL** — `Authorization: Bearer <token>` only; a
  `?token=…` query string does **not** authenticate (test `test_token_never_in_url`).
  Token stored as a SHA-256 **hash** (`state._hash_token`), never the token itself
  (`pairing_and_auth.md`).
- **Schema negotiation** — server responds `min(server_supported=1, requested)` and echoes
  `X-Frame-Schema-Version` + `"v"` in the body.
- **Error envelope** — `{"error","retry"}` per the table: `401 bad_token/false`,
  `404 unknown_device/false`, `503 frame_unavailable/true`. Auth precedence: bad token →
  401 before unknown device → 404.
- **Array caps** — `fleet ≤ 6` (truncate lowest priority, overflow → `fleet_more`),
  `quota ≤ 2` (top-2 risk).
- **2 KB hard cap** — frame trimmed server-side before send (`_enforce_byte_cap`): drop
  quota first, then fleet rows from the tail, accumulating into `fleet_more`.
- **Provider display label** — `primary`/`fleet` `provider` is Title-case (`Codex`,
  `Claude`) mapped from the lowercase wire enum; `account`/`project` carry the lowercase
  sanitized alias verbatim.
- **`confidence` integer** — quota `confidence` maps `high→3 / medium→2 / low→1 /
  unknown→0`.
- **`seq` increments only on content change** — a content signature (everything except
  `server_time`/`seq`) gates the increment (`State Rules`: "Frame sequence increases only
  when rendered content or scene changes").

### 3.2 `cloud_contract.md` — Priority Rules (verbatim)

Base scores `WAITING +100 / ERROR +90 / CODING +70 / THINKING +65 / TESTING +60 /
READING +55 / DONE +20 / IDLE +0`; modifiers `low-quota +30 / pinned +50 / stale>10min
−20`. `UNKNOWN` scores `+0` like IDLE and is never a distinct scene. `OFFLINE`/`STALE` are
liveness states scored low so a live session wins focus. Codex + Claude share one priority
queue; provider is display metadata, not a scene.

### 3.3 Scene selection (`display_spec.md` + Frame Generation Rules)

Precedence: collector-heartbeat-lost → `offline` → `alert` (WAITING / ERROR / quota
≥ 90 %) → all-offline → top-`stale` → all-idle/done → `sleep` → else `focus`. Alert
interrupts all normal scenes; offline/stale preempt normal scenes.

### 3.4 Status → accent (from `docs/ui/mockups/scenes.html` palette)

`idle→blue, thinking→purple, coding→purple, reading→cyan, testing→green, waiting→yellow,
done→green, error→red, offline→muted, stale→white, unknown→muted`. Quota-danger alert
forces `red`. (The accent enum has 8 members; thinking/coding both map to the mockup's
purple family `#6d6bff`/`#a06bff`.)

### 3.5 Liveness (`architecture.md` → Session Lifetime / Liveness)

`STALE_AFTER_S = 120`, `OFFLINE_AFTER_S = 600`, `COLLECTOR_HEARTBEAT_STALE_S = 90`. A
session past its window is downgraded in `_effective_status` so a dead session can never
render as active. Tested with a monkeypatched clock (`test_stale_after_120s`,
`test_offline_after_600s`, `test_collector_heartbeat_lost_is_offline`).

### 3.6 `pairing_and_auth.md` — local mode

The local frame server plays the cloud's pairing/auth role:
`POST /api/v1/device/{id}/pair` exchanges a **one-time, burn-on-use** code (≤ 10 min TTL)
for the device token; `POST /admin/device/{id}/code` is the local-CLI role that issues the
code. `device_id` is validated against `^[A-Za-z0-9_-]{1,64}$`. The dev device `orb-01`
also returns its known token for a frictionless first-run (documented stub).

---

## 4. The sanitizer mechanism (the product's trust claim)

`sanitize.py` implements **default-deny**: a raw signal becomes a controlled enum, a
user-controlled alias, or a keyed-HMAC label — never a guess.

- **Alias map** (`AliasMap`, loaded from `~/.config/agentlamp/aliases.toml` via stdlib
  `tomllib`, LOCAL ONLY): raw `cwd`/account → neutral alias. The mapping **value** itself
  is validated neutral (cannot smuggle a plan tier / path / email past the policy).
- **Keyed HMAC** (`hmac_label = first_n(HMAC_SHA256(pepper, raw))`): unmapped `cwd` →
  `project-<hmac6>`; unmapped account → `account-<hmac4>`; low-entropy branch →
  `branch-<hmac6>`; session id → `hmac:<hmac12>`. **Never plain `sha256("main")`** (a relay
  operator brute-forces that instantly). The pepper is per-collector, generated locally,
  never uploaded (env `AGENTLAMP_PEPPER_HEX` or a per-process random 32 bytes for the
  local server).
- **Basename rule (hard invariant)**: `project_alias` never emits a directory basename,
  repo slug, or path substring — `test_unmapped_cwd_emits_hmac_not_basename` asserts every
  path segment is absent from the label.
- **Enum-only fields**: `status / status_detail / tool_category / task_label / error_label /
  model / provider / confidence` are coerced to controlled vocabularies. Free text never
  survives: a prompt fragment maps to a `task_label` member or `unknown`; a real model id
  collapses to the provider enum; an error string with a path/secret drops to
  `error_label: unknown`.
- **Recursive default-deny**: `reject_unknown_fields` rejects the **whole event** on any
  key not in the known envelope/payload sets, and hard-rejects the explicit raw-leak key
  names (`cwd`, `transcript_path`, `prompt`, `tool_input`, `command`, …).
- **Leaf scan**: every string leaf is scanned for forbidden patterns (paths, URLs, git
  remotes, `sk-`, Bearer, Cookie, JWT, private-key blocks, emails, plan tiers, real model
  ids, code density). The **only** exception is `payload.model`, which legitimately
  *collapses* a real model id to the enum (so it must not reject the event); every other
  field — including `error_label` and `task_label` — is fully scanned.
- **Counts-only audit**: `SanitizationError` carries a reason + payload **hash**, never the
  offending value; `FrameState` records `redaction_count` / `rejection_count`.

---

## 5. Test results (real pytest output)

Command: `/Users/hulu/huluman/agentlamp/.venv/bin/pytest -q` (run from `server/`).

```
.........................................................................[ 84%]
.............                                                            [100%]
85 passed in 0.20s
```

Verbose run header: `platform darwin -- Python 3.14.3, pytest-9.0.3, pluggy-1.6.0 …
rootdir: /Users/hulu/huluman/agentlamp/server  configfile: pytest.ini  collected 85 items`.

**Required Fixtures (`sanitization_policy.md` → Required Fixtures) — every one present and
passing:**

| Fixture | Test(s) | Asserts |
|---------|---------|---------|
| `unmapped_cwd` | `test_unmapped_cwd_emits_hmac_not_basename` | emits `project-<hmac6>`; **basename + every path segment absent** |
| `low_entropy_branch` | `test_low_entropy_branch_uses_hmac_not_plain_sha256[main\|feature/login\|develop]` | keyed HMAC; **`≠ plain sha256("main")`** |
| `plan_tier_account` | `test_plan_tier_account_mapped_to_generic`, `…_unmapped_account_hmac_never_tier`, `…_as_alias_value_is_rejected`, `test_event_with_plan_tier_leaf_rejected` | `Claude Max` → generic / HMAC; **never `Max`** |
| `real_model_id` | `test_real_model_id_collapses[10 cases]`, `test_event_with_real_model_id_in_model_field_collapses` | `claude-opus-4-…`/`ft:gpt-4o:acme:…` → `claude`/`codex`; **real id absent** |
| `error_with_path` | `test_error_with_path_drops_to_unknown[4 cases]`, `test_event_with_path_in_error_label_rejected_or_dropped` | path/secret error → `unknown` / event rejected |
| `free_text_task` | `test_free_text_task_maps_to_controlled_label[5 cases]`, `test_task_label_derived_from_tool_category` | fragment → controlled `task_label`/`unknown`; **fragment absent** |
| `unknown_field` | `test_unknown_top_level_field_rejected`, `test_unknown_payload_field_rejected`, `test_forbidden_raw_key_rejected` | extra/raw-leak key → **whole event rejected** |
| `stable_label` | `test_stable_label_deterministic` | same input → identical HMAC; rotate pepper → relabels |

**Frame-size < 2 KB:** `test_frame_under_2kb_normal`, `test_frame_under_2kb_with_overflow`
(12 sessions + 4 quota windows → still < 2048 B with caps), `test_fleet_capped_to_6…`,
`test_quota_capped_to_2_top_risk`.

**Frame schema:** `test_frame_schema_v1_shape` (exact key set, enum membership, types,
Title-case provider, verbatim lowercase alias), `test_frame_serializes_to_json`,
`test_schema_negotiation_min`.

**Priority / scene / liveness / seq:** waiting→alert(yellow), error→alert(red),
coding→focus(purple), waiting beats coding, quota-danger→alert(red), all-idle→sleep,
empty→sleep, stale@120s(white), offline@600s(muted), heartbeat-lost→offline, seq stable on
no-change, seq++ on scene change.

**HTTP API (TestClient):** bearer required (401), bad token (401), unknown device (404),
ok + header echo + <2 KB wire, schema negotiation header, admin event drives frame, admin
event rejects leak (422), admin quota drives red alert, reset, pair stub returns token,
real burn-on-use code flow, bad device id (404), preview HTML served, healthz, token never
in URL.

---

## 6. Live server proof (real `curl` against `python -m agentlamp_server`)

Started: `AGENTLAMP_LOCAL_BIND=127.0.0.1:8799 AGENTLAMP_PEPPER_HEX=<rand>
.venv/bin/python -m agentlamp_server` → log: `Uvicorn running on http://127.0.0.1:8799`.
(Bound to loopback for the capture; production default is `0.0.0.0:8787` per the env file.
`curl --noproxy '*'` was needed only because the agent shell routes through an HTTP proxy.)

```
$ curl --noproxy '*' http://127.0.0.1:8799/healthz
{"ok":true,"service":"agentlamp-frame-server","v":1}                       HTTP 200

$ curl --noproxy '*' http://127.0.0.1:8799/api/v1/device/orb-01/frame      # no token
{"error":"bad_token","retry":false}                                        HTTP 401

# inject state
POST /admin/event {provider:claude,account:work,status:CODING,project:project-a}   200
POST /admin/event {provider:codex,account:main,status:WAITING,project:project-a}   200
POST /admin/quota {provider:codex,account:main,window_type:5h,used_ratio:0.72}     200
```

**The live authed frame** (`-H 'Authorization: Bearer dev-local-token'
-H 'X-Frame-Schema-Version: 1'`) — HTTP 200, header `x-frame-schema-version: 1`,
**438 bytes** (< 2048):

```json
{
  "v": 1,
  "device_id": "orb-01",
  "scene": "alert",
  "headline": "ACTION REQUIRED",
  "primary": {
    "provider": "Codex",
    "account": "main",
    "status": "WAITING",
    "project": "project-a",
    "task": "waiting"
  },
  "fleet": [
    {"provider": "Codex", "count": 1, "status": "WAITING"},
    {"provider": "Claude", "count": 1, "status": "CODING"}
  ],
  "quota": [
    {"provider": "Codex", "account": "main", "w5": 0.72, "confidence": 2, "estimated": true}
  ],
  "accent": "yellow",
  "ttl": 5,
  "seq": 3,
  "server_time": 1780145597
}
```

This matches the `device_frame_api.md` schema-v1 example structurally clause-for-clause
(WAITING interrupts to the `alert` scene with the `ACTION REQUIRED` headline and `yellow`
accent; Codex priority `+100` beats Claude's `+70` for `primary`; quota carries the
`confidence: 2` integer and `estimated: true`).

**Other endpoints verified live:** pair stub returns `{"device_token":"dev-local-token"}`;
the burn-on-use code flow (`/admin/device/orb-02/code` → `/pair`) returns `tok-orb-02` then
`400 bad_pairing_code` on reuse; unknown device → 404; `?token=` in URL → 401;
`/admin/event` with a `/Users/…` path → `422 {"rejected":true,"reason":"forbidden:/Users/",
"payload_hash":"6f226fec…"}` (counts-only, no leaked value); `/preview` → 11,934 B HTML.
Server stopped cleanly (`kill`), port freed.

---

## 7. Live simulator (`/preview`)

`GET /preview?device=<id>&token=<tok>` serves a self-contained 172×320 simulator (no
external CDNs) that renders from the **exact** frame JSON, polling `/frame` every 3 s
(`display_spec.md` → Browser Simulator). It satisfies the spec requirements: renders from
the real frame JSON, shows the payload byte size with an **over-2 KB warning**, highlights
stale/expired TTL (grayscale on `stale`/`offline` scenes), and exposes inject buttons
(`/admin/event`, `/admin/quota`, `/admin/reset`) to drive every scene. The device chrome,
palette, and per-scene layouts are borrowed from `docs/ui/mockups/scenes.html`. The full
frame JSON is also printed in a side panel for screenshot-based regression.

---

## 8. Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | In-memory materialized state (dicts), not a DB | MVP build order #2 is "local server + mock state"; State Rules require state be rebuildable from events — it is. DB/retention purge is a documented later step. |
| 2 | `payload.model` is the **only** leaf-scan exemption | Policy says model ids *collapse* to the enum; every other field (incl. `error_label`/`task_label`) is fully scanned and a leak there rejects the whole event. |
| 3 | Admin shorthand wrapped into a full provider envelope | So injected events run through the **same** default-deny sanitizer as a real adapter event — no bypass path to state. |
| 4 | Two purple accents (thinking + coding) | The accent enum has no separate "indigo"; the mockup palette puts both in the purple family. Kept faithful to the design board. |
| 5 | Local-mode pairing stub returns the dev token for `orb-01` | Frictionless first-run without a separate CLI step; the real burn-on-use code path is fully implemented and tested for any other device. |
| 6 | `code_density ≥ 2 braces/semicolons OR a newline` rejects | Implements the fixtures-doc "code body with multiple braces/semicolons/line breaks" rule without flagging a normal one-token alias. |
| 7 | Loopback bind for the curl capture | The agent shell proxies outbound HTTP; `0.0.0.0:8787` remains the documented production/LAN default (matches `secrets.h` `FRAME_BASE_URL`). |

---

## 9. Problems & fixes

| # | Problem | Fix |
|---|---------|-----|
| 1 | `from .conftest import …` failed — `tests/` was not a package | Added `tests/__init__.py` + `pytest.ini` (`pythonpath=.`); `conftest.py` also injects `server/` onto `sys.path` (no editable install in the venv). |
| 2 | Model-id regex `claude-[a-z]+-\d` false-matched the generated session id `hmac:claude-account-01-…` → rejected legitimate events | Tightened `_MODEL_ID_RE` to anchor on real model-family tokens (`claude-opus/sonnet/haiku`, `gpt-[0-9o]`, `ft:`, `gemini-\d`, `llama-?\d`, `o[134]-mini/preview`); generic aliases no longer false-positive. |
| 3 | A real model id in `payload.model` rejected the whole event (leaf scan) instead of collapsing | Excluded `payload.model` from the leaf scan; it is normalized to the enum separately. All other fields stay scanned. |
| 4 | `error_label "429 too many requests"` dropped to `unknown` (8-char `requests` tripped the identifier-run drop before the keyword sniff) | Reordered `normalize_error_label`: hard path/secret drop first, then keyword sniff, then the identifier-run drop only for unmatched opaque text. |
| 5 | Code-snippet `task_label` (`const x = {a:1}; b();`) was not rejected | Added a code-density check (`≥2` of `{ } ;` or a newline) to `contains_forbidden`, so snippets reject in the leaf scan. |
| 6 | `curl` to localhost returned HTTP 502 | Agent shell routes through an HTTP proxy; `curl --noproxy '*'` bypasses it. The server itself was healthy (`lsof` confirmed LISTEN). |

---

## 10. How to run

```bash
# from server/  (or set PYTHONPATH=server)
cd /Users/hulu/huluman/agentlamp/server

# tests
/Users/hulu/huluman/agentlamp/.venv/bin/pytest -q          # 85 passed

# run the local LAN frame server (default bind 0.0.0.0:8787)
/Users/hulu/huluman/agentlamp/.venv/bin/python -m agentlamp_server
#   or: .venv/bin/uvicorn agentlamp_server.app:app --host 0.0.0.0 --port 8787

# device frame (Bearer; token never in URL)
curl -H 'Authorization: Bearer dev-local-token' \
     http://192.168.1.148:8787/api/v1/device/orb-01/frame

# live simulator
open http://192.168.1.148:8787/preview
```

Env knobs: `AGENTLAMP_LOCAL_BIND` (default `0.0.0.0:8787`), `AGENTLAMP_DEV_DEVICE_ID`
(`orb-01`), `AGENTLAMP_DEV_DEVICE_TOKEN` (`dev-local-token`), `AGENTLAMP_ALIAS_FILE`
(`~/.config/agentlamp/aliases.toml`), `AGENTLAMP_PEPPER_HEX` (else per-process random).

---

## 11. Notes for the next phases

- **Codex/Claude hook adapters (TASK-005):** each adapter resolves raw `cwd`/account/branch
  via `sanitize.project_alias`/`account_alias`/`branch_label` (keyring pepper) **before**
  emitting the envelope, then feeds `FrameState.apply_event` — which runs the same
  default-deny sanitizer. The `provider_sanitization_fixtures.md` cases are already encoded
  in `test_sanitization_fixtures.py`; wire the adapters to satisfy them.
- **Persistence + retention:** swap the in-memory `sessions`/`quota` dicts for the
  `architecture.md` storage model; add the 30-day purge job (auditable). The frame
  generator is unchanged (rebuildable-from-events already holds).
- **Relay mode (optional, last):** the priority + frame rules here are reused verbatim by
  the cloud; only add the collector-ingest HMAC surface + a second independent sanitization
  gate (identical forbidden-pattern table + recursive reject).
- **Firmware (TASK-004):** point `FRAME_BASE_URL` at `http://192.168.1.148:8787`; the frame
  is < 2 KB and ignores unknown fields (`fleet_more`) per contract.

**Nothing was committed** (per instructions). `git status` shows `server/` and
`docs/devlog/02-server-impl.md` untracked.

---

## Review fixes

Applied from `docs/devlog/02-server-review.md`. Each finding was reproduced first,
then fixed, then proven (unit test + live curl). Full suite: **105 passed** (was 85;
+20 regression tests). Re-curl confirmed every fix on a live server. Nothing committed.

### P0 (must fix)

**P0.1 — aliases NOT default-deny (`sanitize.py`)** — verified ✅ (real).
- Repro: `sanitize_event({…,project_alias:"client-acme-prod"})` → emitted `client-acme-prod`
  verbatim; `account_alias` same; `project_alias:"a"*3000` → 3000-char value unchanged. The
  forbidden-pattern + `looks_like_prompt` checks pass these. Required Fixtures only tested the
  `project_alias()` function, never the event-pipeline emit path.
- Fix: added a **positive shape gate** on the emit path. `looks_like_neutral_alias()` (max-len
  40 + allowlist regex: lowercase, ≤ 2 hyphen segments, or `hmac:…` / `—`) + `coerce_alias()`
  which HMAC-collapses anything that does not positively match (`project-<hmac6>` /
  `account-<hmac4>`). `sanitize_event()` now runs both `project_alias` and `account_alias`
  through it (account also gets the `looks_like_prompt` guard it previously lacked).
- Proof: `client-acme-prod` → `project-<hmac>` (no `acme`/`client`/`prod` leak, deterministic);
  `a`*3000 → 14-char `project-<hmac>`; neutral aliases (`project-a`, `main`, `work`,
  `project-7f3a9c`, `account-7f3a`, `claude-1`) survive verbatim. New **pipeline** fixtures:
  `test_pipeline_basename_alias_is_hmac_collapsed`, `test_pipeline_oversize_alias_is_collapsed`,
  `test_pipeline_neutral_alias_survives_verbatim`. Live: `admin/event project=client-acme-prod`
  → frame `primary.project = project-a8fa6f`.

**P0.2 — 2 KB hard cap NOT guaranteed (`state.py`)** — verified ✅ (real).
- Repro: a 3000-char `project_alias` produced a **3261-byte** frame (≥ 2048). `_enforce_byte_cap`
  trimmed only quota/fleet, never the primary string fields.
- Fix: `_enforce_byte_cap` now clamps the primary string fields (longest-first, halving toward a
  floor with a `…` marker) as a last resort after quota+fleet trimming, so the serialized body is
  provably < `FRAME_BYTE_CAP` regardless of input. (P0.1 already bounds aliases at sanitize time;
  this is the independent backstop for any path that populated state directly.)
- Proof: a directly-injected `Session` with 5000-char fields (bypassing the sanitizer) → 1826-byte
  frame, still JSON-round-trips and keeps `PRIMARY_KEYS`. New regression
  `test_frame_under_2kb_with_oversize_primary_alias`.

**P0.3 — pairing token leak (`app.py`)** — verified ✅ (real).
- Repro: `POST /api/v1/device/orb-01/pair` with a bogus or absent `pairing_code` → `200
  {device_token:"dev-local-token"}`. Violated the one-time-code contract (`pairing_and_auth.md`
  §Device Pairing 1-3).
- Fix: removed the "dev device → mint the dev token when code is bad/absent" shortcut.
  `pair_device` now returns the token **only** in exchange for a valid issued one-time code
  (burned on use) for **every** device incl. the dev device; otherwise `400 bad_pairing_code`.
- Proof: bogus → 400, absent → 400; a code issued via `admin/device/orb-01/code` redeems once
  (200) and replays as 400. Tests `test_pair_bogus_code_rejected`, `test_pair_absent_code_rejected`,
  `test_pair_dev_device_with_valid_issued_code` (the old `test_pair_returns_device_token`, which
  encoded the buggy contract, was replaced). Live curl confirmed 400 on bogus.

### P1 (should fix)

**P1.1 — alert suppressed by priority modifiers (`state.py`)** — verified ✅ (real).
- Repro: CODING + low-quota (+30, recency tie-break) and pinned CODING (+50) both became
  `ordered[0]`, yielding `scene=focus` and suppressing a WAITING alert elsewhere. Contradicts
  `cloud_contract.md` ("Alert scene interrupts … for waiting/error/quota danger/offline").
- Fix: `_select_scene` now detects the alert interrupt by scanning **all** sessions for
  WAITING/ERROR (focus = highest-priority such session), not just `ordered[0]`. (Fixed in the
  same edit as P1.2.)
- Proof: both repros now yield `scene=alert, primary.status=WAITING`; ERROR case too. Tests
  `test_waiting_alert_not_suppressed_by_low_quota_modifier`,
  `test_waiting_alert_not_suppressed_by_pinned_modifier`,
  `test_error_alert_not_suppressed_by_pinned_coding`. Live curl confirmed.

**P1.2 — quota danger ignored with no sessions (`state.py`)** — verified ✅ (real).
- Repro: quota 0.95 + zero sessions → `scene=sleep` (the `if not ordered: return sleep` ran
  before the quota-danger check).
- Fix: the quota-danger interrupt now runs **before** the no-session sleep branch, so it fires
  with zero live sessions.
- Proof: quota 0.95 + zero sessions → `scene=alert, accent=red`. Test
  `test_quota_danger_alert_with_no_sessions`. Live curl confirmed.

**P1.3 — error-envelope + crash hardening (`app.py` + `state.py`)** — verified ✅ (real).
- Repro: malformed `X-Frame-Schema-Version: abc` → FastAPI default `422 {detail:[…]}` (not the
  contract `{error,retry}` envelope); `build_frame(schema_version="not-an-int")` raised a raw
  `ValueError`.
- Fix: `get_frame` accepts the header as `str | None` and coerces it defensively via
  `_coerce_schema_version` (garbage/absent → server default, never 422). `build_frame` wraps
  `int(schema_version)` so a non-int raises `SanitizationError` (mapped to the `{error,retry}`
  503 envelope by the existing `except`), never a raw `ValueError`.
- Proof: `abc` header → `200`, no `detail`, `v=1`; absent header → 200; `build_frame(…,
  "not-an-int")` → `SanitizationError`. Tests
  `test_frame_malformed_schema_version_header_uses_contract_envelope`,
  `test_frame_missing_schema_version_header_ok`,
  `test_build_frame_non_int_schema_version_raises_sanitization_error`. Live curl confirmed.

**P1.4 — schema exactness drift (`state.py` + `device_frame_api.md` + `test_frame.py`)** —
verified ✅ (real).
- Repro: quota entries emitted only one of `w5`/`week` (doc schema shows both); `fleet_more`
  appeared top-level on overflow (in the Array Caps text but not the schema example), and
  `test_frame_schema_v1_shape` whitelisted `fleet_more` (`- {"fleet_more"}`) so exactness was
  never enforced.
- Fix (one canonical shape): the generator now **merges** the per-window `QuotaWindow` records
  into one `AccountQuota` per `(provider, account)` carrying **both** `w5` and `week` (absent
  window omitted, never `null`; confidence = lowest across windows, estimated = any), matching
  the `device_frame_api.md` / `agentlamp_ai_spec.md` schema example. `fleet_more` is now a
  **documented optional** top-level key (added to the schema example + an explainer note),
  present only when overflow > 0. The dead `out_marker` code in `_fleet_block` was removed.
- Proof: two-window account → single entry `{provider,account,confidence,estimated,w5,week}`;
  single-window account omits the absent key; single session → exact key set `== REQUIRED_KEYS`
  (no `fleet_more`); overflow → key set `== REQUIRED_KEYS | {fleet_more}`. Tests
  `test_quota_entry_merges_both_windows_per_account`, `test_quota_entry_omits_absent_window`,
  `test_frame_schema_v1_shape` (escape hatch removed), `test_fleet_capped_to_6_with_overflow_count`
  (now asserts `fleet_more` exactness). Live curl confirmed the merged quota shape.

### Files touched
- `server/agentlamp_server/sanitize.py` — positive alias shape gate (P0.1).
- `server/agentlamp_server/state.py` — byte-cap primary clamp (P0.2), all-session alert scan +
  quota-danger-before-sleep (P1.1/P1.2), non-int schema guard (P1.3), quota window merge +
  fleet_more cleanup (P1.4).
- `server/agentlamp_server/app.py` — pairing one-time-code enforcement (P0.3), defensive
  schema-version header coercion (P1.3).
- `server/tests/test_sanitization_fixtures.py`, `server/tests/test_frame.py`,
  `server/tests/test_api.py` — +20 regression tests.
- `docs/api/device_frame_api.md` — documented `fleet_more` + the both-window quota shape (P1.4).
