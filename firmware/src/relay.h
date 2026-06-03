// AgentLamp relay-mode HTTPS transport.
//
// Local mode (config.h FRAME_BASE_URL = http://<lan>:8787) talks plain HTTP over the LAN and
// auto-follows the server IP via mDNS — see main.cpp fetchFrame(). RELAY MODE instead polls a
// FIXED https:// relay URL provisioned into NVS, over a verified TLS connection. The device's
// backend URL never changes (the whole point: switch WiFi / switch computer without re-pairing
// the orb), so this file owns only the secure transport, not discovery.
//
// Contract (firmware_contract.md §"TLS / Certificate Lifecycle"):
//   - WiFiClientSecure with a PINNED ROOT CA BUNDLE (2-3 long-lived roots, ca_bundle.h).
//   - NTP/SNTP time-sync BEFORE the first handshake — mbedTLS rejects a cert as "not yet valid"
//     if the clock is at the 1970 epoch, so the clock MUST be set first or every handshake fails.
//   - GET /api/v1/device/{id}/cacerts refresh path so a CA rotation does not brick the device:
//     the fresh bundle is stored in NVS and WINS over the compiled ca_bundle.h fallback.
//   - On TLS validation failure: NEVER fall back to unverified HTTP. Fail closed → Diagnostics.
//
// I3: NO single-machine literals here. The relay URL + device token come from NVS provisioning
// (Provisioning::Creds.server / .token). This file hardcodes nothing host-specific.

#pragma once

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <time.h>

#include "ca_bundle.h"
#include "frame.h"

// ── tunables ────────────────────────────────────────────────────────────────────────────
#ifndef RELAY_HTTP_TIMEOUT_MS
#define RELAY_HTTP_TIMEOUT_MS 4000     // TLS handshake is heavier than plain HTTP; allow more
#endif
#ifndef NTP_SYNC_TIMEOUT_MS
#define NTP_SYNC_TIMEOUT_MS 8000       // wall-clock budget to acquire NTP before first handshake
#endif
// A cert is "valid" only after its notBefore; ISRG Root X1 notBefore is 2015. Anything past a
// sane floor proves SNTP actually set the clock (not still at the 1970 epoch).
#ifndef NTP_MIN_EPOCH
#define NTP_MIN_EPOCH 1700000000UL     // 2023-11-14; clock must be at least this to be "synced"
#endif

// Fetch-result codes mirror main.cpp fetchFrame(): 0 ok, >0 HTTP status, <0 transport/local.
// -10 = CA bundle not usable (fail-closed), -11 = clock not synced (refuse handshake).
static constexpr int RELAY_ERR_NO_CA   = -10;
static constexpr int RELAY_ERR_NO_TIME = -11;

class RelayClient {
 public:
  // baseUrl: provisioned https relay base (no trailing slash), e.g. https://relay.example.dev
  // token  : device bearer token (NVS, never compiled in).
  void configure(const char* baseUrl, const char* deviceId, const char* token) {
    strlcpy(_base, baseUrl ? baseUrl : "", sizeof(_base));
    strlcpy(_id,   deviceId ? deviceId : "", sizeof(_id));
    strlcpy(_token, token ? token : "", sizeof(_token));
  }

  bool isHttps() const { return strncmp(_base, "https://", 8) == 0; }

  // ── NTP / SNTP ──────────────────────────────────────────────────────────────────────
  // Kick off SNTP and BLOCK (bounded) until the clock crosses NTP_MIN_EPOCH. Must run after
  // WiFi is up and BEFORE the first TLS handshake. Idempotent: returns fast once synced.
  // Uses pool.ntp.org + a couple of anycast servers — no single-machine NTP host (I3).
  bool ensureTimeSynced() {
    if (timeIsSane()) return true;
    if (!_sntpStarted) {
      // UTC; cert validity is UTC. (gmtOffset=0, dstOffset=0.)
      configTime(0, 0, "pool.ntp.org", "time.cloudflare.com", "time.google.com");
      _sntpStarted = true;
      Serial.println(F("relay ntp      : SNTP started (pool.ntp.org / cloudflare / google)"));
    }
    unsigned long t0 = millis();
    while (!timeIsSane() && (millis() - t0) < NTP_SYNC_TIMEOUT_MS) {
      delay(150);
    }
    bool ok = timeIsSane();
    Serial.printf("relay ntp      : %s (epoch=%lu)\n", ok ? "synced" : "TIMEOUT",
                  (unsigned long)time(nullptr));
    return ok;
  }

