// AgentLamp firmware — Waveshare ESP32-S3-LCD-1.47B.
//
// Pipeline: WiFi join -> poll GET {FRAME_BASE_URL}/api/v1/device/{id}/frame with
// Bearer token every ~4s -> validate (size/schema/unknown-field) -> render the
// scene on the ST7789 172x320 -> drive the onboard WS2812 to the status accent.
// Offline after 3 consecutive failures; Stale when the cached frame outlives its
// TTL grace by LOCAL elapsed millis (not RTC). See docs/firmware/firmware_contract.md.

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiMulti.h>     // multi-network: auto-join whichever stored SSID is in range (strongest)
#include <HTTPClient.h>
#include <ESPmDNS.h>       // resolve the frame server's <host>.local -> current IP (DHCP-drift proof)

#include "config.h"        // FRAME_BASE_URL default / DEVICE_ID / DEVICE_TOKEN / RELAY_MODE (no secrets)
#include "provisioning.h"  // runtime SoftAP captive portal + NVS creds (Preferences "agentlamp")
#include "theme.h"
#include "frame.h"
#include "display.h"
#include "renderer.h"
#include "led.h"
#include "relay.h"         // relay-mode HTTPS transport (WiFiClientSecure + pinned CA + NTP)

#ifndef FIRMWARE_VERSION
#define FIRMWARE_VERSION "v0.1"
#endif

// ---- timing ----
static constexpr unsigned long POLL_INTERVAL_MS = 4000;   // ~4s (contract 3-5s)
static constexpr unsigned long HTTP_TIMEOUT_MS  = 2000;   // 2s per request
static constexpr uint8_t       FAIL_BEFORE_OFFLINE = 3;   // 3 fails -> Offline
// Self-heal: after a long run of TRANSPORT failures the WiFi/LWIP stack is usually
// wedged (observed stuck at code=-1 for minutes; WiFi.reconnect() can't clear it),
// so reboot to recover instead of staying dark until a manual reset. At the 4s poll
// cadence this is ~5 min; a genuine long server outage just reboots periodically and
// keeps retrying (harmless). Kept under the uint8_t 255 clamp.
static constexpr uint8_t       FAIL_BEFORE_REBOOT  = 75;  // ~5 min of transport failure -> self-heal reboot
static constexpr unsigned long WIFI_JOIN_TIMEOUT_MS = 12000;
static constexpr int           WIFI_JOIN_ATTEMPTS   = 5;     // retry known-good NVS creds before the portal
static constexpr uint32_t      MDNS_QUERY_TIMEOUT_MS = 1500; // per mDNS host lookup (boot + on-failure re-resolve)
// USB transport: when the Mac pushes frames over the USB-CDC cable (usb_bridge), prefer it and
// let WiFi go dormant — a USB-tethered lamp then needs NO WiFi and works on any network the
// laptop is on. WiFi polling resumes only if USB frames stop arriving for this long.
static constexpr unsigned long USB_FRESH_MS = 12000;

// ---- BOOT-button re-provisioning ----
// GPIO0 is the onboard BOOT button (INPUT_PULLUP; pressed = LOW). Holding it
// LOW for ~3s clears the NVS WiFi creds and reboots into the captive portal.
static constexpr int           PIN_BOOT_BTN = 0;
static constexpr unsigned long REPROVISION_HOLD_MS = 3000;

// ---- hardware ----
static AgentLampDisplay gfx;
static Renderer         render(gfx);
static StatusLed        led;
static Provisioning     prov;
static WiFiMulti        wifiMulti;   // fed from NVS multi-net store; joins the strongest known SSID
static RelayClient      relay;       // relay-mode HTTPS transport (only used when base url is https://)

// ---- state ----
static Frame         cached;                 // last valid frame
static bool          haveCached = false;
static unsigned long lastFetchOkMs = 0;      // millis() of last good fetch
static unsigned long lastPollMs = 0;
static unsigned long lastUsbFrameMs = 0;     // millis() of last valid frame read over USB
static uint8_t       consecutiveFails = 0;
static unsigned long pollIntervalMs = POLL_INTERVAL_MS;
static bool          pairingRequired = false; // 401/403/404 -> stop polling
static int           lastRelayBlock = 0;       // 0 none / -10 CA invalid / -11 clock unsynced (relay fail-closed)
static Scene         shownScene = Scene::UNKNOWN;
static unsigned long shownSeq = (unsigned long)-1;
static bool          provisioningHalt = false; // portal active: hold the SETUP scene
static unsigned long lastTimeRepaintMs = 0;    // coarse clock for time-bearing scenes

