# AgentLamp — AI-Friendly Implementation Specification

> ⚠️ **ARCHIVED ORIGINAL SPEC — do not implement directly.** This is the original
> brainstorming spec. The canonical contracts in `docs/product/`, `docs/architecture/`,
> `docs/api/`, `docs/security/`, `docs/firmware/`, `docs/cloud/`, `docs/collector/`, and
> `docs/providers/` **override** this document. In particular: (1) AgentLamp is now
> **local-mode-first** — the public cloud is optional (relay), not the center; (2) several
> raw fields shown below (`branch`, `repo`, `account_id`, `plan`, real `model` ids, the
> `AI COCKPIT`/`QUOTA COCKPIT` screen labels) **violate the current sanitization policy or
> naming** and must not be implemented as written. Use this file only for historical
> context.

> Version: v2.1  
> Target Hardware: Waveshare ESP32-S3-LCD-1.47B  
> Project Type: Local-first (cloud-optional) multi-account AI agent status terminal  
> Primary Goal: Build a secure, read-only AI status display for Codex, Claude, and future providers.

---

## 0. Executive Summary

AgentLamp is a physical desktop status terminal built on **Waveshare ESP32-S3-LCD-1.47B**.

It displays:

- active Codex / Claude / AI coding sessions
- multiple account quota windows
- 5-hour quota usage
- weekly quota usage
- waiting-for-approval alerts
- errors and stale/offline states
- high-priority session focus
- provider/account summary

The ESP32 device does **not** render HTML webpages.

Instead, the architecture is:

```text
Local Collectors
  -> sanitize provider/account/session/quota data
  -> push summaries to public cloud server over HTTPS

AgentLamp Cloud
  -> aggregate multi-provider, multi-account, multi-session state
  -> compute display priority
  -> generate compact device frame JSON

ESP32-S3-LCD-1.47B
  -> pull compact frame JSON from public domain
  -> render scene UI locally
  -> drive RGB ambient light
```

---

## 1. Product Name

Recommended name:

```text
AgentLamp
```

Alternative names:

```text
AI AgentLamp
Codex Cockpit Mini
Quota Orb
Agent Fleet Terminal
```

Use **AgentLamp** as the canonical project name.

---

## 2. Design Philosophy

The device should feel like:

```text
a premium ambient AI work terminal
```

Not:

```text
a cheap ESP32 board showing debug text
```

Visual direction:

```text
OpenAI × Nothing × Teenage Engineering × Cyber Terminal
```

Core visual keywords:

```text
dark background
minimal layout
high-contrast typography
status-driven animation
micro particles
breathing RGB light
low information density
strong alert hierarchy
physical product feel
```

---

## 3. Hardware Target

### 3.1 Target Board

```text
Waveshare ESP32-S3-LCD-1.47B
```

### 3.2 Expected Hardware Capabilities

```text
ESP32-S3 MCU
1.47-inch LCD
172 × 320 resolution
Wi-Fi connectivity
RGB LED
USB-C power and flashing
optional battery support
custom enclosure support
```

### 3.3 Hard Limitation

Do **not** design this as a browser device.

Forbidden design:

```text
ESP32 opens dashboard.html and renders HTML/CSS/JS
```

Correct design:

```text
ESP32 fetches compact JSON frame over HTTPS
ESP32 renders UI locally with LovyanGFX / LVGL
ESP32 controls RGB light locally
```

---

## 4. Top-Level Architecture

### 4.1 Final Architecture

```text
Codex / Claude / Cockpit Tools / CodexBar / CLI
        ↓
Local Collector
        ↓ HTTPS Push, sanitized summaries only
AgentLamp Cloud
        ↓ HTTPS Pull, compact frame JSON
ESP32-S3-LCD-1.47B
        ↓
Scene UI + RGB Ambient Light
```

### 4.2 Core Principles

```text
Complex logic runs in the cloud.
Sensitive credentials stay local.
ESP32 only renders display frames.
Cloud stores sanitized summaries only.
Device API is read-only.
```

---

## 5. Security Boundary

