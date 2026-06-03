# 12 — 4-AI council review hardening (R1/R2/R3)

> 2026-06-01. A second, broader adversarial review (4 AIs: Claude, OpenClaude, OpenCode,
> Codex — review-only) over the R1/R2/R3 code + docs, with an explicit lens on
> cross-platform / cross-machine / DRY. Each concrete finding was re-verified against the
> live code before fixing (the council is input, not gospel). Server logic (state.py R2
> semantics) came back clean from all four.

## Fixed

| # | sev | area | finding | fix |
|---|-----|------|---------|-----|
| 1 | high | cross-platform | `install_hooks.py` venv path `.venv/bin/python` is POSIX-only; Windows falls back to `python3` (not on PATH); double-quoted TOML + Windows backslashes = invalid TOML | OS-aware venv (`Scripts/python.exe` on `nt`), `sys.executable` fallback, single-quoted TOML **literal** strings (backslash-safe), `_quote()` per-OS |
| 2 | med | cross-machine | hook command not shell-quoted → a space in the install path silently kills the fire-and-forget hook | `shlex.quote` (POSIX) / double-quote (Windows) each path component |
| 3 | high | bug | firmware `FleetRow.provider[28]` / `Frame.project[28]` hard-truncate labels 28–40 chars (server `ALIAS_MAX_LEN = 40`) **before** `drawFit` can ellipsize → silent mid-word cut, no `..` | widen both to `[41]`; bump `drawFit` buf to 48; + server test `test_fleet_label_not_truncated_server_side` (40-char label survives) |
| 4 | med | DRY / consistency | server caps fleet at **6** but device + preview render **5** → a 6th row is always transmitted-but-invisible; the `5` vs `6` split invites drift | cap server at `FLEET_MAX_ROWS = 5` (named constant; wire cap = render cap); updated API doc + cap test |
| 5 | med | cross-platform | `hook_sink.py` non-POSIX stdin fallback was a **blocking** read → can hang to the host's 5 s hook timeout on Windows | threaded daemon-read with a real deadline for Windows / non-main-thread |
| 6 | med | omission | Codex hook generator's "add compaction" comment was false (`CODEX_EVENTS` had none); `SubagentStart` also missing though `normalize` handles it | added `PreCompact`, `PostCompact`, `SubagentStart` → 10 events (all valid per the Codex hooks doc) |
| 7 | low | faithfulness | preview used `×` (U+00D7) + full uppercase status; firmware draws ASCII `x` + 4-char lowercase → simulator could mask a real on-device layout diff | preview now uses ASCII `x` + `status.slice(0,4).toLowerCase()`, `.st` text-transform none; comment softened from "mirror exactly" to "approximate" |
| 8 | low | docs/robustness | stale `state.py` module docstring (fleet provider = title-case); `device_frame_api.md` example had `fleet_more: 0` (prose says absent unless >0); devlog "6px clearance" (badge→status is 4px); `_pending_fleet_more` set outside `__init__` (`# type: ignore`); overflow test only asserted `> 0` | all corrected; `_pending_fleet_more` initialized in `__init__`; overflow test asserts exact `== 3` |

## Dismissed (verified false positives — did NOT change)

- **OpenCode C1/H2 — "server still bakes ` xN` into the provider"**: FALSE. `state.py:573-574`
  emits `{"provider": r["project"], ...}` (clean); `test_fleet_groups_by_project_with_count`
  asserts `" x" not in provider` and **passes**. OpenCode misread the diff's deleted `-`
  line as current code. (Claude, OpenClaude, Codex all correctly saw the clean label.)
- **OpenCode H1 — "`badge[8]` overflows for count ≥ 10"**: FALSE. 8 bytes holds `"x255\0"`;
  and `drawFit` ellipsizes if the glyph ever exceeds the 26 px cell. No overflow.
- **OpenClaude #1 — "`drawFit` ellipsis truncates from the right, wrong for right-aligned"**:
  unreachable. The only ellipsizing caller is the LEFT-aligned project name (where a `..`
  suffix is correct); right-aligned callers (status = 4 chars, badge = short) always fit, so
  the path never runs for them. Left as-is.

## Verification

- 48 collector + **116** server tests green (cap test renamed `_to_5_` + exact `fleet_more`;
  new long-label test).
- `install_hooks.py --print codex` → valid TOML (tomllib), 10 events, single-quoted literal
  command, machine-local warning header.
- Firmware rebuilt + re-flashed; serial `frame ok`.

## Follow-up: mDNS server discovery (the real cross-machine fix)

Mid-session the orb went offline — root cause: the Mac's **DHCP IP drifted `192.168.1.148 →
.147`** and the firmware polled the IP pinned in NVS. A USB replug didn't help (the IP it
polls didn't change). Boss: *"拔插一次就这么麻烦…你必须给我搞好修复好"* — so this got the proper
fix, exactly the cross-machine brittleness the council's lens predicted (R6-ops).

**Fix (firmware-only): the orb discovers the server by name via mDNS.** macOS/Bonjour keeps
`<LocalHostName>.local` mapped to the host's *current* IP, so resolving it auto-follows any
IP change. `firmware/src/main.cpp` now: `resolveServerViaMdns()` queries `FRAME_MDNS_HOST`
(`config.h`, default `yangzhenzhous-macbook-air`) → rebuilds `frameBaseUrl` from the live IP
+ `FRAME_SERVER_PORT`. Called at boot, after any (re)join, and **every 3 transport fails** so
a mid-run IP change self-heals in ~12 s (no reboot, no reflash, no re-provision). The stored
NVS / compiled `FRAME_BASE_URL` stays as the fallback when mDNS can't resolve. `ESPmDNS.h` is
in the Arduino-ESP32 core (no new dep).

**Proven on the device boot log:**
```
frame_base_url : http://192.168.1.148:8787      ← stale stored IP
wifi           : connected, ip=192.168.1.169
mdns           : server -> http://192.168.1.147:8787   ← discovered the live IP
frame ok       : scene=2 seq=1034 ttl=5          ← online
```
Set `FRAME_MDNS_HOST=""` to disable (falls back to the fixed IP). If the Mac is renamed or
swapped, update `FRAME_MDNS_HOST` to the new `scutil --get LocalHostName`.

## Note on the live machine

The R1 install on this Mac has the original **7** Codex events (trusted). The generator now
emits **10**; the 3 added (compaction + subagent-start) are additive THINKING states —
re-run `install_hooks --write-codex` + re-trust via the Codex `/hooks` prompt to capture
them. The original 7 cover the full visible arc, so this is optional.
