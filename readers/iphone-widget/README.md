# iPhone widget reader

**Hardware:** any iPhone (iOS 16+) · **Host:** [Scriptable](https://scriptable.app) (free) ·
**Status:** 🆕 widget template implemented + conformance-tested; not yet run on a real device.

A read-only AgentLamp reader on your iPhone home/lock screen. It consumes the **same**
device-frame contract as the ESP32 lamp — `GET /api/v1/device/:id/frame`, schema-v1, Bearer —
and renders it as a native iOS widget. No App, no jailbreak, no developer account.

> ## 🚨 Widget is one-file now
> For the home-screen widget, paste **only** [`agentlamp-widget.js`](agentlamp-widget.js)
> into one Scriptable script named `AgentLamp`. The optional alert script still imports
> [`frame-view.js`](frame-view.js); save that second file only if you use alerts.

## Effect

Renders the same scenes as the lamp (focus / fleet / quota / alert) — see the scene strip in
the [root README](../../README.md#-这是什么what). The frame data is identical; only the
renderer (Scriptable JS vs ESP32 C++) and form factor differ. The current widget uses a light
HULU card, Chinese labels, Claude/Codex quota blocks, plan chips, absolute reset times, and
**remaining** quota percentages. Multi-device identity appears in the subtitle when each
machine uses a distinct `account_alias`.

> **Honest limit:** iOS throttles widget refresh to ~5–15 min (`refreshAfterDate` is a hint).
> For instant `WAITING`/`ERROR` pings, use the P2 alert script — see [`DEPLOY.md`](DEPLOY.md).

## Files

| File | What | Platform |
|------|------|----------|
| [`agentlamp-widget.js`](agentlamp-widget.js) | The widget — single-file Scriptable template with fetch + last-good cache + render. | Scriptable |
| [`frame-view.js`](frame-view.js) | Shared pure alert/test logic: `buildViewModel` + `shouldAlert`. No I/O. | Scriptable + Node |
| [`agentlamp-alert.js`](agentlamp-alert.js) | **P2** instant-alert notifier (Shortcuts timer; optional Pushcut). Requires `frame-view`. | Scriptable |
| [`test/frame-view.test.cjs`](test/frame-view.test.cjs) | Conformance tests vs the real relay parity fixtures. | Node (zero-dep) |
| [`test/widget-template.test.cjs`](test/widget-template.test.cjs) | Static guard: widget stays one-file and contains no live token/relay URL. | Node (zero-dep) |
| [`DEPLOY.md`](DEPLOY.md) | Full phone walkthrough + verify + troubleshooting. | — |

## Test

Cross-platform, zero-dependency — runs anywhere Node is installed:

```sh
node --test readers/iphone-widget/test/frame-view.test.cjs readers/iphone-widget/test/widget-template.test.cjs
```

It loads `frame-view.js` against `tests/fixtures/parity/frame_vectors.json` (the same
canonical frames the cloud is tested against), so if the frame schema drifts, this test
drifts with it.

## Why the widget is single-file, but alerts still share logic

The widget is optimized for phone deployment: one Scriptable paste, no hidden companion file.
The optional alert script still shares `frame-view.js` so its dedup/key logic stays tested.
Neither path shares rendering code with the ESP32 lamp — an iOS widget renderer and an LCD
renderer have nothing renderable in common; the abstraction they share is the **wire contract**,
not code. See
[architecture.md → Heterogeneous readers](../../docs/architecture/architecture.md#heterogeneous-readers-hardware-extensibility).