### 5.1 Cloud Server Must NOT Store

The public server must not store:

```text
OpenAI cookies
Claude cookies
refresh tokens
browser sessions
full account login state
full transcripts
full prompts
full local code paths
source code contents
provider raw credential files
```

### 5.2 Cloud Server May Store

The public server may store sanitized summaries:

```text
account alias
provider name
plan name
quota summary
session summary
last_seen timestamp
confidence level
display priority
device heartbeat
event hashes
audit metadata
```

### 5.3 Local Collector Responsibility

The Local Collector runs on the user's own computer.

It is responsible for:

```text
reading local Codex / Claude / Cockpit Tools / CodexBar status
reading local hook events
sanitizing sensitive fields
summarizing quota/session state
signing payloads
pushing summaries to cloud over HTTPS
retrying with offline cache
```

Collector must not upload raw credentials.

---

## 6. Provider Scope

### 6.1 Initial Providers

```text
Codex
Claude / Claude Code
Cockpit Tools
CodexBar
Manual test provider
```

### 6.2 Future Providers

```text
Cursor
GitHub Copilot
Gemini CLI
Windsurf
Local Shell
CI/CD
```

### 6.3 Unified Model

All providers must be normalized into:

```text
Provider
Account
QuotaWindow
Session
Event
DeviceFrame
```

---

## 7. Multi-Session Display Strategy

### 7.1 Cloud Owns the Session List

Cloud stores all active and recent sessions.

Example session object:

```json
{
  "session_id": "codex_abc123",
  "provider": "codex",
  "account_id": "openai_main",
  "account_label": "OpenAI Main",
  "project": "app-core",
  "repo": "app-core",
  "branch": "feature/login",
  "status": "CODING",
  "priority": 80,
  "model": "codex",
  "task": "Refactor auth flow",
  "started_at": 1716900000,
  "updated_at": 1716900480,
  "elapsed_sec": 480,
  "needs_attention": false
}
```

### 7.2 ESP32 Must Not Sort Sessions

ESP32 must not perform complex prioritization.

Cloud is responsible for:

```text
session aggregation
session priority calculation
quota risk calculation
alert detection
frame generation
```

ESP32 is responsible for:

```text
fetch frame
render scene
animate scene
set RGB color/effect
handle offline/stale fallback
```

---

## 8. Display Modes

ESP32 has four primary display modes.

---

### 8.1 Fleet Overview

Purpose: summarize all providers and active sessions.

Example:

```text
┌────────────────────┐
│ AI COCKPIT    14:32│
│                    │
│  ● Codex     3 run │
│  ● Claude    1 wait│
│  ● Cursor    idle  │
│                    │
│  WAITING: Claude   │
│  app-core / auth   │
│                    │
│  5H 72%  WK 41%    │
└────────────────────┘
```

---

### 8.2 Focus Session

Purpose: show the highest-priority session.

Example:

```text
┌────────────────────┐
│ CODEX      Pro Main│
│                    │
│      CODING        │
│                    │
│ app-core           │
│ editing auth.ts    │
│                    │
│ 08:12    5H 72%    │
└────────────────────┘
```

---

### 8.3 Quota Cockpit

Purpose: show quota usage for the most important or most constrained accounts.

Example:

```text
┌────────────────────┐
│ QUOTA COCKPIT      │
│                    │
│ Codex Main         │
│ 5H  ███████░ 72%   │
│ WK  ████░░░░ 41%   │
│                    │
│ Claude Max         │
│ 5H  █████░░░ 58%   │
│ WK  ███░░░░░ 34%   │
└────────────────────┘
```

Rules:

```text
Only show the top 2 most important quota entries.
If there are more accounts, show "+N more".
If quota is estimated, show "~" before the number.
If confidence is low, show a visual confidence indicator.
```

---

### 8.4 Alert Page

Purpose: interrupt normal rotation for waiting approvals, errors, quota danger, or server/device offline.

Example:

```text
┌────────────────────┐
│ ACTION REQUIRED    │
│                    │
│ Claude Max         │
│ WAITING APPROVAL   │
│                    │
│ Codex Main         │
│ 5H quota 91% used  │
│                    │
│ Check dashboard    │
└────────────────────┘
```