// runtime creds (loaded from NVS at boot; server URL drives the poll target)
static Provisioning::Creds creds;
static char          frameBaseUrl[96] = FRAME_BASE_URL;   // effective base URL

// LED helpers — use the VIVID led palette (saturated), not the soft LCD palette,
// or a bare WS2812 looks washed-out/pale.
static void ledForStatus(Status s) {
  Rgb c = ledStatusColor(s);
  led.setColor(c.r, c.g, c.b);
}
static void ledForAccent(Accent a) {
  Rgb c = ledAccentColor(a);
  led.setColor(c.r, c.g, c.b);
}

// ----- WiFi (multi-network) -----
// True once NVS holds at least one stored network. Drives whether we even try to join.
static bool haveWifiCreds() { return creds.hasWifi; }

// ----- relay provisioning gate (RELAY_MODE only) -----
// A RELAY_MODE image has NO compiled base URL or token (config.h: both empty in relay mode, I3);
// they come from NVS provisioning. config.h promises an un-provisioned relay orb shows the
// SETUP/pairing scene — but an empty FRAME_BASE_URL would otherwise fall into fetchFrame()'s
// local-HTTP branch (scheme != "https://") and dial a HOSTLESS URL ("http:///api/v1/...") →
// transport fail → OFFLINE, contradicting the contract. This predicate gates that: a relay
// build is "provisioned" only once BOTH the https relay base AND the device token are present.
// (Local mode is always considered provisioned — it ships a working compiled default.)
static bool relayUnprovisioned() {
#if defined(RELAY_MODE) && RELAY_MODE
  bool hasHttpsBase = (strncmp(frameBaseUrl, "https://", 8) == 0);
  bool hasToken     = (creds.token[0] != '\0');
  return !(hasHttpsBase && hasToken);
#else
  return false;   // local mode: compiled http base + dev token = always provisioned
#endif
}

// (Re)load all stored networks from `creds` into the WiFiMulti list. WiFiMulti scans on run()
// and joins the STRONGEST network whose SSID it knows — so the orb auto-follows the owner
// across home / office / phone-hotspot without re-provisioning, as long as each was added once.
static void loadWifiNetworks() {
  WiFi.mode(WIFI_STA);
  for (uint8_t i = 0; i < creds.netCount; i++) {
    if (creds.nets[i].ssid[0]) {
      wifiMulti.addAP(creds.nets[i].ssid, creds.nets[i].pass);
      Serial.printf("wifi           : known net [%u] %s\n", (unsigned)i, creds.nets[i].ssid);
    }
  }
}

// Attempt to join the strongest known network within the timeout. Returns true on connect.
static bool wifiConnect() {
  if (!haveWifiCreds()) return false;
  // WiFiMulti.run(timeout) scans + joins the strongest known AP; loop in case the first scan
  // misses an AP that is briefly absent (matches the single-net retry behaviour we replaced).
  unsigned long t0 = millis();
  while ((millis() - t0) < WIFI_JOIN_TIMEOUT_MS) {
    if (wifiMulti.run(WIFI_JOIN_TIMEOUT_MS) == WL_CONNECTED) return true;
    if (WiFi.status() == WL_CONNECTED) return true;
    delay(200);
  }
  return WiFi.status() == WL_CONNECTED;
}

// ----- mDNS server discovery -----
// Resolve the frame server's CURRENT IP from FRAME_MDNS_HOST (<host>.local). macOS/Bonjour
// keeps that name mapped to the host's live DHCP IP, so this auto-follows IP changes — the
// fix for "unplugged / rebooted / DHCP renewed -> orb stuck offline on a stale IP". On
// success frameBaseUrl is (re)pointed at the resolved IP; on failure frameBaseUrl is left
// as-is (the NVS / compiled FRAME_BASE_URL fallback). Requires WiFi connected.
static bool mdnsStarted = false;

