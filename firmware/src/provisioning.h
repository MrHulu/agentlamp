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

#ifndef AP_SSID
#define AP_SSID "AgentLamp-Setup"
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

class Provisioning {
 public:
  // Result of loading NVS creds + how to act on them.
  struct Creds {
    char ssid[64]   = {0};
    char pass[64]   = {0};
    char server[96] = {0};   // frame base url
    bool hasWifi = false;    // ssid non-empty
  };

  // ---- NVS (Preferences, namespace "agentlamp") ----------------------------

  // Load ssid/pass/server_url from NVS. server_url falls back to the compile-time
  // FRAME_BASE_URL default when NVS has none.
  static Creds loadCreds() {
    Creds c;
    Preferences p;
    p.begin("agentlamp", /*readOnly=*/true);
    p.getString("ssid", c.ssid, sizeof(c.ssid));
    p.getString("pass", c.pass, sizeof(c.pass));
    if (!p.getString("server", c.server, sizeof(c.server)) || c.server[0] == '\0') {
      strlcpy(c.server, FRAME_BASE_URL, sizeof(c.server));
    }
    p.end();
    c.hasWifi = (c.ssid[0] != '\0');
    return c;
  }

  // Persist ssid/pass/server_url to NVS. Empty server keeps the existing/default.
  static void saveCreds(const char* ssid, const char* pass, const char* server) {
    Preferences p;
    p.begin("agentlamp", /*readOnly=*/false);
    p.putString("ssid", ssid ? ssid : "");
    p.putString("pass", pass ? pass : "");
    if (server && server[0]) p.putString("server", server);
    p.end();
  }

  // Wipe WiFi creds (re-provisioning). Leaves server_url so the next portal
  // pre-fills the last-known server.
  static void clearCreds() {
    Preferences p;
    p.begin("agentlamp", /*readOnly=*/false);
    p.remove("ssid");
    p.remove("pass");
    p.end();
  }

  // ---- captive portal ------------------------------------------------------

  // Bring up SoftAP + DNS + HTTP. `serverDefault` pre-fills the Server URL field.
  void beginPortal(const char* serverDefault) {
    strlcpy(_serverDefault, (serverDefault && serverDefault[0]) ? serverDefault : FRAME_BASE_URL,
            sizeof(_serverDefault));
    _saved = false;

    WiFi.mode(WIFI_AP_STA);
    WiFi.softAP(AP_SSID, AP_PASS);
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

    Serial.print(F("portal         : AP=")); Serial.print(AP_SSID);
    Serial.print(F(" pass=")); Serial.print(AP_PASS);
    Serial.print(F(" ip=")); Serial.println(apIP);
    _running = true;
  }

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
           "<h1>AgentLamp</h1><p class=sub>Connect your orb to WiFi.</p>"
           "<form method=POST action=/save>"
           "<label>WiFi network</label>"
           "<input name=ssid placeholder='your WiFi name' autocomplete=off required>"
           "<label>WiFi password</label>"
           "<input name=pass type=password placeholder='WiFi password'>"
           "<label>Server URL</label>"
           "<input name=server value='");
    h += htmlEscape(_serverDefault);
    h += F("'>"
           "<button type=submit>Save &amp; Connect</button>"
           "</form></div></body></html>");
    _server.send(200, "text/html", h);
  }

  void handleSave() {
    String ssid   = _server.arg("ssid");
    String pass   = _server.arg("pass");
    String server = _server.arg("server");
    ssid.trim(); server.trim();

    saveCreds(ssid.c_str(), pass.c_str(), server.c_str());

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