---

## 9. Session Priority Rules

Cloud computes `display_score`.

Recommended scoring:

```text
WAITING_PERMISSION  +100
ERROR               +90
CODING              +70
THINKING            +65
TESTING             +60
RUNNING             +55
DONE                +20
IDLE                +0

quota below 20%      +30
user pinned          +50
stale > 10 minutes   -20
```

Display rules:

```text
WAITING / ERROR immediately interrupts rotation.
Multiple active sessions are ordered by display_score.
Without alerts, rotate Fleet / Focus / Quota pages.
Low quota periodically inserts Quota Warning.
```

---

## 10. Quota System

### 10.1 Quota Is Not Always Exact

Do not assume provider quota is always precise or officially available.

Support these quota types:

```text
official quota
observed quota
inferred quota
manual quota
unknown quota
```

### 10.2 Quota Model

```json
{
  "provider": "codex",
  "account_id": "openai_main",
  "account_label": "OpenAI Main",
  "plan": "Pro",
  "window_type": "5h",
  "used_ratio": 0.72,
  "remaining_ratio": 0.28,
  "reset_at": 1716912000,
  "reset_in_sec": 4320,
  "source": "collector",
  "source_type": "local_app",
  "confidence": "medium",
  "is_estimated": true,
  "last_verified_at": 1716909000
}
```

### 10.3 Confidence Levels

```text
high     official API or explicit local provider state
medium   local tool, CLI, dashboard, or strong inference
low      text parsing or manual estimate
unknown  unavailable or unverified
```

### 10.4 Displaying Estimated Quota

If quota is estimated, display:

```text
5H ~72%
WK ~41%
```

Optional visual indicator:

```text
●●● high
●●○ medium
●○○ low
```

---

## 11. Cloud Backend Architecture

### 11.1 Recommended Stack

```text
Python 3.11+
FastAPI
Uvicorn
Pydantic
PostgreSQL
Redis
SQLAlchemy or SQLModel
APScheduler
httpx
orjson
python-jose or PyJWT
loguru
prometheus-client
Docker
Docker Compose
Caddy or Nginx
Let's Encrypt HTTPS
```

### 11.2 Cloud Modules

```text
cloud/
├─ account_registry
├─ quota_aggregator
├─ quota_confidence_engine
├─ session_aggregator
├─ display_priority_engine
├─ provider_adapters
├─ collector_ingest_api
├─ device_frame_api
├─ admin_dashboard
├─ alert_engine
├─ auth_service
└─ audit_log
```

### 11.3 Backend Responsibilities

```text
receive Local Collector pushes
verify signatures and collector identity
deduplicate events
handle out-of-order events
aggregate multi-account quota
aggregate multi-session state
detect alerts
generate ESP32 compact frames
provide Web Dashboard
store history and audit logs
```

---

## 12. Local Collector Architecture

### 12.1 Recommended Stack

```text
Python 3.11+
httpx
watchdog
pydantic
keyring
platformdirs
loguru
rich
psutil
tenacity
```

### 12.2 Collector Modules

```text
collector/
├─ codex_adapter
├─ claude_adapter
├─ cockpit_tools_adapter
├─ codexbar_adapter
├─ local_session_watcher
├─ quota_reader
├─ sanitizer
├─ push_client
├─ offline_cache
└─ config_manager
```

### 12.3 Collector Push Requirements

Collector push must support:

```text
HTTPS
HMAC-SHA256 signature
timestamp anti-replay
collector_id
payload hash
retry with exponential backoff
offline cache
sanitization before send
```

---

## 13. ESP32 Firmware Architecture

### 13.1 Recommended Stack

```text
PlatformIO
Arduino-ESP32 Core 3.x
LovyanGFX
ArduinoJson
WiFiClientSecure
HTTPClient
Preferences
ESPmDNS optional
ArduinoOTA optional
Adafruit NeoPixel or FastLED
NTPClient or time.h
```

### 13.2 Firmware Modules

