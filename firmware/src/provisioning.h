// AgentLamp WiFi provisioning — runtime SoftAP captive portal.
//
// Open-source safety invariant: this repo contains ZERO WiFi credentials. The
// SSID/password live ONLY in NVS (Preferences, namespace "agentlamp"), written
// by the user through a temporary captive-portal form on first boot.
//
// Flow:
//   boot -> Provisioning::loadCreds() reads ssid/pass/server_url from NVS.
//   no creds / join fail -> Provisioning::beginPortal():
//       WiFi.mode(WIFI_AP_STA); WiFi.softAP("AgentLamp-Setup", AP_PASS)
//       DNSServer on :53 wildcards every host -> 192.168.4.1 (captive portal)
//       WebServer on :80 serves GET / (form) and POST /save (writes NVS)
//   user submits -> creds stored in NVS -> device leaves AP, joins the WiFi.
//
// Re-provisioning: hold BOOT (GPIO0) LOW ~3s -> clearCreds() + ESP.restart().
//
// Deps are all Arduino-ESP32 core built-ins (WebServer / DNSServer / Preferences
// / WiFi) — NO async lib, NO new platformio dependency.

#pragma once

#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <Preferences.h>

#include "config.h"   // FRAME_BASE_URL default

// AP SSID is "AgentLamp-Setup-<suffix>" where <suffix> is the last 4 hex of this chip's
// factory MAC (efuse). The per-device suffix disambiguates multiple orbs setting up nearby
// (the runbook tells the user to look for "AgentLamp-Setup-<device_id_suffix>"). AP_SSID below
// is the fixed PREFIX only; the live SSID is built at runtime by Provisioning::apSsid().
#ifndef AP_SSID_PREFIX
#define AP_SSID_PREFIX "AgentLamp-Setup"
#endif
// Back-compat alias: older call sites used AP_SSID as the literal prefix. Kept so any
// "join AP_SSID" hint still compiles; the live AP name adds the per-device suffix.
#ifndef AP_SSID
#define AP_SSID AP_SSID_PREFIX
#endif
// Fixed AP password keeps the setup AP off open-network scanners. Documented in
// docs/devlog/04-provisioning-impl.md so the Boss knows what to type if the phone
// asks (>=8 chars, WPA2 minimum).
#ifndef AP_PASS
#define AP_PASS "agentlamp"
#endif
#ifndef AP_PORTAL_IP
#define AP_PORTAL_IP "192.168.4.1"
#endif

// Multi-network WiFi store: the orb remembers several SSIDs and auto-joins whichever known
// network is present (WiFiMulti picks the strongest). The captive portal ADDS a new network
// (it never clobbers the others), so moving the lamp to a new place is a 1-field change and
// the backend URL — fixed in relay mode — is untouched. Bounded so NVS use is predictable.
#ifndef WIFI_MAX_NETS
#define WIFI_MAX_NETS 5
#endif

class Provisioning {
 public:
  // The live captive-portal AP name: "AgentLamp-Setup-<suffix>", where <suffix> is the last
  // 4 hex digits of this chip's factory MAC (efuse) — stable per device, unique enough to tell
  // two orbs apart during setup. Matches the runbook's "AgentLamp-Setup-<device_id_suffix>".
  // Returned by value (short String); cache it if you need a stable c_str() for the AP call.
  static String apSsid() {
    uint64_t mac = ESP.getEfuseMac();          // 48-bit factory MAC, stable per chip
    uint16_t suffix = (uint16_t)(mac & 0xFFFF); // last 16 bits -> 4 hex digits
    char buf[8];
    snprintf(buf, sizeof(buf), "%04X", suffix);
    return String(AP_SSID_PREFIX "-") + buf;
  }

  // One stored WiFi network.
  struct WifiNet {
    char ssid[64] = {0};
    char pass[64] = {0};
  };