static bool resolveServerViaMdns() {
  if (FRAME_MDNS_HOST[0] == '\0') return false;          // mDNS disabled (always so in relay mode)
  if (strncmp(frameBaseUrl, "https://", 8) == 0) return false;  // relay mode: fixed https url, never mDNS-rewritten
  if (WiFi.status() != WL_CONNECTED) return false;
  if (!mdnsStarted) {
    if (!MDNS.begin("agentlamp-orb")) return false;      // also advertises the orb itself
    mdnsStarted = true;
  }
  IPAddress ip = MDNS.queryHost(FRAME_MDNS_HOST, MDNS_QUERY_TIMEOUT_MS);
  if ((uint32_t)ip == 0) return false;                   // not found this round
  char url[96];
  snprintf(url, sizeof(url), "http://%u.%u.%u.%u:%u",
           ip[0], ip[1], ip[2], ip[3], (unsigned)FRAME_SERVER_PORT);
  if (strcmp(url, frameBaseUrl) != 0) {
    strlcpy(frameBaseUrl, url, sizeof(frameBaseUrl));
    Serial.print(F("mdns           : server -> ")); Serial.println(frameBaseUrl);
  }
  return true;
}

// ----- captive-portal provisioning -----
// Render the SETUP scene + start the SoftAP portal, then latch provisioningHalt
// so loop() services DNS/HTTP without renderCurrent() repainting over the scene.
static void enterPortal(const char* title, const char* footerLine) {
  Serial.println(F("provisioning   : starting SoftAP captive portal"));
  prov.beginPortal(frameBaseUrl);
  // Show the LIVE per-device SSID ("AgentLamp-Setup-<suffix>") so the runbook's instruction is
  // literally correct on screen; beginPortal() has already computed it.
  char joinLine[40];
  snprintf(joinLine, sizeof(joinLine), "join %s", prov.activeApSsid());
  render.wifiConfig(title, "SETUP",
                    joinLine,
                    footerLine ? footerLine : "browse " AP_PORTAL_IP);
  ledForAccent(Accent::CYAN);
  provisioningHalt = true;
}

// Hold BOOT (GPIO0) LOW for ~3s -> wipe NVS creds + reboot into the portal.
static void checkReprovisionButton() {
  static unsigned long pressedSince = 0;
  if (digitalRead(PIN_BOOT_BTN) == LOW) {       // pressed
    if (pressedSince == 0) pressedSince = millis();
    else if (millis() - pressedSince >= REPROVISION_HOLD_MS) {
      Serial.println(F("provisioning   : BOOT held 3s -> clearing creds + reboot"));
      Provisioning::clearCreds();
      led.setColor(C_READ.r, C_READ.g, C_READ.b);
      delay(150);
      ESP.restart();
    }
  } else {
    pressedSince = 0;                            // released -> reset hold timer
  }
}

