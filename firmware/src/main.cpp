// AgentLamp firmware — Waveshare ESP32-S3-LCD-1.47B.
//
// Pipeline: WiFi join -> poll GET {FRAME_BASE_URL}/api/v1/device/{id}/frame with
// Bearer token every ~4s -> validate (size/schema/unknown-field) -> render the
// scene on the ST7789 172x320 -> drive the onboard WS2812 to the status accent.
// Offline after 3 consecutive failures; Stale when the cached frame outlives its
// TTL grace by LOCAL elapsed millis (not RTC). See docs/firmware/firmware_contract.md.

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>

#include "config.h"        // FRAME_BASE_URL default / DEVICE_ID / DEVICE_TOKEN (no secrets)
#include "provisioning.h"  // runtime SoftAP captive portal + NVS creds (Preferences "agentlamp")
#include "theme.h"
#include "frame.h"
#include "display.h"
#include "renderer.h"
#include "led.h"

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

// ---- state ----
static Frame         cached;                 // last valid frame
static bool          haveCached = false;
static unsigned long lastFetchOkMs = 0;      // millis() of last good fetch
static unsigned long lastPollMs = 0;
static uint8_t       consecutiveFails = 0;
static unsigned long pollIntervalMs = POLL_INTERVAL_MS;
static bool          pairingRequired = false; // 401/403/404 -> stop polling
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

// ----- WiFi -----
// True once NVS holds a non-empty SSID. Drives whether we even try to join.
static bool haveWifiCreds() { return creds.hasWifi; }

// Attempt to join using the NVS creds. Returns true on connect within timeout.
static bool wifiConnect() {
  if (!haveWifiCreds()) return false;
  WiFi.mode(WIFI_STA);
  WiFi.begin(creds.ssid, creds.pass);
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && (millis() - t0) < WIFI_JOIN_TIMEOUT_MS) {
    delay(200);
  }
  return WiFi.status() == WL_CONNECTED;
}

// ----- captive-portal provisioning -----
// Render the SETUP scene + start the SoftAP portal, then latch provisioningHalt
// so loop() services DNS/HTTP without renderCurrent() repainting over the scene.
static void enterPortal(const char* title, const char* footerLine) {
  Serial.println(F("provisioning   : starting SoftAP captive portal"));
  prov.beginPortal(frameBaseUrl);
  render.wifiConfig(title, "SETUP",
                    "join " AP_SSID,
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

// ----- HTTP fetch -----
// Returns: 0 = ok (frame filled), >0 = HTTP status (error), -1 = transport fail.
static int fetchFrame(Frame& out) {
  if (WiFi.status() != WL_CONNECTED) return -1;

  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.setConnectTimeout(HTTP_TIMEOUT_MS);

  char url[160];
  snprintf(url, sizeof(url), "%s/api/v1/device/%s/frame", frameBaseUrl, DEVICE_ID);

  // Relay-mode TLS guard: an https:// base URL passed to http.begin(url) (the
  // single-arg form) would fall into UNVERIFIED TLS on Arduino-ESP32 2.0.x — the
  // contract (firmware_contract.md §TLS) forbids unverified relay TLS. Until
  // WiFiClientSecure + setCACert(pinned root) lands, reject https:// outright so
  // we never silently talk plaintext-trust to a relay. v1 is local-mode http://.
  if (strncmp(url, "https://", 8) == 0) {
    Serial.println(F("frame err      : https relay needs pinned CA (unimplemented) -> refusing"));
    return -1;
  }
  if (!http.begin(url)) return -1;

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

// pick the effective render scene from frame + local staleness/offline state
static Scene effectiveScene(unsigned long now) {
  if (pairingRequired) return Scene::DIAGNOSTICS;
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
    case Scene::DIAGNOSTICS:   // pairing required (401/403/404)
      render.message("PAIRING", C_ERR, "REQUIRED", "re-pair on laptop",
                     "agentlamp device pair");
      led.setColor(C_ERR.r, C_ERR.g, C_ERR.b);
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
  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && (millis() - t0) < 2000) delay(10);

  pinMode(PIN_BOOT_BTN, INPUT_PULLUP);   // BOOT button for re-provisioning

  // Load creds from NVS (Preferences "agentlamp"). server_url falls back to the
  // compile-time FRAME_BASE_URL default when NVS has none.
  creds = Provisioning::loadCreds();
  strlcpy(frameBaseUrl, creds.server, sizeof(frameBaseUrl));

  Serial.println();
  Serial.println(F("=== AgentLamp firmware ==="));
  Serial.print(F("device_id      : ")); Serial.println(DEVICE_ID);
  Serial.print(F("frame_base_url : ")); Serial.println(frameBaseUrl);
  Serial.print(F("wifi creds     : ")); Serial.println(haveWifiCreds() ? F("present (NVS)") : F("none -> portal"));
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

  // No creds in NVS -> first-boot provisioning portal.
  if (!haveWifiCreds()) {
    Serial.println(F("wifi           : no NVS creds -> captive portal"));
    enterPortal("connect wifi", "browse " AP_PORTAL_IP);
    return;   // loop() services the portal; reboots/joins once creds arrive
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
    bool saved = prov.service();   // pumps dns.processNextRequest() + server.handleClient()
    if (saved) {
      // User POSTed creds -> reload from NVS, tear down portal, try to join.
      Serial.println(F("provisioning   : creds received -> attempting join"));
      creds = Provisioning::loadCreds();
      strlcpy(frameBaseUrl, creds.server, sizeof(frameBaseUrl));
      // brief "connecting" repaint so the LCD isn't stuck on SETUP during join
      render.wifiConfig("connecting", "JOIN", creds.ssid, "please wait");
      delay(400);                  // let the HTTP 200 flush to the phone
      prov.endPortal();
      if (wifiConnect()) {
        Serial.print(F("wifi           : connected, ip=")); Serial.println(WiFi.localIP());
        provisioningHalt = false;
        shownScene = Scene::BOOT;
        lastPollMs = millis() - pollIntervalMs;
      } else {
        // join failed with the new creds: relaunch the portal to re-enter.
        Serial.println(F("wifi           : JOIN FAILED with new creds -> portal again"));
        enterPortal("wifi failed", "browse " AP_PORTAL_IP);
      }
    }
    delay(5);   // keep DNS/HTTP responsive; do NOT fall through to renderCurrent()
    return;
  }

  // If we lost WiFi mid-run (have creds), idle — try a light reconnect.
  if (WiFi.status() != WL_CONNECTED && haveWifiCreds()) {
    static unsigned long lastReconnect = 0;
    if (millis() - lastReconnect > 10000) {
      lastReconnect = millis();
      WiFi.reconnect();
    }
  }

  unsigned long now = millis();

  if (!pairingRequired && (now - lastPollMs) >= pollIntervalMs &&
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
      pollIntervalMs = POLL_INTERVAL_MS;   // reset any 429 backoff
      Serial.printf("frame ok       : scene=%d seq=%lu ttl=%lu\n",
                    (int)cached.scene, cached.seq, cached.ttl);
    } else if (r == 401 || r == 403 || r == 404) {
      // pairing required: stop normal polling, show diagnostics
      pairingRequired = true;
      Serial.printf("frame err      : http %d -> PAIRING REQUIRED\n", r);
    } else {
      // transport / oversized / bad-json / 429 / 503: keep cache, count failures.
      // Clamp so the uint8_t never wraps 255->0 (~17 min of continuous failure)
      // and momentarily drops out of the Offline state.
      if (consecutiveFails < 255) consecutiveFails++;
      Serial.printf("frame fail     : code=%d fails=%u\n", r, consecutiveFails);
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