  // Result of loading NVS creds + how to act on them.
  //
  // `ssid`/`pass` mirror nets[0] (the most-recently-added network) for backward compatibility
  // with the existing single-net code paths (wifiConnect()). `nets[]` is the full multi-network
  // list fed to WiFiMulti. `token` is the relay device bearer token (relay mode; NVS only — I3).
  struct Creds {
    char ssid[64]   = {0};
    char pass[64]   = {0};
    char server[96] = {0};   // frame base url
    char token[96]  = {0};   // relay device token (empty in local mode)
    WifiNet nets[WIFI_MAX_NETS];
    uint8_t netCount = 0;
    bool hasWifi = false;    // at least one stored network
  };

  // ---- NVS (Preferences, namespace "agentlamp") ----------------------------
  //
  // Layout: legacy single keys "ssid"/"pass" (kept for migration) + multi-net keys
  // "ssid0".."ssidN" / "pass0".."passN" + "netn" (count) + "server" + "token".

  // Load the full creds (multi-network + server + token) from NVS. server_url falls back to the
  // compile-time FRAME_BASE_URL default when NVS has none (empty in a relay build).
  static Creds loadCreds() {
    Creds c;
    Preferences p;
    p.begin("agentlamp", /*readOnly=*/true);

    if (!p.getString("server", c.server, sizeof(c.server)) || c.server[0] == '\0') {
      strlcpy(c.server, FRAME_BASE_URL, sizeof(c.server));
    }
    p.getString("token", c.token, sizeof(c.token));

    // Multi-network: read up to WIFI_MAX_NETS stored networks.
    uint8_t stored = (uint8_t)p.getUChar("netn", 0);
    if (stored > WIFI_MAX_NETS) stored = WIFI_MAX_NETS;
    for (uint8_t i = 0; i < stored; i++) {
      char k[8];
      snprintf(k, sizeof(k), "ssid%u", (unsigned)i);
      char s[64] = {0};
      p.getString(k, s, sizeof(s));
      if (s[0] == '\0') continue;
      snprintf(k, sizeof(k), "pass%u", (unsigned)i);
      strlcpy(c.nets[c.netCount].ssid, s, sizeof(c.nets[c.netCount].ssid));
      p.getString(k, c.nets[c.netCount].pass, sizeof(c.nets[c.netCount].pass));
      c.netCount++;
    }

    // Migration: a device provisioned by the OLD single-net firmware has only "ssid"/"pass".
    // Fold it in as net[0] if the multi-net list is empty so we don't lose the saved network.
    if (c.netCount == 0) {
      char s[64] = {0};
      p.getString("ssid", s, sizeof(s));
      if (s[0] != '\0') {
        strlcpy(c.nets[0].ssid, s, sizeof(c.nets[0].ssid));
        p.getString("pass", c.nets[0].pass, sizeof(c.nets[0].pass));
        c.netCount = 1;
      }
    }
    p.end();

    if (c.netCount > 0) {
      strlcpy(c.ssid, c.nets[0].ssid, sizeof(c.ssid));
      strlcpy(c.pass, c.nets[0].pass, sizeof(c.pass));
    }
    c.hasWifi = (c.netCount > 0);
    return c;
  }

  // ADD a network to the multi-net store (newest goes to slot 0; older shift down; the list is
  // capped at WIFI_MAX_NETS and a duplicate SSID is updated in place, not duplicated). Persists
  // server + token too. Empty server keeps the existing/default; empty token leaves token as-is.
  static void saveCreds(const char* ssid, const char* pass, const char* server,
                        const char* token = nullptr) {
    // Load existing list, prepend/replace, rewrite.
    Creds cur = loadCreds();
    WifiNet out[WIFI_MAX_NETS];
    uint8_t n = 0;
    if (ssid && ssid[0]) {
      strlcpy(out[0].ssid, ssid, sizeof(out[0].ssid));
      strlcpy(out[0].pass, pass ? pass : "", sizeof(out[0].pass));
      n = 1;
    }
    for (uint8_t i = 0; i < cur.netCount && n < WIFI_MAX_NETS; i++) {
      if (ssid && ssid[0] && strcmp(cur.nets[i].ssid, ssid) == 0) continue;  // dedupe: just refreshed it
      out[n++] = cur.nets[i];
    }

    Preferences p;
    p.begin("agentlamp", /*readOnly=*/false);
    for (uint8_t i = 0; i < n; i++) {
      char k[8];
      snprintf(k, sizeof(k), "ssid%u", (unsigned)i);
      p.putString(k, out[i].ssid);
      snprintf(k, sizeof(k), "pass%u", (unsigned)i);
      p.putString(k, out[i].pass);
    }
    p.putUChar("netn", n);
    // keep legacy keys in sync with slot 0 so an older code path still sees the latest net
    if (n > 0) { p.putString("ssid", out[0].ssid); p.putString("pass", out[0].pass); }
    if (server && server[0]) p.putString("server", server);
    if (token && token[0])   p.putString("token", token);
    p.end();
  }