// ----- frame fetch -----
// Returns: 0 = ok (frame filled), >0 = HTTP status (error), <0 = transport/local
// (-1 transport, -2 oversize, -3 bad json, RELAY_ERR_NO_CA/-10, RELAY_ERR_NO_TIME/-11).
//
// Transport selection is by the BASE URL SCHEME, set from NVS provisioning (I3): an https://
// base routes through the verified relay transport (WiFiClientSecure + pinned CA + NTP-before-
// TLS); an http:// base uses plain HTTP over the LAN with mDNS IP auto-follow (local mode).
// The relay path FAILS CLOSED — it never downgrades to unverified TLS.
static int fetchFrame(Frame& out) {
  if (WiFi.status() != WL_CONNECTED) return -1;

#if defined(RELAY_MODE) && RELAY_MODE
  // RELAY_MODE backstop: a relay image must NEVER dial a non-https / hostless URL. If we ever
  // reach here un-provisioned (empty base or non-https), refuse the fetch as a transport failure
  // rather than constructing "http:///api/v1/..." and timing out into OFFLINE. The setup()/loop()
  // gates normally keep us in the SETUP portal before this point; this is the last-line guard.
  if (strncmp(frameBaseUrl, "https://", 8) != 0) return -1;
#endif

  // Relay mode: https base url -> verified TLS transport (relay.h owns NTP + pinned CA bundle).
  if (strncmp(frameBaseUrl, "https://", 8) == 0) {
    relay.configure(frameBaseUrl, DEVICE_ID, creds.token);
    return relay.fetchFrame(out, pollIntervalMs, POLL_INTERVAL_MS);
  }

  // Local mode: plain HTTP over the LAN.
  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.setConnectTimeout(HTTP_TIMEOUT_MS);

  char url[160];
  snprintf(url, sizeof(url), "%s/api/v1/device/%s/frame", frameBaseUrl, DEVICE_ID);

  if (!http.begin(url)) return -1;

  // Local-mode token: the compiled local default (relay tokens are NVS-only, used above).
  http.addHeader("Authorization", "Bearer " DEVICE_TOKEN);
  http.addHeader("Accept", "application/json");
  http.addHeader("X-Frame-Schema-Version", "1");

  // Capture Retry-After so the 429 backoff can honour it. HTTPClient only
  // retains headers named here (otherwise header() always returns "" because
  // _headerKeysCount stays 0). Must be set before GET().
  static const char* kCollect[] = {"Retry-After"};
  http.collectHeaders(kCollect, 1);

  int code = http.GET();
  if (code <= 0) { http.end(); return -1; }     // transport error

  if (code != 200) {
    // 429: honour Retry-After / back off; others handled by caller
    if (code == 429) {
      String ra = http.header("Retry-After");
      unsigned long backoff = ra.length() ? (unsigned long)ra.toInt() * 1000UL : 0;
      pollIntervalMs = max(POLL_INTERVAL_MS * 2, max(backoff, 60000UL));
    }
    http.end();
    return code;
  }

  // Bounded body read. getSize() returns the Content-Length, or -1 for a
  // chunked / no-Content-Length response — so the old `getSize() > cap` guard
  // was BYPASSED for chunked bodies (-1 > 2048 is false), and getString() would
  // then stream an unbounded body into a heap String and OOM the ESP32. Instead
  // stream straight into a fixed stack buffer and reject as soon as it overflows,
  // which bounds RAM regardless of Content-Length presence.
  int declared = http.getSize();
  if (declared > (int)FRAME_MAX_BYTES) { http.end(); return -2; }  // oversized -> reject early

  static char buf[FRAME_MAX_BYTES + 1];   // 2049 B, one shot of static RAM (not per-call heap)
  size_t got = 0;
  WiFiClient* stream = http.getStreamPtr();
  if (!stream) { http.end(); return -1; }

  unsigned long readStart = millis();
  while (http.connected() && got <= FRAME_MAX_BYTES) {
    size_t avail = stream->available();
    if (avail) {
      size_t room = (FRAME_MAX_BYTES + 1) - got;        // leave no room past 2049
      size_t toRead = avail < room ? avail : room;
      int n = stream->readBytes(buf + got, toRead);
      if (n <= 0) break;
      got += (size_t)n;
      if (got > FRAME_MAX_BYTES) { http.end(); return -2; }  // body exceeds 2 KB -> reject
    } else {
      if (declared >= 0 && (int)got >= declared) break;       // got the whole declared body
      if (millis() - readStart > HTTP_TIMEOUT_MS) break;      // chunked / slow source: bail
      delay(1);
    }
  }
  http.end();

  if (got == 0 || got > FRAME_MAX_BYTES) return -2;

  if (!parseFrame(buf, got, out)) return -3;  // bad json/schema
  return 0;
}

// ----- USB-CDC frame transport -----
// The Mac-side usb_bridge writes one frame JSON per line to the device's serial RX. USB-CDC is
// full-duplex, so this Mac->device direction is independent of our TX log output. A valid frame
// is applied exactly like a good fetch — so a USB-tethered lamp needs NO WiFi and works on any
// network the laptop is on. Returns true iff a frame was applied this call.
static bool readUsbFrame() {
  static char line[FRAME_MAX_BYTES + 1];
  static size_t len = 0;
  bool applied = false;
  while (Serial.available() > 0) {
    int ci = Serial.read();
    if (ci < 0) break;
    char c = (char)ci;
    if (c == '\n' || c == '\r') {
      if (len > 0) {
        Frame fresh;
        if (parseFrame(line, len, fresh)) {
          unsigned long now = millis();
          cached = fresh; haveCached = true;
          lastFetchOkMs = now; lastUsbFrameMs = now;
          consecutiveFails = 0; pairingRequired = false;
          Serial.printf("frame ok       : via=usb scene=%d seq=%lu\n", (int)fresh.scene, fresh.seq);
          applied = true;
        }
        len = 0;       // line consumed (or unparseable) — start the next one
      }
    } else if (len < FRAME_MAX_BYTES) {
      line[len++] = c;
    } else {
      len = 0;         // overlong line w/o newline: drop + resync, never overflow
    }
  }
  return applied;
}

// Read USB frames for up to `ms`, returning true as soon as one is applied. Used at boot so a
// tethered lamp comes straight up on USB without waiting on (or needing) WiFi.
static bool probeUsbFrame(unsigned long ms) {
  unsigned long t0 = millis();
  while (millis() - t0 < ms) {
    if (readUsbFrame()) return true;
    delay(10);
  }
  return false;
}

static bool usbFresh(unsigned long now) {
  return lastUsbFrameMs != 0 && (now - lastUsbFrameMs) < USB_FRESH_MS;
}