  // ── CA trust store ────────────────────────────────────────────────────────────────────
  // Load the live CA bundle into `dst`. Precedence: NVS-refreshed bundle (from /cacerts) WINS;
  // otherwise the compiled ca_bundle.h roots. Returns false (fail-closed) if neither yields a
  // structurally valid PEM, so the caller refuses the handshake rather than going insecure.
  bool loadCaBundle(String& dst) {
    dst = "";
    Preferences p;
    p.begin("agentlamp", /*readOnly=*/true);
    String nvsCa = p.getString("cacerts", "");
    p.end();
    if (pemLooksValid(nvsCa.c_str())) {
      dst = nvsCa;
      return true;
    }
    // Compiled fallback: concatenate whichever pinned roots are populated.
    if (pemLooksValid(ROOT_CA_ISRG_X1))      { dst += ROOT_CA_ISRG_X1;      dst += "\n"; }
    if (pemLooksValid(ROOT_CA_DIGICERT_G2))  { dst += ROOT_CA_DIGICERT_G2;  dst += "\n"; }
    if (pemLooksValid(ROOT_CA_BALTIMORE))    { dst += ROOT_CA_BALTIMORE;    dst += "\n"; }
    return pemLooksValid(dst.c_str());
  }

  // Refresh the pinned bundle from GET /api/v1/device/{id}/cacerts (authenticated). Stores a
  // structurally valid PEM into NVS so subsequent boots use it (survives a CA rotation with no
  // reflash). Called opportunistically (e.g. on repeated TLS failures). Returns true on store.
  bool refreshCaBundle() {
    if (!isHttps() || !WiFi.isConnected()) return false;
    if (!ensureTimeSynced()) return false;

    String trust;
    if (!loadCaBundle(trust)) return false;   // need a trust anchor to fetch the refresh securely

    WiFiClientSecure client;
    client.setTimeout(RELAY_HTTP_TIMEOUT_MS / 1000);
    client.setCACert(trust.c_str());          // verify the relay even while refreshing its CAs

    HTTPClient http;
    char url[192];
    snprintf(url, sizeof(url), "%s/api/v1/device/%s/cacerts", _base, _id);
    if (!http.begin(client, url)) return false;
    http.setTimeout(RELAY_HTTP_TIMEOUT_MS);
    http.addHeader("Authorization", String("Bearer ") + _token);
    http.addHeader("Accept", "application/x-pem-file");
    http.addHeader("User-Agent", "agentlamp-orb/1.0");  // Cloudflare edge blocks a stock/empty UA (error 1010)

    int code = http.GET();
    if (code != 200) { http.end(); Serial.printf("relay cacerts  : http %d\n", code); return false; }
    String body = http.getString();
    http.end();

    if (!pemLooksValid(body.c_str())) {
      Serial.println(F("relay cacerts  : refused — not a valid PEM bundle"));
      return false;                            // fail-closed: never store garbage
    }
    Preferences p;
    p.begin("agentlamp", /*readOnly=*/false);
    p.putString("cacerts", body);
    p.end();
    Serial.println(F("relay cacerts  : refreshed bundle stored in NVS"));
    return true;
  }

