# 17 — Multi-device cloud aggregation: investigation

> 2026-06-07. The owner asked whether his **two computers** (same personal account, both
> running coding agents) could each push to the cloud while the phone just pulls **one
> aggregated status**. This entry is the investigation log — what was read, what was found,
> the "aha" moment, and the one real blind spot. No code shipped; the forward plan lives in
> [`../plans/2026-06-07-multi-device-cloud-aggregation.md`](../plans/2026-06-07-multi-device-cloud-aggregation.md).

## The question

> "我现在有两台电脑设备，两电脑设备都会在使用我的账号……让他们两个都能上传到云端，然后手机就只负责去这个云端去拿取状态就行了。"

First instinct: *this probably needs new cloud aggregation code, or one device per machine
with the phone merging client-side.* Both wrong, as it turned out.

## What I read

1. **`src/cloud/src/index.ts`** → `const id = env.RELAY.idFromName("relay")`. There is exactly
   **one** RelayDO instance globally. Every ingest and every frame request routes to it. The
   aggregation question dissolved on the spot — it's not "should we build it," it's **already
   built that way**.
2. **`src/cloud/src/frame.ts`** → `applySanitizedEvent` writes each event into the **shared**
   `st.sessions`; `buildFrame` derives the device frame from that same shared state. Confirmed:
   multiple collectors' sessions physically live in one place.
3. **`src/cloud/src/relay_do.ts`** → enroll/revoke registries: each collector gets its own
   `kid` + secret, runtime-enrolled into DO storage with **no `wrangler deploy`**. Adding a
   machine is cheap; revoke is strongly consistent.
4. **`src/collector/config.py`** → `COLLECTOR_ID` is per-machine overridable (`config.py:112`).

## Aha

I expected to write aggregation code. The actual cloud delta is **zero**. The real problem
isn't *"can it aggregate"* — it's *"once aggregated, how does the phone tell which machine an
agent is on."*

## The design blind spot

`sessionKey = (provider, account_alias, session_id ‖ project_alias)` — **no machine
dimension.** Two machines on the same account + same project, with an empty `session_id`, can
**overwrite each other** (same key); and even when they don't, the fleet view groups by
`displayLabel`, so two machines on one project **collapse into a single row (count=2)**. That
is the genuine work of this project — but it's a *display-convention* problem, not a core-key
problem. Start with a per-machine `account_alias` (plan §3, option A); only promote hostname to
a first-class field (open question D1) if the alias convention proves insufficient under a real
two-machine test.

## Decisions taken

- DD1 — reuse the singleton DO aggregation, don't touch the cloud algorithm.
- DD2 — each machine is its own collector (`kid` + unique `collector_id`): per-machine write
  audit, independent revoke, no mutual clobber.
- DD3 — disambiguate machines by alias convention first (zero code, reversible).

Deferred for a real two-machine test: D1 hostname as a first-class field · D2 per-collector
heartbeat (the global `last_collector_heartbeat` makes "whole-fleet offline" weaker with N
machines — single-machine death is handled fine by per-session STALE/OFFLINE decay) · D3
whether doubled session volume blows the `FLEET_MAX_ROWS=5` / `FRAME_BYTE_CAP=2048` ceilings.

## Status

Investigation complete, **nothing landed.** Next step (awaiting owner go): run the plan's
enroll + relay-flip on the second machine, then verify what the fleet actually looks like with
both machines live and whether the alias disambiguation is enough.

<!-- Next entry reserve: two-machine enroll results — actual fleet layout, alias sufficiency, 5-row/2KB headroom -->
