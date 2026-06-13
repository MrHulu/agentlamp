# Readers — supported hardware

> An AgentLamp **reader** is any device that displays your agent status. All readers are
> interchangeable consumers of one contract — `GET /api/v1/device/:id/frame` (schema-v1, Bearer
> auth). The cloud/collector never knows or cares which hardware is reading; adding a new device
> is **never** a core change. Background:
> [architecture.md → Heterogeneous readers](../docs/architecture/architecture.md#heterogeneous-readers-hardware-extensibility).

## Supported hardware

| Reader | Hardware | Renderer | Status | Effect | Code | Deploy |
|--------|----------|----------|--------|--------|------|--------|
| **ESP32 lamp** | Waveshare ESP32-S3-LCD-1.47B (172×320 + RGB) | C++ / PlatformIO | ✅ shipping | desk orb, ~4s realtime | [`../firmware/`](../firmware/) | [`../docs/BUILD.md`](../docs/BUILD.md) |
| **iPhone widget** | any iPhone (iOS 16+) | Scriptable JS | 🆕 single-file widget implemented + conformance-tested, not yet on-device | home/lock-screen widget (+ P2 instant alerts), ~5–15 min refresh | [`iphone-widget/`](iphone-widget/) | [`iphone-widget/DEPLOY.md`](iphone-widget/DEPLOY.md) |

Both render the **same scenes** (focus / fleet / quota / alert) from the same frame — see the
scene strip in the [root README](../README.md#-这是什么what). They differ only in renderer
language and form factor.

## Directory structure

```
readers/
├── README.md                 ← this catalog
└── iphone-widget/            ← iPhone reader (Scriptable)
    ├── README.md             ← what + effect + test command
    ├── agentlamp-widget.js   ← single-file Scriptable widget (fetch + last-good cache + render)
    ├── frame-view.js         ← shared pure logic for alert/tests (buildViewModel + shouldAlert)
    ├── agentlamp-alert.js    ← P2 instant-alert notifier (Shortcuts timer; optional Pushcut)
    ├── DEPLOY.md             ← phone + second-computer deployment walkthrough
    └── test/
        ├── frame-view.test.cjs       ← zero-dep Node conformance tests vs parity fixtures
        └── widget-template.test.cjs  ← static guard for one-file template + no live secrets

../firmware/                  ← ESP32 lamp reader (C++/PlatformIO) — kept at its established home;
                                 indexed here as the first reader. May move under readers/ later (low-pri, not now).
```

## Adding a new reader (future hardware)

A new hardware type (Android widget, e-ink board, desktop menubar, …) attaches by implementing
**fetch → parse → render** against the device-frame contract — no collector or cloud change:

1. Read the contract: [`../docs/api/device_frame_api.md`](../docs/api/device_frame_api.md) (the
   frame schema is the *only* thing a reader depends on).
2. Add `readers/<your-device>/` with the renderer + a `DEPLOY.md`.
3. Add a row to the table above.
4. If your display is **larger** than the lamp and the lamp-shaped frame
   (`FLEET_MAX_ROWS=5`, `FRAME_BYTE_CAP=2048`) feels cramped, negotiate a bigger frame via an
   `X-Frame-Profile` header — see architecture.md. Don't build that until a reader actually needs
   it.