  // Wipe ALL WiFi networks (re-provisioning). Leaves server_url + token so the next portal
  // pre-fills the last-known relay and the device stays paired to the relay.
  static void clearCreds() {
    Preferences p;
    p.begin("agentlamp", /*readOnly=*/false);
    p.remove("ssid");
    p.remove("pass");
    for (uint8_t i = 0; i < WIFI_MAX_NETS; i++) {
      char k[8];
      snprintf(k, sizeof(k), "ssid%u", (unsigned)i); p.remove(k);
      snprintf(k, sizeof(k), "pass%u", (unsigned)i); p.remove(k);
    }
    p.remove("netn");
    p.end();
  }

  // ---- captive portal ------------------------------------------------------

  // Bring up SoftAP + DNS + HTTP. `serverDefault` pre-fills the Server URL field.
  void beginPortal(const char* serverDefault) {
    strlcpy(_serverDefault, (serverDefault && serverDefault[0]) ? serverDefault : FRAME_BASE_URL,
            sizeof(_serverDefault));
    _saved = false;

    // Build the per-device AP name once and keep a stable copy (softAP needs a live c_str()).
    strlcpy(_apSsid, apSsid().c_str(), sizeof(_apSsid));

    WiFi.mode(WIFI_AP_STA);
    WiFi.softAP(_apSsid, AP_PASS);
    delay(200);                                   // let the AP settle
    IPAddress apIP = WiFi.softAPIP();             // 192.168.4.1

    // DNS: answer every query with the AP IP so any hostname pops the portal.
    _dns.setErrorReplyCode(DNSReplyCode::NoError);
    _dns.start(53, "*", apIP);

    _server.on("/", HTTP_GET, [this]() { handleRoot(); });
    _server.on("/save", HTTP_POST, [this]() { handleSave(); });
    _server.on("/generate_204", HTTP_GET, [this]() { handleRoot(); });  // Android
    _server.on("/hotspot-detect.html", HTTP_GET, [this]() { handleRoot(); });  // iOS/macOS
    _server.onNotFound([this]() { handleRoot(); });  // catch-all -> portal
    _server.begin();

    Serial.print(F("portal         : AP=")); Serial.print(_apSsid);
    Serial.print(F(" pass=")); Serial.print(AP_PASS);
    Serial.print(F(" ip=")); Serial.println(apIP);
    _running = true;
  }

  // Live AP name ("AgentLamp-Setup-<suffix>") after beginPortal(); empty before. Lets the
  // renderer show the user the exact SSID to join (matches the runbook).
  const char* activeApSsid() const { return _apSsid; }

  // Service DNS + HTTP. Call every loop while provisioning. Returns true once
  // the user has POSTed creds (so the caller can tear down + try to join).
  bool service() {
    if (!_running) return false;
    _dns.processNextRequest();
    _server.handleClient();
    return _saved;
  }

  // Tear down the portal (AP stays for STA join; DNS/HTTP stop).
  void endPortal() {
    if (!_running) return;
    _server.stop();
    _dns.stop();
    _running = false;
  }

  bool running() const { return _running; }

 private:
  WebServer  _server{80};
  DNSServer  _dns;
  bool       _running = false;
  bool       _saved   = false;
  char       _serverDefault[96] = {0};
  char       _apSsid[40] = {0};       // live "AgentLamp-Setup-<suffix>" name for this device

