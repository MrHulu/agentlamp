// AgentLamp scene renderer.
//
// Draws the device scenes from docs/ui/mockups/scenes.html onto the 172x320
// ST7789. Design language: near-black base (#080a10), one dominant status word
// in the accent colour, a small top bar (provider·account + clock), and a bottom
// meta line. We draw straight to the panel (no PSRAM sprite) — low text density.
//
// TYPOGRAPHY RULE (Boss 2026-05-31): every text line is rendered through drawFit(),
// which measures the string at decreasing font sizes and picks the LARGEST that
// fits the usable width. This guarantees NO horizontal clipping for any string
// (long words like "THINKING" or "AgentLamp-Setup" auto-shrink) while keeping
// text as large as possible. Earlier the fonts were hard-coded and overflowed.

#pragma once

#include "display.h"
#include "frame.h"
#include "theme.h"

#ifndef LCD_WIDTH
#define LCD_WIDTH 172
#endif
#ifndef LCD_HEIGHT
#define LCD_HEIGHT 320
#endif

// usable text width: 8 px side margins
#define FIT_W (LCD_WIDTH - 16)

class Renderer {
  AgentLampDisplay& _d;

  // ----- font ladders (largest -> smallest); drawFit picks the largest that fits
  static const lgfx::IFont* big(int i) {
    static const lgfx::IFont* L[] = {&fonts::FreeSansBold24pt7b, &fonts::FreeSansBold18pt7b,
                                     &fonts::FreeSansBold12pt7b, &fonts::FreeSans9pt7b};
    return (i < 4) ? L[i] : nullptr;
  }
  static const lgfx::IFont* mid(int i) {
    static const lgfx::IFont* L[] = {&fonts::FreeSansBold12pt7b, &fonts::FreeSans9pt7b,
                                     &fonts::Font4, &fonts::Font2};
    return (i < 4) ? L[i] : nullptr;
  }
  static const lgfx::IFont* sm(int i) {
    static const lgfx::IFont* L[] = {&fonts::FreeSans9pt7b, &fonts::Font4, &fonts::Font2};
    return (i < 3) ? L[i] : nullptr;
  }

  // Draw `text` with datum `datum` at (x,y), choosing the largest font from the
  // ladder whose measured width fits `maxW`. Never clips horizontally.
  void drawFit(const char* text, int x, int y, int maxW, const Rgb& color,
               const lgfx::IFont* (*ladder)(int), textdatum_t datum = textdatum_t::middle_center) {
    if (!text || !text[0]) return;
    _d.setTextColor(to565(color), to565(C_SCREEN));
    _d.setTextDatum(datum);
    const lgfx::IFont* chosen = nullptr;
    for (int i = 0; ladder(i); i++) {
      _d.setFont(ladder(i));
      chosen = ladder(i);
      if (_d.textWidth(text) <= maxW) break;   // largest that fits
    }
    if (chosen) _d.setFont(chosen);
    _d.drawString(text, x, y);
    _d.setTextDatum(textdatum_t::top_left);
  }

  void bg() { _d.fillScreen(to565(C_SCREEN)); }

  // top bar: left "● who", right clock (both fit-bounded).
  void topBar(const char* who, const Rgb& accent, const char* clock) {
    _d.fillCircle(13, 23, 4, to565(accent));
    drawFit(who, 24, 23, 96, C_INK_DIM, sm, textdatum_t::middle_left);
    if (clock && clock[0])
      drawFit(clock, LCD_WIDTH - 12, 23, 56, C_INK_DIM, sm, textdatum_t::middle_right);
  }

  // bottom meta line (faint), centred + fit-bounded.
  void bottom(const char* s, const Rgb& c) {
    drawFit(s, LCD_WIDTH / 2, LCD_HEIGHT - 16, FIT_W, c, sm, textdatum_t::bottom_center);
  }

  // dominant status word, centred at y — biggest font that fits FIT_W.
  void statusWordBig(const char* word, const Rgb& accent, int y) {
    drawFit(word, LCD_WIDTH / 2, y, FIT_W, accent, big);
  }

 public:
  explicit Renderer(AgentLampDisplay& d) : _d(d) {}

  static void uptimeClock(char* buf, size_t cap, unsigned long ms) {
    unsigned long s = ms / 1000UL;
    unsigned long m = (s / 60UL) % 100UL;
    snprintf(buf, cap, "%02lu:%02lu", m, s % 60UL);
  }

  // ===================== SCENES =====================