```text
firmware/
├─ main.cpp
├─ config/
│  ├─ board_pins.h
│  ├─ build_config.h
│  └─ secrets.example.h
├─ board/
│  ├─ display_driver.cpp
│  ├─ rgb_driver.cpp
│  ├─ backlight.cpp
│  └─ battery.cpp
├─ network/
│  ├─ wifi_manager.cpp
│  ├─ https_client.cpp
│  ├─ frame_client.cpp
│  └─ ota_manager.cpp
├─ core/
│  ├─ app_state.cpp
│  ├─ scene_manager.cpp
│  ├─ cache.cpp
│  └─ diagnostics.cpp
├─ ui/
│  ├─ renderer.cpp
│  ├─ theme.cpp
│  ├─ animation.cpp
│  ├─ widgets/
│  └─ scenes/
└─ assets/
```

### 13.3 ESP32 Device API

ESP32 should call only:

```http
GET https://YOUR_DOMAIN/api/device/{device_id}/frame
```

### 13.4 Frame Response Constraints

```text
response size < 2KB
poll interval 3–5 seconds
include ttl
include seq
include schema version
include server_time
```

---

## 14. Device Frame API Contract

### 14.1 Endpoint

```http
GET /api/device/{device_id}/frame
```

### 14.2 Example Response

```json
{
  "v": 1,
  "device_id": "orb-01",
  "scene": "alert",
  "headline": "ACTION REQUIRED",
  "primary": {
    "provider": "Claude",
    "account": "Max",
    "status": "WAITING",
    "project": "app-core",
    "task": "approval needed"
  },
  "fleet": [
    {"provider": "Codex", "count": 3, "status": "CODING"},
    {"provider": "Claude", "count": 1, "status": "WAITING"}
  ],
  "quota": [
    {
      "provider": "Codex",
      "account": "Main",
      "w5": 0.72,
      "week": 0.41,
      "confidence": 2,
      "estimated": true
    },
    {
      "provider": "Claude",
      "account": "Max",
      "w5": 0.58,
      "week": 0.34,
      "confidence": 1,
      "estimated": true
    }
  ],
  "accent": "yellow",
  "ttl": 5,
  "seq": 1852,
  "server_time": 1716900400
}
```

### 14.3 ESP32 Behavior

```text
fetch frame
validate schema version
validate ttl
if seq unchanged, continue current animation
if scene changed, run scene transition
if request fails, use cached frame
after 3 consecutive failures, show OFFLINE
if frame expired, show STALE indicator
```

---

## 15. HTTPS and Certificate Strategy

ESP32 HTTPS concerns:

```text
certificate chain changes
root certificate expiration
DNS failure
TLS handshake failure
weak Wi-Fi
server 502/504
oversized JSON
```

Recommended strategy:

```text
use WiFiClientSecure
pin root or intermediate certificate
do not pin short-lived leaf certificate
support OTA certificate updates
2-second request timeout
exponential backoff on failure
cache last valid frame
```

---

## 16. UI Scene System

### 16.1 Scenes

```text
BootScene
PairingScene
FleetScene
FocusScene
QuotaScene
AlertScene
OfflineScene
StaleScene
DiagnosticsScene
SleepScene
```

### 16.2 Scene Lifecycle

Each scene should implement:

```text
enter()
update(delta_ms)
render()
exit()
```

### 16.3 Animation Types

```text
fade in
fade out
slide transition
breathing opacity
orb rotation
particle drift
scanline shimmer
glitch effect
progress sweep
toast notification
```

### 16.4 RGB Effects

```text
IDLE      dark blue breathing
THINKING  blue-purple breathing
CODING    purple pulse
READING   cyan flow
TESTING   white-blue scan
WAITING   yellow blink
DONE      green bloom
ERROR     red blink
OFFLINE   red-blue alternate
STALE     white slow blink
```

RGB brightness should be capped at 20%–35% by default.

---

## 17. Admin Dashboard

### 17.1 Required Features

Web Dashboard should support:

```text
view all providers
view all accounts
view quota windows
view active sessions
view ESP32 device state
manually trigger test frame
switch theme
view history
view collector heartbeat
view alerts
```

### 17.2 Suggested Pages

```text
/admin
/accounts
/sessions
/quotas
/devices
/themes
/events
/audit
/preview
```

### 17.3 Screen Preview

Must provide a browser-based simulator:

```text
172 × 320 virtual screen
real-time frame preview
simulate Fleet / Focus / Quota / Alert scenes
debug UI without flashing ESP32
```

---

## 18. Compliance and Product Boundary

### 18.1 First Version Is Read-Only

Allowed:

```text
display status
display quota
display alerts
display reset countdown
display multi-account summaries
display session priority
```

Not allowed in v1:

```text
automatic account switching
automatic quota evasion
automatic task wake-up that consumes quota
public-server proxying provider requests
uploading provider credentials
uploading full login state
```

### 18.2 Multi-Account Purpose

Multi-account support is for:

```text
observation
awareness
manual decision-making
```

Not for:

```text
automatic provider limit circumvention
automatic account rotation
automatic task dispatch based on quota evasion
```

---

## 19. Database Design

### 19.1 Suggested Tables

```text
accounts
providers
collectors
collector_events
sessions
quota_windows
devices
device_frames
alerts
audit_logs
themes
```

### 19.2 Event Idempotency Fields

Events should include:

```text
event_id
collector_id
provider
provider_event_name
source_seq
idempotency_key
dedupe_key
raw_event_hash
received_at
event_time
schema_version
payload_sanitized
```

### 19.3 Out-of-Order Handling

State machine must handle:

```text
out-of-order events
duplicate events
late events
collector offline replay
session timeout
provider field missing
schema version mismatch
```

---

## 20. Deployment Architecture

### 20.1 Recommended Deployment

```text
Ubuntu 22.04 or 24.04
Docker
Docker Compose
Caddy
PostgreSQL
Redis
FastAPI app
```

### 20.2 Domain

Example:

```text
https://ai-cockpit.example.com
```

### 20.3 Services

```text
caddy
api
postgres
redis
worker
admin-web
```

### 20.4 Security Requirements

```text
HTTPS only
admin password
optional TOTP
device token
collector token
rate limit
audit log
regular backup
```

---

## 21. Milestones

### M0 — Requirement Freeze

Deliverables:

```text
product_spec.md
architecture.md
api_contract.md
security_model.md
```

Acceptance:

```text
hardware confirmed
public-cloud architecture confirmed
read-only boundary confirmed
no credential upload confirmed
```

---

### M1 — Cloud Foundation

Deliverables:

```text
FastAPI server
PostgreSQL schema
Redis cache
Device Frame API
basic Admin page
```

Acceptance:

```text
/api/device/{id}/frame returns mock frame
/admin shows mock state
```

---

### M2 — ESP32 Mock Frame Display

Deliverables:

```text
ESP32 firmware
HTTPS frame fetching
Fleet / Focus / Quota / Alert scenes
RGB light effects
```

Acceptance:

```text
cloud scene change updates ESP32 within 5 seconds
network loss shows OFFLINE
network recovery restores display
```

---

### M3 — Collector MVP

Deliverables:

```text
Local Collector
manual session push
manual quota push
HMAC signature
offline cache
```

Acceptance:

```text
Collector push updates cloud frame
ESP32 display changes accordingly
```

---

### M4 — Codex / Claude Integration

Deliverables:

```text
Codex hooks adapter
Claude adapter
CodexBar / Cockpit Tools adapter optional
Provider normalization
```

Acceptance:

```text
Codex prompt -> THINKING
Codex tool use -> CODING
approval required -> WAITING
completed task -> DONE
quota displayed with confidence
```

---

### M5 — Advanced Experience

Deliverables:

```text
theme system
screen simulator
OTA
pairing mode
device heartbeat
alert rules
```

Acceptance:

```text
theme can be changed from admin
device reports heartbeat
OTA works
first boot can configure Wi-Fi/server
```

---

### M6 — Enclosure Productization

Deliverables:

```text
enclosure design
assembly guide
screen cutout
RGB light diffuser
USB-C port cutout
```

Acceptance:

```text
plug-and-play
product-like appearance
24-hour stable operation
```

---

## 22. Acceptance Criteria

### 22.1 Functional

```text
supports public domain
ESP32 fetches frame over HTTPS
supports multiple sessions
supports multiple accounts
supports 5h and weekly quota windows
supports quota confidence
supports WAITING / ERROR alerts
supports OFFLINE / STALE fallback
supports Collector push
cloud stores no sensitive credentials
```

### 22.2 Visual

```text
readable on 172×320 screen
not crowded
clear status word
quota page readable
RGB not too bright
smooth animation
weak-network state obvious
```

### 22.3 Stability

```text
ESP32 runs for 24 hours
device recovers after cloud restart
device reconnects after Wi-Fi loss
malformed JSON does not crash device
Collector can replay offline events
duplicate events do not corrupt state
```

### 22.4 Security

```text
no cookie/token in cloud database
Collector push is signed
Device API is read-only
Admin is protected by login
events are auditable
sensitive fields are sanitized
```

---

## 23. Risks

### 23.1 High Risks

```text
quota may not be precisely readable
provider schemas are inconsistent
public cloud exposure increases security risk
ESP32 HTTPS certificate maintenance is tricky
small screen may become overloaded
```

### 23.2 Medium Risks

```text
Codex / Claude hook fields may change
Cockpit Tools / CodexBar data source may change
Collector may go offline
events may arrive out of order
Redis and PostgreSQL state may diverge
```

### 23.3 Low Risks

```text
RGB too bright
screen brightness unsuitable
enclosure heat
font readability
```

---

## 24. Risk Mitigation

```text
Quota imprecision:
  use confidence and estimated fields.

Credential leakage:
  keep credentials local; cloud receives summaries only.

Multi-session complexity:
  cloud generates final frame; ESP32 does no sorting.

ESP32 HTTPS instability:
  cache last frame; show OFFLINE / STALE fallback.

Small-screen overload:
  show only top 2 accounts and highest-priority session.

Hook instability:
  hooks are one source; support manual and adapter updates.
```

---

## 25. Subagent Review Tasks

### 25.1 Security Reviewer

```text
Review whether the public cloud architecture leaks OpenAI / Claude / CodexBar / Cockpit Tools credentials.

Check:
1. Are cookies/tokens ever uploaded to cloud?
2. Does Collector push use HMAC signature?
3. Is ESP32 device token read-only?
4. Is replay attack protection implemented?
5. Does database store sensitive prompt / transcript / local path?
6. Does Web Dashboard have login, optional 2FA, and rate limit?

Output:
High / medium / low risk list with remediation suggestions.
```

---

### 25.2 Embedded Firmware Reviewer

```text
Review ESP32-S3-LCD-1.47B firmware design.

Check:
1. Is HTTPS polling frequency reasonable?
2. Is compact JSON smaller than 2KB?
3. Is TLS certificate pinning maintainable?
4. Does UI degrade gracefully under offline / weak network / server error?
5. Is LovyanGFX / LVGL usage appropriate?
6. Is there USB flashing fallback if OTA fails?
7. Could animations cause memory fragmentation or flicker?

Output:
Firmware risks and recommended module boundaries.
```

---

### 25.3 Cloud Backend Reviewer

```text
Review FastAPI + PostgreSQL + Redis cloud architecture.

Check:
1. Is ingest API idempotent?
2. Does event schema support multiple providers?
3. Are quota confidence and display priority decoupled?
4. Does session state machine handle out-of-order events?
5. Are TTL/stale mechanisms implemented?
6. Is device frame API lightweight enough?
7. Is database schema extensible?

Output:
Data model and API improvement suggestions.
```

---

### 25.4 Provider Integration Reviewer