// pick the effective render scene from frame + local staleness/offline state
static Scene effectiveScene(unsigned long now) {
  if (pairingRequired) return Scene::DIAGNOSTICS;
  // Relay fail-closed (contract §TLS: TLS-validation / precondition failure -> Diagnostics,
  // keep cached frame, retry with backoff — NEVER unverified HTTP). Shown only once we've
  // accumulated a few consecutive fails so a single transient hiccup doesn't flash the banner.
  if (lastRelayBlock != 0 && consecutiveFails >= FAIL_BEFORE_OFFLINE) return Scene::DIAGNOSTICS;
  if (consecutiveFails >= FAIL_BEFORE_OFFLINE) return Scene::OFFLINE;
  if (!haveCached) return Scene::BOOT;

  // staleness from LOCAL elapsed millis (not RTC): elapsed > ttl grace -> Stale.
  unsigned long elapsed = now - lastFetchOkMs;
  unsigned long graceMs = (cached.ttl * 1000UL) * 3UL;   // ttl x3 grace window
  if (elapsed > graceMs) return Scene::STALE;

  // otherwise honour the frame's declared scene (focus is the Live view)
  switch (cached.scene) {
    case Scene::ALERT:  return Scene::ALERT;
    case Scene::FLEET:  return Scene::FLEET;
    case Scene::QUOTA:  return Scene::QUOTA;
    case Scene::FOCUS:  return Scene::FOCUS;
    case Scene::SLEEP:  return Scene::SLEEP;
    case Scene::BOOT:   return Scene::BOOT;
    default:            return Scene::FOCUS;   // sensible default Live view
  }
}

static void renderCurrent(unsigned long now) {
  char clock[8];
  Renderer::uptimeClock(clock, sizeof(clock), now);

  Scene sc = effectiveScene(now);

  // Offline/Stale carry "last seen Ns ago" / "updated Nm ago" + the top-bar
  // uptime clock — those advance with wall time, not with scene/seq. Without a
  // coarse periodic repaint that text freezes the moment we park in Offline/Stale.
  bool timeBearing = (sc == Scene::OFFLINE || sc == Scene::STALE);
  bool tick = timeBearing && (now - lastTimeRepaintMs >= 1000UL);

  // only repaint when the scene or the frame seq changes (anti-flicker), or on
  // the coarse tick for time-bearing scenes.
  bool changed = (sc != shownScene) || (haveCached && cached.seq != shownSeq) || tick;
  if (!changed) return;
  if (tick || sc != shownScene) lastTimeRepaintMs = now;
  shownScene = sc;
  shownSeq   = haveCached ? cached.seq : (unsigned long)-1;

  switch (sc) {
    case Scene::DIAGNOSTICS:
      if (pairingRequired) {              // 401/403/404 -> re-pair
        render.message("PAIRING", C_ERR, "REQUIRED", "re-pair on laptop",
                       "re-enroll device token");
        led.setColor(C_ERR.r, C_ERR.g, C_ERR.b);
      } else if (lastRelayBlock == RELAY_ERR_NO_TIME) {  // clock not synced -> can't verify cert
        render.message("SECURE", C_WAIT, "syncing clock", "no NTP yet",
                       "TLS needs the time");
        led.setColor(C_WAIT.r, C_WAIT.g, C_WAIT.b);
      } else {                            // RELAY_ERR_NO_CA -> CA bundle missing/invalid
        render.message("SECURE", C_ERR, "cert pin failed", "refreshing CA",
                       "won't use insecure tls");
        led.setColor(C_ERR.r, C_ERR.g, C_ERR.b);
      }
      break;

    case Scene::OFFLINE:
      render.offline(lastFetchOkMs ? (now - lastFetchOkMs) : 0, clock);
      ledForStatus(Status::OFFLINE);
      break;

    case Scene::STALE:
      render.stale(cached, now - lastFetchOkMs, clock);
      led.setColor(C_STALE.r / 3, C_STALE.g / 3, C_STALE.b / 3);  // dim white
      break;

    case Scene::ALERT: {
      // Distinguish the 3 alert types by hue on BOTH the ring/word and the LED so
      // they're glanceable: WAITING = amber, ERROR = red, QUOTA-danger = orange.
      // (A quota alert has status=IDLE; without this it would glow blue and read
      // the same red as ERROR.)
      Rgb ring, ledc;
      if (cached.status == Status::WAITING)     { ring = C_WAIT;           ledc = {255, 150, 0}; }
      else if (cached.status == Status::ERROR)  { ring = C_ERR;            ledc = {255,   0, 0}; }
      else                                       { ring = {0xff,0x7a,0x18}; ledc = {255,  70, 0}; }  // quota = orange
      render.alert(cached, ring, clock);
      led.setColor(ledc.r, ledc.g, ledc.b);
      break;
    }
    case Scene::FLEET: {
      Rgb a = accentColor(cached.accent);
      render.fleet(cached, a, clock);
      ledForAccent(cached.accent);
      break;
    }
    case Scene::QUOTA: {
      Rgb a = accentColor(cached.accent);
      render.quota(cached, a, clock);
      ledForAccent(cached.accent);
      break;
    }
    case Scene::SLEEP:
      render.message("", C_MUTED, "", "", "all idle");
      led.setColor(C_IDLE.r / 6, C_IDLE.g / 6, C_IDLE.b / 6);
      break;

    case Scene::FOCUS:
    default: {
      Rgb a = (cached.accent == Accent::MUTED) ? statusColor(cached.status)
                                               : accentColor(cached.accent);
      render.focus(cached, a, clock);
      ledForStatus(cached.status);
      break;
    }
  }
}