  void boot(const char* version) {
    bg();
    _d.fillCircle(LCD_WIDTH / 2, 120, 18, to565(C_SCREEN));
    _d.drawCircle(LCD_WIDTH / 2, 120, 18, to565(C_STALE));
    _d.fillCircle(LCD_WIDTH / 2 + 5, 120, 14, to565(C_STALE));
    drawFit("AgentLamp", LCD_WIDTH / 2, 174, FIT_W, C_INK, big);
    char line[28];
    snprintf(line, sizeof(line), "starting  %s", version ? version : "v0.1");
    drawFit(line, LCD_WIDTH / 2, 210, FIT_W, C_INK_DIM, sm);
    bottom("local mode", C_INK_DIM);
  }

  // WiFiConfig / provisioning portal. Short, big, two clear steps. Any long AP
  // name / address auto-shrinks via drawFit, so nothing clips.
  void wifiConfig(const char* title, const char* code, const char* helper,
                  const char* footer) {
    bg();
    topBar("setup", C_READ, "");
    drawFit(title ? title : "connect wi-fi", LCD_WIDTH / 2, 92, FIT_W, C_INK_DIM, sm);
    drawFit(code ? code : "SETUP", LCD_WIDTH / 2, 132, FIT_W, C_READ, big);
    // step 1: join AP
    drawFit("1  join wi-fi", LCD_WIDTH / 2, 182, FIT_W, C_INK_DIM, sm);
    drawFit(helper && helper[0] ? helper : "AgentLamp-Setup", LCD_WIDTH / 2, 206, FIT_W, C_INK, mid);
    // step 2: open portal
    drawFit("2  open", LCD_WIDTH / 2, 246, FIT_W, C_INK_DIM, sm);
    drawFit(footer && footer[0] ? footer : "192.168.4.1", LCD_WIDTH / 2, 270, FIT_W, C_READ, mid);
    bottom("then enter wi-fi", C_INK_FAINT);
  }

  // Live / Focus: kicker (provider·account), dominant status word, project + task.
  void focus(const Frame& f, const Rgb& accent, const char* clock) {
    bg();
    topBar("focus", accent, clock);
    char kicker[40];
    if (f.account[0]) snprintf(kicker, sizeof(kicker), "%s  %s", f.provider, f.account);
    else              snprintf(kicker, sizeof(kicker), "%s", f.provider);
    drawFit(kicker, LCD_WIDTH / 2, 118, FIT_W, C_INK_DIM, sm);
    statusWordBig(statusWord(f.status), accent, 164);
    if (f.project[0]) drawFit(f.project, LCD_WIDTH / 2, 214, FIT_W, C_INK, mid);
    if (f.task[0])    drawFit(f.task, LCD_WIDTH / 2, 244, FIT_W, C_INK_DIM, sm);
    char meta[40];
    snprintf(meta, sizeof(meta), "seq %lu", f.seq);
    bottom(meta, C_INK_FAINT);
  }

  // Fleet: up to ~5 rows "provider ......... status", + summary.
  void fleet(const Frame& f, const Rgb& accent, const char* clock) {
    bg();
    topBar("agents", accent, clock);
    int y = 80;
    uint8_t n = f.fleetCount < 5 ? f.fleetCount : 5;
    for (uint8_t i = 0; i < n; i++) {
      const FleetRow& r = f.fleet[i];
      Rgb rc = statusColor(r.status);
      // provider on the left (bounded to left 60% so it can't collide with the tag)
      drawFit(r.provider, 14, y, 96, C_INK, mid, textdatum_t::middle_left);
      char st[10];
      const char* w = statusWord(r.status);
      snprintf(st, sizeof(st), "%.4s", w);
      for (char* p = st; *p; ++p) *p = tolower(*p);
      drawFit(st, LCD_WIDTH - 14, y, 56, rc, mid, textdatum_t::middle_right);
      y += 40;
    }
    char summary[40];
    int active = 0;
    for (uint8_t i = 0; i < f.fleetCount; i++)
      if (f.fleet[i].status != Status::IDLE && f.fleet[i].status != Status::OFFLINE) active++;
    if (f.fleetMore > 0) snprintf(summary, sizeof(summary), "%d active  +%d more", active, f.fleetMore);
    else                 snprintf(summary, sizeof(summary), "%d active", active);
    bottom(summary, C_INK_DIM);
  }

