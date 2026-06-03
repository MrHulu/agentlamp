# 11 — R2 + R3: fleet legibility (active count + LCD layout)

> 2026-05-31. Closes R2/TASK-010 and the software side of R3/TASK-011 (pending Boss's
> physical-LCD eyeball). R2 (count semantics) and R3 (rendering) were done together
> because both live on the same fleet-row contract.

## The problem (seen, not assumed)

Staged a 7-project fleet on a clean server + screenshotted `/preview`. Two bugs were
visible on the device mockup:

- **Double count:** rows read `ai-center x5 ×5`, `channel-bridge x2 ×2` — the server
  baked `" xN"` into the `provider` string AND sent a structured `count`, so the
  simulator (which renders the count) printed it twice. The firmware ignored the
  structured `count` entirely.
- **Long-name truncation:** `moza-perception-analysis` (24 chars) hit the firmware's
  `FleetRow.provider[16]` and was hard-truncated to 15 chars *before* the renderer
  could shrink it; the HTML preview wrapped it to 3 lines (the device never wraps).
- **R2 count lie:** `ai-center x5` while only 3 of those 5 sessions were actually
  CODING (2 were DONE).

## The fix (server + firmware + preview, one coherent contract)

**Server (`state.py`)** — R2:
- `_is_active(eff)` (`_ACTIVE_EXCLUDED = {IDLE,DONE,UNKNOWN,STALE,OFFLINE}`) is the
  single source of truth, shared by `_select_scene` (≥2 active → fleet) and
  `_fleet_block`, so "how many are busy" can't drift between scene choice and rows.
- `_fleet_block` counts ONLY active sessions per project, drops projects with zero
  active agents, sets the row status to the top active status, and emits a CLEAN
  project label (no baked `xN`); the count rides in the structured `count` field.

**Firmware (`renderer.h`, `frame.h`)** — R3:
- `FleetRow.provider` and `Frame.project` widened 16/24 → **28** bytes (hold the
  24-char example + NUL).
- `drawFit` now **shrinks to the readable floor THEN ellipsizes** with `..` (ASCII —
  the Adafruit GFX fonts have no U+2026) when even the smallest font overflows, so a
  long label never runs into the next cell.
- `fleet()` draws a clean name + a **separate `xN` badge** (only when count>1, from the
  structured field) + the status word, with **disjoint pixel budgets** (≥4px clearance):
  name `[14,104]` no-badge (6px gap to status) / `[14,74]` with badge (6px gap to badge),
  badge `[80,106]` (4px gap to status), status `[110,158]`.
- Summary counts ONLY the rendered rows (≤5) and folds any undrawn row + the server's
  `fleet_more` into `+N more`, so the visible rows never disagree with the count.

**Preview (`preview.py`)** — made faithful to the device: clean name (single-line,
ellipsis — never wraps), separate `×N` badge (count>1) as its own flex item (so the
ellipsis can't swallow it), 5-row cap + fold, two-space `N active  +M more` summary.

**Contract doc (`device_frame_api.md`)** — corrected a pre-existing drift: a `fleet`
row's `provider` field carries the **project** display label (clean, no count suffix);
`count` is the number of **active** agents; `fleet_more` is additional active agents in
dropped projects. (The doc previously implied provider-grouping with title-case provider.)

## Adversarial review (4-AI, before flashing)

Ran a 3-lens review (server-logic / firmware-safety / contract-regression) → each finding
verified by a skeptic. **Server logic: zero defects.** Firmware/contract: **5 real defects
found and fixed** (the author missed all 5 — exactly why the council step is mandatory):

| # | sev | defect | fix |
|---|-----|--------|-----|
| 1 | high | name budget `[14,84]` overlapped badge `[80,106]` by 4px | name maxW 70→60 (6px gap) |
| 2 | med | single-agent name `[14,110]` abutted status at x=110, zero gap | name maxW 96→90 |
| 3 | med | summary counted 6 rows but only 5 rendered → 6th counted-but-invisible | count rendered rows; fold rest into `+more` |
| 4 | low | `provider[24]` truncated a 24-char name by one | widen to 28 |
| 5 | low | preview summary separator diverged from device | align to `  +N more` |

(Verifier suggested rendering 6 rows for #3; re-checked the geometry myself — 6 rows at
pitch 40 leaves ~1px to the summary = collision — so took the safer fold-into-`+more`
route. Don't rubber-stamp the council.)

## Verification

- 48 collector + **115** server tests green (3 new: active-only count, drop-all-idle,
  clean-label-no-suffix).
- Firmware compiles (RAM 15.6% / Flash 15.9%); flashed to `/dev/cu.usbmodem1101`;
  serial shows `frame ok` (boots, polls, parses/renders without crashing).
- **Live R2 proof:** with real sessions running, the orb showed `ai-center x3` (active
  only) instead of the prior inflated total.
- **Pending:** Boss's physical-LCD eyeball (R3 acceptance: "operator signs off"). A
  worst-case fleet (long name + badges) was staged on the live orb for that look.

## Residual / next

- `/preview` HTML can't perfectly replicate LovyanGFX font metrics — it's triage; the
  physical LCD is authoritative (hence the eyeball gate).
- R4/R5 (per-session identity, status-mix) remain; revisit after the eyeball.