void setup() {
  // Enlarge the USB-CDC RX FIFO BEFORE begin(): a frame line is ~0.5 KB, but the default RX
  // buffer is only 256 B, so a frame pushed by usb_bridge would be truncated before a full
  // '\n'-line ever forms — readUsbFrame would never see a parseable frame. Must precede begin().
  Serial.setRxBufferSize(FRAME_MAX_BYTES + 256);
  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && (millis() - t0) < 2000) delay(10);

  pinMode(PIN_BOOT_BTN, INPUT_PULLUP);   // BOOT button for re-provisioning

  // Load creds from NVS (Preferences "agentlamp"). server_url falls back to the
  // compile-time FRAME_BASE_URL default when NVS has none.
  creds = Provisioning::loadCreds();
  strlcpy(frameBaseUrl, creds.server, sizeof(frameBaseUrl));
  loadWifiNetworks();   // feed all stored SSIDs into WiFiMulti (joins the strongest in range)

  Serial.println();
  Serial.println(F("=== AgentLamp firmware ==="));
  Serial.print(F("device_id      : ")); Serial.println(DEVICE_ID);
  Serial.print(F("frame_base_url : ")); Serial.println(frameBaseUrl);
  Serial.print(F("transport      : ")); Serial.println(
      strncmp(frameBaseUrl, "https://", 8) == 0 ? F("relay (HTTPS, pinned CA)") : F("local (HTTP/LAN)"));
  Serial.print(F("wifi nets      : ")); Serial.print(creds.netCount);
  Serial.println(haveWifiCreds() ? F(" stored (NVS)") : F(" none -> portal"));
#if defined(BOARD_HAS_PSRAM)
  Serial.print(F("PSRAM size     : ")); Serial.println(ESP.getPsramSize());
#else
  Serial.println(F("PSRAM          : NOT enabled"));