```text
Review Codex, Claude, CodexBar, and Cockpit Tools integration.

Check:
1. Are Codex hooks using stable fields only?
2. Is the design wrongly dependent on transcript format?
3. Are Claude hook fields over-assumed?
4. Should CodexBar / Cockpit Tools data remain local-only?
5. Can multi-account and quota windows be represented with confidence?
6. Is there any automatic account switching or quota evasion design?

Output:
Provider integration risk matrix.
```

---

### 25.5 Product/UI Reviewer

```text
Review 172×320 small-screen UI.

Check:
1. Does multi-session display overload the screen?
2. Is quota page readable?
3. Are Alert / Focus / Fleet / Quota modes sufficient?
4. Are rotation, interruption, and priority rules clear?
5. Is RGB lighting restrained?
6. Can user understand offline / stale / low-confidence quota?

Output:
Final UI page and display-rule recommendations.
```

---

### 25.6 Compliance Reviewer

```text
Review compliance risk of multi-account, multi-quota, multi-provider monitoring.

Check:
1. Does the system attempt to bypass service limits?
2. Does it automatically switch accounts to avoid quota?
3. Does it upload or share account credentials?
4. Does it rely on ToS-sensitive scraping or automation?
5. Should default behavior remain read-only?

Output:
Allowed features, deferred features, and prohibited features.
```

---

## 26. Master Prompt for Implementation Agent

```text
Implement a public-cloud AgentLamp.

Hardware:
- Waveshare ESP32-S3-LCD-1.47B

Firmware requirements:
- ESP32 must not render webpages.
- ESP32 fetches compact JSON frames from /api/device/{device_id}/frame over HTTPS.
- ESP32 renders local scene UI based on frame.scene.
- ESP32 supports Fleet Overview, Focus Session, Quota Cockpit, Alert Page, Offline, and Stale scenes.
- ESP32 controls RGB LED based on frame accent/status.
- Firmware stack: PlatformIO + Arduino-ESP32 + LovyanGFX + ArduinoJson + WiFiClientSecure + HTTPClient + Preferences + RGB LED library.

Cloud requirements:
- Deploy on public server and domain.
- Use FastAPI + PostgreSQL + Redis + Docker + Caddy/HTTPS.
- Aggregate sanitized data from multiple Local Collectors.
- Support Codex, Claude, Cockpit Tools, CodexBar, and manual provider.
- Support multiple accounts, multiple providers, multiple sessions, 5-hour quota, weekly quota, reset countdown, waiting alerts, and error alerts.
- Generate final compact device frames.
- ESP32 must not sort sessions or calculate complex quota risk.

Security requirements:
- Cloud must not store complete login state, browser cookies, refresh tokens, full transcripts, full prompts, or source code contents.
- Local Collector reads sensitive local data, sanitizes it, signs it, and pushes only summaries.
- Collector push must use HTTPS, HMAC signature, timestamp anti-replay, offline cache, and retry.
- Device API must be read-only.
- Admin Dashboard must require authentication.

Product boundary:
- v1 is read-only monitoring only.
- Do not implement automatic account switching.
- Do not implement quota evasion.
- Do not proxy provider requests through public cloud.

Goal:
- Build a plug-and-play physical AI Agent Cockpit that can be placed in a custom enclosure and used as a premium desktop status object.
```

---

## 27. Explicit Non-Goals

Do not implement in v1:

```text
ESP32 web browser
automatic Codex / Claude account switching
automatic quota evasion
automatic task wake-up that consumes quota
cloud storage of account cookies/tokens
cloud storage of full prompt/transcript
large-screen complex dashboard on ESP32
complex account logic on ESP32
```

---

## 28. Final Architecture Summary

```text
Local Collector
  -> sanitized push

AgentLamp Cloud
  -> multi-account aggregation
  -> multi-session aggregation
  -> quota confidence
  -> display priority
  -> compact device frame

ESP32-S3-LCD-1.47B
  -> HTTPS frame pull
  -> Scene UI
  -> RGB ambient light
  -> OFFLINE / STALE fallback
```

Core value:

```text
A physical AI Agent Cockpit for observing multi-account quota, multi-session status, waiting approvals, and error alerts.
```

Product goal:

```text
premium
stable
secure
read-only
extensible
gift-worthy
```
