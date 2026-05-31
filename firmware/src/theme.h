// AgentLamp visual theme — single source of truth for the status palette.
//
// Colours are lifted directly from docs/ui/mockups/scenes.html (the :root CSS
// vars) so the device matches the design board. RGB565 for the LCD, plus the
// full-scale RGB888 used to drive the physical NeoPixel.

#pragma once

#include <Arduino.h>

// ---- RGB565 (LCD) ----------------------------------------------------------
// Helper at compile/run time: pack 8-bit components into 565.
static inline uint16_t rgb565(uint8_t r, uint8_t g, uint8_t b) {
  return (uint16_t)((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3);
}

// ---- Status enum -----------------------------------------------------------
// Authoritative set from docs/api/device_frame_api.md.
enum class Status : uint8_t {
  IDLE, THINKING, CODING, READING, TESTING,
  WAITING, DONE, ERROR, OFFLINE, STALE, UNKNOWN
};

// ---- Scene enum ------------------------------------------------------------
enum class Scene : uint8_t {
  BOOT, PAIRING, FLEET, FOCUS, QUOTA, ALERT,
  OFFLINE, STALE, DIAGNOSTICS, SLEEP, WIFICONFIG, UNKNOWN
};

// ---- Accent enum (frame `accent` field) ------------------------------------
enum class Accent : uint8_t {
  BLUE, CYAN, PURPLE, YELLOW, GREEN, RED, WHITE, MUTED
};

// ---- Palette (matches scenes.html :root) -----------------------------------
// Full-scale RGB888 components, mirrored to 565 for the panel.
struct Rgb { uint8_t r, g, b; };

// status palette
static constexpr Rgb C_IDLE   = {0x4d, 0x7c, 0xff};  // --idle  blue
static constexpr Rgb C_THINK  = {0x96, 0x5a, 0xff};  // --think blue-violet (more red so it isn't read as plain blue)
static constexpr Rgb C_CODE   = {0xc8, 0x55, 0xf5};  // --code  purple/magenta (was 0xa06bff = too blue, read as blue)
static constexpr Rgb C_READ   = {0x22, 0xd3, 0xee};  // --read  cyan
static constexpr Rgb C_TEST   = {0x2d, 0xd4, 0xa7};  // --test  teal
static constexpr Rgb C_WAIT   = {0xff, 0xb0, 0x20};  // --wait  amber
static constexpr Rgb C_DONE   = {0x34, 0xd3, 0x99};  // --done  green
static constexpr Rgb C_ERR    = {0xff, 0x54, 0x70};  // --err   red
static constexpr Rgb C_OFF    = {0x7b, 0x86, 0x96};  // --off   grey
static constexpr Rgb C_STALE  = {0xd6, 0xda, 0xe0};  // --stale near-white

// accents that aren't 1:1 with a status word
static constexpr Rgb C_WHITE  = {0xee, 0xf1, 0xf6};  // --ink
static constexpr Rgb C_MUTED  = {0x5b, 0x64, 0x73};  // --ink-faint

// ink (text) shades
static constexpr Rgb C_INK      = {0xee, 0xf1, 0xf6};  // --ink
static constexpr Rgb C_INK_DIM  = {0x9a, 0xa3, 0xb2};  // --ink-dim
static constexpr Rgb C_INK_FAINT= {0x5b, 0x64, 0x73};  // --ink-faint
static constexpr Rgb C_SCREEN   = {0x08, 0x0a, 0x10};  // --screen near-black

static inline uint16_t to565(const Rgb& c) { return rgb565(c.r, c.g, c.b); }

// ---- status -> accent colour ----------------------------------------------
static inline Rgb statusColor(Status s) {
  switch (s) {
    case Status::IDLE:    return C_IDLE;
    case Status::THINKING:return C_THINK;
    case Status::CODING:  return C_CODE;
    case Status::READING: return C_READ;
    case Status::TESTING: return C_TEST;
    case Status::WAITING: return C_WAIT;
    case Status::DONE:    return C_DONE;
    case Status::ERROR:   return C_ERR;
    case Status::OFFLINE: return C_OFF;
    case Status::STALE:   return C_STALE;
    case Status::UNKNOWN: default: return C_INK_FAINT;  // muted, like IDLE-dim
  }
}

// ---- frame `accent` string -> colour --------------------------------------
static inline Rgb accentColor(Accent a) {
  switch (a) {
    case Accent::BLUE:   return C_IDLE;
    case Accent::CYAN:   return C_READ;
    case Accent::PURPLE: return C_CODE;
    case Accent::YELLOW: return C_WAIT;
    case Accent::GREEN:  return C_DONE;
    case Accent::RED:    return C_ERR;
    case Accent::WHITE:  return C_WHITE;
    case Accent::MUTED:  default: return C_MUTED;
  }
}

// ---- vivid LED palette -----------------------------------------------------
// The LCD palette above is intentionally soft (designed for a dark anti-aliased
// screen). A bare WS2812 point source needs near-pure channels or those soft
// colours read as washed-out/pale. These saturated values drive the LED ONLY.
static inline Rgb ledStatusColor(Status s) {
  switch (s) {
    case Status::IDLE:    return {0,   40, 200};
    case Status::THINKING:return {120,  0, 255};
    case Status::CODING:  return {185,  0, 255};
    case Status::READING: return {0,  170, 255};
    case Status::TESTING: return {0,  255, 110};
    case Status::WAITING: return {255, 150,  0};
    case Status::DONE:    return {0,  255,  70};
    case Status::ERROR:   return {255,   0,  0};
    case Status::OFFLINE: return {60,  60,  72};
    case Status::STALE:   return {170, 170, 170};
    default:              return {50,  50,  70};
  }
}
static inline Rgb ledAccentColor(Accent a) {
  switch (a) {
    case Accent::BLUE:   return {0,   40, 200};
    case Accent::CYAN:   return {0,  170, 255};
    case Accent::PURPLE: return {185,  0, 255};
    case Accent::YELLOW: return {255, 150,  0};
    case Accent::GREEN:  return {0,  255,  70};
    case Accent::RED:    return {255,   0,  0};
    case Accent::WHITE:  return {200, 200, 200};
    default:             return {50,  50,  70};
  }
}

// ---- string parsers (wire -> enum) ----------------------------------------
static inline Status parseStatus(const char* s) {
  if (!s) return Status::UNKNOWN;
  if (!strcmp(s, "IDLE"))     return Status::IDLE;
  if (!strcmp(s, "THINKING")) return Status::THINKING;
  if (!strcmp(s, "CODING"))   return Status::CODING;
  if (!strcmp(s, "READING"))  return Status::READING;
  if (!strcmp(s, "TESTING"))  return Status::TESTING;
  if (!strcmp(s, "WAITING"))  return Status::WAITING;
  if (!strcmp(s, "DONE"))     return Status::DONE;
  if (!strcmp(s, "ERROR"))    return Status::ERROR;
  if (!strcmp(s, "OFFLINE"))  return Status::OFFLINE;
  if (!strcmp(s, "STALE"))    return Status::STALE;
  return Status::UNKNOWN;
}

static inline Scene parseScene(const char* s) {
  if (!s) return Scene::UNKNOWN;
  if (!strcmp(s, "boot"))        return Scene::BOOT;
  if (!strcmp(s, "pairing"))     return Scene::PAIRING;
  if (!strcmp(s, "fleet"))       return Scene::FLEET;
  if (!strcmp(s, "focus"))       return Scene::FOCUS;
  if (!strcmp(s, "quota"))       return Scene::QUOTA;
  if (!strcmp(s, "alert"))       return Scene::ALERT;
  if (!strcmp(s, "offline"))     return Scene::OFFLINE;
  if (!strcmp(s, "stale"))       return Scene::STALE;
  if (!strcmp(s, "diagnostics")) return Scene::DIAGNOSTICS;
  if (!strcmp(s, "sleep"))       return Scene::SLEEP;
  return Scene::UNKNOWN;
}

static inline Accent parseAccent(const char* s) {
  if (!s) return Accent::MUTED;
  if (!strcmp(s, "blue"))   return Accent::BLUE;
  if (!strcmp(s, "cyan"))   return Accent::CYAN;
  if (!strcmp(s, "purple")) return Accent::PURPLE;
  if (!strcmp(s, "yellow")) return Accent::YELLOW;
  if (!strcmp(s, "green"))  return Accent::GREEN;
  if (!strcmp(s, "red"))    return Accent::RED;
  if (!strcmp(s, "white"))  return Accent::WHITE;
  if (!strcmp(s, "muted"))  return Accent::MUTED;
  return Accent::MUTED;
}

static inline const char* statusWord(Status s) {
  switch (s) {
    case Status::IDLE:    return "IDLE";
    case Status::THINKING:return "THINKING";
    case Status::CODING:  return "CODING";
    case Status::READING: return "READING";
    case Status::TESTING: return "TESTING";
    case Status::WAITING: return "WAITING";
    case Status::DONE:    return "DONE";
    case Status::ERROR:   return "ERROR";
    case Status::OFFLINE: return "OFFLINE";
    case Status::STALE:   return "STALE";
    default:              return "UNKNOWN";
  }
}