#endif
  Serial.print(F("free heap      : ")); Serial.println(ESP.getFreeHeap());

  // bring up panel + LED
  gfx.init();
  gfx.setRotation(0);          // portrait 172x320
  gfx.setBrightness(200);      // backlight ~80%
  led.begin();

  render.boot(FIRMWARE_VERSION);
  led.setColor(C_STALE.r / 4, C_STALE.g / 4, C_STALE.b / 4);  // dim boot glow

  // USB-FIRST: if the Mac is already pushing frames down the cable (usb_bridge), use that and
  // skip WiFi entirely — a USB-tethered lamp then needs no network config and works on whatever
  // network the laptop is on (or none). WiFi stays as the fallback if USB frames stop.
  if (probeUsbFrame(3000)) {
    Serial.println(F("transport      : USB-CDC (frames over the cable) -> skipping WiFi"));
    shownScene = Scene::BOOT;
    lastPollMs = millis() - pollIntervalMs;
    return;
  }

  // No creds in NVS -> first-boot provisioning portal.
  if (!haveWifiCreds()) {
    Serial.println(F("wifi           : no NVS creds -> captive portal"));
    enterPortal("connect wifi", "browse " AP_PORTAL_IP);
    return;   // loop() services the portal; reboots/joins once creds arrive
  }

  // RELAY_MODE provisioning gate: a relay build with no https relay base / no device token in
  // NVS must open the captive portal (SETUP scene) — NEVER dial a hostless URL into OFFLINE.
  // This honours config.h's promise that an un-provisioned relay orb shows pairing/SETUP.
  if (relayUnprovisioned()) {
    Serial.println(F("relay          : no relay URL/token in NVS -> SETUP (captive portal)"));
    enterPortal("pair relay", "paste token in portal");
    return;   // loop() services the portal; the user pastes the relay URL + token
  }

  // Have creds -> try to join, WITH RETRIES. The NVS creds are known-good (they
  // joined before), so a single transient join timeout after a reboot must NOT
  // drop us to the portal. Retry a few times (showing "connecting · retry n/N")
  // before assuming the creds are actually wrong and re-entering provisioning.
  Serial.print(F("wifi           : joining ")); Serial.println(creds.ssid);
  bool joined = false;
  for (int attempt = 1; attempt <= WIFI_JOIN_ATTEMPTS && !joined; attempt++) {
    if (attempt > 1) {
      char sub[24];
      snprintf(sub, sizeof(sub), "retry %d/%d", attempt, WIFI_JOIN_ATTEMPTS);
      render.message("CONNECTING", C_READ, creds.ssid, sub, "");
      ledForAccent(Accent::CYAN);
      WiFi.disconnect(true);
      delay(400);
    }
    joined = wifiConnect();
    if (!joined)
      Serial.printf("wifi           : join attempt %d/%d failed\n", attempt, WIFI_JOIN_ATTEMPTS);
  }
  if (!joined) {
    Serial.println(F("wifi           : all attempts failed -> captive portal"));
    enterPortal("wifi failed", "hold BOOT 3s to redo");
    return;
  }
  Serial.print(F("wifi           : connected, ip=")); Serial.println(WiFi.localIP());
  Serial.print(F("wifi rssi      : ")); Serial.println(WiFi.RSSI());

  // Discover the server's current IP via mDNS (overrides a stale NVS/compiled IP); falls
  // back silently to frameBaseUrl if mDNS can't resolve this round (re-tried while polling).
  resolveServerViaMdns();

  shownScene = Scene::BOOT;     // force first poll to repaint
  lastPollMs = millis() - pollIntervalMs;   // poll immediately
}