  // ── frame fetch ───────────────────────────────────────────────────────────────────────
  // GET {base}/api/v1/device/{id}/frame over verified TLS into `out`.
  // Returns: 0 ok, >0 HTTP status, -1 transport, -2 oversize, -3 bad json,
  //          RELAY_ERR_NO_CA / RELAY_ERR_NO_TIME for the fail-closed preconditions.
  // `pollIntervalMsOut` is bumped on a 429 (honours Retry-After) just like local mode.
  int fetchFrame(Frame& out, unsigned long& pollIntervalMsOut, unsigned long pollBaseMs) {
    if (!isHttps()) return -1;                 // relay mode requires https; caller shouldn't call otherwise
    if (!WiFi.isConnected()) return -1;

    // Fail-closed preconditions (contract: never unverified TLS).
    if (!ensureTimeSynced()) return RELAY_ERR_NO_TIME;
    String trust;
    if (!loadCaBundle(trust)) {
      Serial.println(F("frame err      : relay CA bundle empty/invalid -> refusing (fail-closed)"));
      return RELAY_ERR_NO_CA;
    }

    WiFiClientSecure client;
    client.setTimeout(RELAY_HTTP_TIMEOUT_MS / 1000);
    client.setCACert(trust.c_str());           // PINNED roots — NOT setInsecure()
    // (SNI/host verification is on by default in WiFiClientSecure when a CA is set.)

    HTTPClient http;
    http.setReuse(false);
    http.setTimeout(RELAY_HTTP_TIMEOUT_MS);
    http.setConnectTimeout(RELAY_HTTP_TIMEOUT_MS);

    char url[192];
    snprintf(url, sizeof(url), "%s/api/v1/device/%s/frame", _base, _id);
    if (!http.begin(client, url)) return -1;    // TLS begin (handshake on GET); failure -> transport err

    http.addHeader("Authorization", String("Bearer ") + _token);
    http.addHeader("Accept", "application/json");
    http.addHeader("X-Frame-Schema-Version", "1");
    http.addHeader("User-Agent", "agentlamp-orb/1.0");  // Cloudflare edge blocks a stock/empty UA (error 1010)
    static const char* kCollect[] = {"Retry-After"};
    http.collectHeaders(kCollect, 1);

    int code = http.GET();
    if (code <= 0) { http.end(); return -1; }   // transport / TLS handshake failure -> fail closed

    if (code != 200) {
      if (code == 429) {
        String ra = http.header("Retry-After");
        unsigned long backoff = ra.length() ? (unsigned long)ra.toInt() * 1000UL : 0;
        pollIntervalMsOut = max(pollBaseMs * 2, max(backoff, 60000UL));
      }
      http.end();
      return code;
    }

    int declared = http.getSize();
    if (declared > (int)FRAME_MAX_BYTES) { http.end(); return -2; }

    static char buf[FRAME_MAX_BYTES + 1];
    size_t got = 0;
    WiFiClient* stream = http.getStreamPtr();
    if (!stream) { http.end(); return -1; }

    unsigned long readStart = millis();
    while (http.connected() && got <= FRAME_MAX_BYTES) {
      size_t avail = stream->available();
      if (avail) {
        size_t room = (FRAME_MAX_BYTES + 1) - got;
        size_t toRead = avail < room ? avail : room;
        int n = stream->readBytes(buf + got, toRead);
        if (n <= 0) break;
        got += (size_t)n;
        if (got > FRAME_MAX_BYTES) { http.end(); return -2; }
      } else {
        if (declared >= 0 && (int)got >= declared) break;
        if (millis() - readStart > RELAY_HTTP_TIMEOUT_MS) break;
        delay(1);
      }
    }
    http.end();

    if (got == 0 || got > FRAME_MAX_BYTES) return -2;
    if (!parseFrame(buf, got, out)) return -3;
    return 0;
  }

 private:
  char _base[96]  = {0};
  char _id[32]    = {0};
  char _token[96] = {0};
  bool _sntpStarted = false;

  static bool timeIsSane() { return (unsigned long)time(nullptr) >= NTP_MIN_EPOCH; }
};