  void handleRoot() {
    // Mobile-friendly, self-contained (no external assets — the phone has no
    // internet while on this AP). Big tap targets, dark theme matching the orb.
    String h;
    h.reserve(2200);
    h += F("<!doctype html><html><head><meta charset=utf-8>"
           "<meta name=viewport content='width=device-width,initial-scale=1'>"
           "<title>AgentLamp Setup</title><style>"
           "*{box-sizing:border-box}"
           "body{margin:0;background:#080a10;color:#eef1f6;"
           "font-family:-apple-system,system-ui,Segoe UI,Roboto,sans-serif;"
           "padding:28px 20px;font-size:18px;line-height:1.5}"
           ".card{max-width:440px;margin:0 auto}"
           "h1{font-size:30px;margin:0 0 4px;color:#22d3ee}"
           "p.sub{color:#9aa3b2;margin:0 0 24px;font-size:16px}"
           "label{display:block;margin:18px 0 6px;color:#9aa3b2;font-size:15px;"
           "text-transform:uppercase;letter-spacing:.06em}"
           "input{width:100%;padding:14px 14px;font-size:18px;border-radius:12px;"
           "border:1px solid #2a2f3a;background:#11141c;color:#eef1f6}"
           "input:focus{outline:none;border-color:#22d3ee}"
           "button{width:100%;margin-top:28px;padding:16px;font-size:19px;"
           "font-weight:600;border:0;border-radius:14px;background:#22d3ee;"
           "color:#001016}"
           "</style></head><body><div class=card>"
           "<h1>AgentLamp</h1><p class=sub>Add a WiFi network. The orb remembers several and "
           "joins whichever is in range — the backend URL stays the same.</p>"
           "<form method=POST action=/save>"
           "<label>WiFi network</label>"
           "<input name=ssid placeholder='your WiFi name' autocomplete=off required>"
           "<label>WiFi password</label>"
           "<input name=pass type=password placeholder='WiFi password'>"
           "<label>Server URL</label>"
           "<input name=server value='");
    h += htmlEscape(_serverDefault);
    h += F("'>"
           "<label>Device token <span style='text-transform:none;color:#5b6473'>"
           "(relay mode — leave blank for local)</span></label>"
           "<input name=token type=password placeholder='relay device token (see switch-fast runbook)' "
           "autocomplete=off>"
           "<button type=submit>Save &amp; Connect</button>"
           "</form></div></body></html>");
    _server.send(200, "text/html", h);
  }

  void handleSave() {
    String ssid   = _server.arg("ssid");
    String pass   = _server.arg("pass");
    String server = _server.arg("server");
    String token  = _server.arg("token");
    ssid.trim(); server.trim(); token.trim();

    // ADD this network to the multi-net store (does not clobber the others) + persist
    // server/token. A blank token leaves any existing relay token untouched.
    saveCreds(ssid.c_str(), pass.c_str(), server.c_str(),
              token.length() ? token.c_str() : nullptr);

    String h;
    h.reserve(900);
    h += F("<!doctype html><html><head><meta charset=utf-8>"
           "<meta name=viewport content='width=device-width,initial-scale=1'>"
           "<title>Saved</title><style>"
           "body{margin:0;background:#080a10;color:#eef1f6;"
           "font-family:-apple-system,system-ui,sans-serif;"
           "padding:60px 24px;text-align:center}"
           "h1{color:#34d399;font-size:30px}p{color:#9aa3b2;font-size:18px}"
           "</style></head><body>"
           "<h1>Saved &#10003;</h1>"
           "<p>AgentLamp is connecting to <b>");
    h += htmlEscape(ssid.c_str());
    h += F("</b>.<br>You can close this page.</p>"
           "</body></html>");
    _server.send(200, "text/html", h);

    Serial.print(F("portal         : creds saved for ssid=")); Serial.println(ssid);
    _saved = true;   // caller tears down + attempts join
  }

  // minimal HTML-attribute escape (value goes inside single-quoted attrs)
  static String htmlEscape(const char* s) {
    String o;
    if (!s) return o;
    for (const char* p = s; *p; ++p) {
      switch (*p) {
        case '&':  o += F("&amp;");  break;
        case '<':  o += F("&lt;");   break;
        case '>':  o += F("&gt;");   break;
        case '\'': o += F("&#39;");  break;
        case '"':  o += F("&quot;"); break;
        default:   o += *p;          break;
      }
    }
    return o;
  }
};