void loop() {
  // BOOT button held ~3s clears NVS creds + reboots into the portal. Checked on
  // every iteration so it works whether we're polling or parked in the portal.
  checkReprovisionButton();

  // PROVISIONING: SoftAP captive portal is up. HOLD the SETUP scene and service
  // DNS + HTTP so the form is reachable; effectiveScene() must NOT repaint over
  // the "join AgentLamp-Setup / 192.168.4.1" instructions the user needs.
  if (provisioningHalt) {
    // If the Mac starts pushing frames over USB while we're parked in the WiFi portal, leave
    // the portal and switch to the USB transport — no WiFi setup needed for a tethered lamp.
    if (readUsbFrame()) {
      Serial.println(F("transport      : USB-CDC frame arrived -> leaving portal, using USB"));
      prov.endPortal();
      provisioningHalt = false;
      shownScene = Scene::BOOT;
      renderCurrent(millis());
      return;
    }
    bool saved = prov.service();   // pumps dns.processNextRequest() + server.handleClient()
    if (saved) {
      // User POSTed creds -> reload from NVS, tear down portal, try to join.
      Serial.println(F("provisioning   : creds received -> attempting join"));
      creds = Provisioning::loadCreds();
      strlcpy(frameBaseUrl, creds.server, sizeof(frameBaseUrl));
      loadWifiNetworks();   // the portal ADDED a network -> re-feed WiFiMulti with the full list
      // brief "connecting" repaint so the LCD isn't stuck on SETUP during join
      render.wifiConfig("connecting", "JOIN", creds.ssid, "please wait");
      delay(400);                  // let the HTTP 200 flush to the phone
      prov.endPortal();
      if (wifiConnect()) {
        Serial.print(F("wifi           : connected, ip=")); Serial.println(WiFi.localIP());
        // Relay gate again: WiFi joined but if this relay build still has no https base / token
        // (user added WiFi but skipped the relay token), stay in SETUP — never dial a hostless
        // URL. Re-raise the portal so the relay token field is reachable.
        if (relayUnprovisioned()) {
          Serial.println(F("relay          : joined WiFi but no relay URL/token -> SETUP again"));
          enterPortal("pair relay", "paste token in portal");
        } else {
          resolveServerViaMdns();   // re-point at the server's current IP after a (re)join
          provisioningHalt = false;
          shownScene = Scene::BOOT;
          lastPollMs = millis() - pollIntervalMs;
        }
      } else {
        // join failed with the new creds: relaunch the portal to re-enter.
        Serial.println(F("wifi           : JOIN FAILED with new creds -> portal again"));
        enterPortal("wifi failed", "browse " AP_PORTAL_IP);
      }
    }
    delay(5);   // keep DNS/HTTP responsive; do NOT fall through to renderCurrent()
    return;
  }

  // USB transport: read any frame the Mac pushed down the cable. When USB is feeding, it is
  // the live source and WiFi goes dormant (no join attempts, no offline, no self-heal reboot).
  readUsbFrame();
  unsigned long now = millis();
  bool usb = usbFresh(now);

  // If we lost WiFi mid-run (have creds) AND USB isn't feeding, try a light reconnect.
  if (!usb && WiFi.status() != WL_CONNECTED && haveWifiCreds()) {
    static unsigned long lastReconnect = 0;
    if (millis() - lastReconnect > 10000) {
      lastReconnect = millis();
      WiFi.reconnect();
    }
  }

  if (!usb && !pairingRequired && (now - lastPollMs) >= pollIntervalMs &&
      WiFi.status() == WL_CONNECTED) {
    lastPollMs = now;

    Frame fresh;
    int r = fetchFrame(fresh);

    if (r == 0) {
      // success
      cached = fresh;
      haveCached = true;
      lastFetchOkMs = now;
      consecutiveFails = 0;
      lastRelayBlock = 0;                  // clear any relay fail-closed banner
      pollIntervalMs = POLL_INTERVAL_MS;   // reset any 429 backoff
      Serial.printf("frame ok       : scene=%d seq=%lu ttl=%lu\n",
                    (int)cached.scene, cached.seq, cached.ttl);
    } else if (r == 401 || r == 403 || r == 404) {
      // pairing required: stop normal polling, show diagnostics
      pairingRequired = true;
      Serial.printf("frame err      : http %d -> PAIRING REQUIRED\n", r);
    } else {
      // transport / oversized / bad-json / 429 / 503 / relay-fail-closed: keep cache, count fails.
      // Clamp so the uint8_t never wraps 255->0 (~17 min of continuous failure)
      // and momentarily drops out of the Offline state.
      if (consecutiveFails < 255) consecutiveFails++;
      Serial.printf("frame fail     : code=%d fails=%u\n", r, consecutiveFails);

      // Track the relay fail-closed precondition so the UI can show WHY (CA missing / clock
      // unsynced) rather than a generic Offline. Cleared on the next good fetch.
      lastRelayBlock = (r == RELAY_ERR_NO_CA || r == RELAY_ERR_NO_TIME) ? r : 0;

      // Relay TLS recovery (contract §TLS): on the fail-closed CA error, or on a run of relay
      // transport failures, try to refresh the pinned bundle from /cacerts so a CA rotation
      // doesn't brick the device. NEVER downgrade to unverified HTTP — refreshCaBundle() itself
      // verifies the relay with the current trust anchor before storing the new bundle.
      bool relayMode = strncmp(frameBaseUrl, "https://", 8) == 0;
      if (relayMode && (r == RELAY_ERR_NO_CA ||
                        (r <= 0 && consecutiveFails % 5 == 0))) {
        Serial.println(F("frame fail     : relay -> attempting /cacerts refresh"));
        relay.refreshCaBundle();
      }

      // Transport failing (r <= 0) in LOCAL mode: the server's IP may have changed (DHCP renew /
      // host moved networks). Re-discover via mDNS every few fails so the orb self-heals in
      // seconds — with no reflash / re-provision. (No-op in relay mode: fixed https url.)
      if (!relayMode && r <= 0 && (consecutiveFails % 3 == 0)) {
        resolveServerViaMdns();
      }
      // Self-heal a wedged network stack: only on TRANSPORT errors (r <= 0) — an
      // HTTP-status error (e.g. 500) is the server's problem and a reboot can't fix
      // it. A reboot reliably clears a stuck WiFi/LWIP stack that reconnect cannot.
      if (r <= 0 && consecutiveFails >= FAIL_BEFORE_REBOOT) {
        Serial.println(F("frame fail     : prolonged transport failure -> self-heal reboot"));
        delay(50);
        ESP.restart();
      }
    }
  }

  renderCurrent(now);

  delay(20);   // cooperative; keeps the loop responsive without busy-spinning
}