  // Quota: up to 2 horizontal bars.
  void quota(const Frame& f, const Rgb& accent, const char* clock) {
    bg();
    topBar("quota", accent, clock);
    int y = 96;
    for (uint8_t i = 0; i < f.quotaCount; i++) {
      const auto& q = f.quota[i];
      float frac = q.w5 >= 0 ? q.w5 : (q.week >= 0 ? q.week : 0);
      const char* win = q.w5 >= 0 ? "5h" : "week";
      Rgb c = frac >= 0.7f ? C_ERR : (frac >= 0.4f ? C_WAIT : C_TEST);
      char lbl[28];
      snprintf(lbl, sizeof(lbl), "%s %s", q.provider, q.account);
      drawFit(lbl, 16, y, 110, C_INK, mid, textdatum_t::middle_left);
      drawFit(win, LCD_WIDTH - 16, y, 40, C_INK_DIM, sm, textdatum_t::middle_right);
      int bx = 16, bw = LCD_WIDTH - 32, by = y + 22, bh = 10;
      _d.fillRoundRect(bx, by, bw, bh, 5, rgb565(28, 28, 32));
      int fw = (int)(bw * (frac > 1 ? 1 : frac));
      if (fw > 0) _d.fillRoundRect(bx, by, fw, bh, 5, to565(c));
      char pct[8];
      snprintf(pct, sizeof(pct), "%d%%", (int)(frac * 100 + 0.5f));
      drawFit(pct, bx + 2, by + 26, 80, c, sm, textdatum_t::middle_left);
      if (q.estimated) drawFit("est", LCD_WIDTH - 16, by + 26, 40, C_INK_DIM, sm, textdatum_t::middle_right);
      y += 100;
    }
    bottom("top 2 risk", C_INK_DIM);
  }

  // Alert: big coloured ring + glyph, status word, meta.
  void alert(const Frame& f, const Rgb& accent, const char* clock) {
    bg();
    topBar("alert", accent, clock);
    int cx = LCD_WIDTH / 2, cy = 118, rr = 44;
    for (int t = 0; t < 3; t++) _d.drawCircle(cx, cy, rr - t, to565(accent));
    const char* glyph = (f.status == Status::ERROR) ? "x" : "!";
    _d.setFont(&fonts::FreeSansBold24pt7b);
    _d.setTextColor(to565(accent), to565(C_SCREEN));
    _d.setTextDatum(textdatum_t::middle_center);
    _d.drawString(glyph, cx, cy);
    _d.setTextDatum(textdatum_t::top_left);
    // dominant word: WAITING/ERROR use the status word; a quota-danger alert
    // (status IDLE + red) would otherwise read "IDLE" in a red ring — show "QUOTA".
    bool statusAlert = (f.status == Status::WAITING || f.status == Status::ERROR);
    statusWordBig(statusAlert ? statusWord(f.status) : "QUOTA", accent, 194);
    char meta[40];
    if (f.account[0]) snprintf(meta, sizeof(meta), "%s  %s", f.provider, f.account);
    else              snprintf(meta, sizeof(meta), "%s", f.provider);
    drawFit(meta, cx, 238, FIT_W, C_INK, mid);
    if (f.task[0]) drawFit(f.task, cx, 266, FIT_W, C_INK_DIM, sm);
    bottom(f.headline[0] ? f.headline : "", C_INK_FAINT);
  }

  void offline(unsigned long lastSeenMs, const char* clock) {
    bg();
    topBar("offline", C_OFF, clock);
    statusWordBig("OFFLINE", C_OFF, 128);
    drawFit("frame source", LCD_WIDTH / 2, 182, FIT_W, C_INK_DIM, sm);
    drawFit("unreachable", LCD_WIDTH / 2, 208, FIT_W, C_INK_DIM, sm);
    char foot[28] = "";
    if (lastSeenMs) {
      unsigned long ago = lastSeenMs / 1000UL;
      if (ago < 120) snprintf(foot, sizeof(foot), "last seen %lus ago", ago);
      else           snprintf(foot, sizeof(foot), "last seen %lum ago", ago / 60);
    }
    bottom(foot, C_INK_FAINT);
  }

  void stale(const Frame& f, unsigned long ageMs, const char* clock) {
    bg();
    topBar("stale", C_STALE, clock);
    char kicker[40];
    if (f.account[0]) snprintf(kicker, sizeof(kicker), "%s  %s", f.provider, f.account);
    else              snprintf(kicker, sizeof(kicker), "%s", f.provider);
    drawFit(kicker, LCD_WIDTH / 2, 118, FIT_W, C_INK_DIM, sm);
    statusWordBig(statusWord(f.status), C_STALE, 164);
    drawFit("showing cached", LCD_WIDTH / 2, 212, FIT_W, C_INK_DIM, sm);
    char foot[28];
    unsigned long ago = ageMs / 1000UL;
    if (ago < 120) snprintf(foot, sizeof(foot), "updated %lus ago", ago);
    else           snprintf(foot, sizeof(foot), "updated %lum ago", ago / 60);
    bottom(foot, C_STALE);
  }

  void message(const char* title, const Rgb& accent, const char* l1,
               const char* l2, const char* footer) {
    bg();
    topBar("diag", accent, "");
    statusWordBig(title, accent, 122);
    drawFit(l1, LCD_WIDTH / 2, 182, FIT_W, C_INK_DIM, sm);
    drawFit(l2, LCD_WIDTH / 2, 208, FIT_W, C_INK_DIM, sm);
    bottom(footer, C_INK_DIM);
  }
};
