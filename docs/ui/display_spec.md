# Display Spec

## Screen

- Physical resolution: 172x320.
- Orientation: portrait by default.
- Design intent: premium ambient AI work terminal.
- Text density: low.
- Contrast: high.

## Scenes

| Scene | Purpose |
|-------|---------|
| Boot | Startup identity and diagnostics |
| Pairing | First-run config and token state |
| Fleet | Provider/session summary |
| Focus | Highest-priority active session |
| Quota | Top quota risk accounts |
| Alert | Waiting/error/quota danger |
| Offline | Frame source unreachable (LAN collector in local mode, or relay in relay mode) |
| Stale | Cached frame expired |
| Diagnostics | Network/frame/schema debug |
| Sleep | Dim ambient mode |

## Layout Rules

- One dominant status word per page.
- Max two quota rows.
- Max one focus session.
- No scrolling in MVP.
- No tiny multi-column dashboards.
- Avoid raw paths, raw branch names, or long task titles.
- Use symbols and color for state; text must remain readable without color.

## Rotation

Normal rotation:

```text
Fleet -> Focus -> Quota -> Fleet
```

Interruptions:

- Alert preempts all normal scenes.
- Offline/Stale preempts normal scenes.
- Diagnostics only by explicit device/admin trigger.

## Browser Simulator

The local frame server (and the cloud dashboard in relay mode) must include a 172x320
simulator before firmware UI is considered stable.

Simulator requirements:

- Render from the exact frame JSON returned by `/api/v1/device/{device_id}/frame`.
- Toggle mock scenes.
- Show payload byte size.
- Highlight stale/expired TTL.
- Support screenshot-based regression checks.

