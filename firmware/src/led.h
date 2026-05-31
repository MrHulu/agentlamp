// AgentLamp onboard RGB LED — single WS2812 (NeoPixel) on GPIO38.
//
// Drives the orb's physical glow to match the dominant status accent colour, so
// the lamp reads at a glance across the room even before you look at the screen.
// Brightness is capped (firmware_contract.md: 20-35%) so a desk object isn't a
// distracting flashlight.

#pragma once

#include <Adafruit_NeoPixel.h>

#ifndef PIN_RGB_LED
#define PIN_RGB_LED 38
#endif

class StatusLed {
  Adafruit_NeoPixel _px;
  uint8_t _r = 0, _g = 0, _b = 0;   // target colour at full scale (pre-brightness)
  uint8_t _brightness = 160;        // 0-255; ~63% — Boss 2026-05-31 found 25% too pale

 public:
  // Color order EMPIRICALLY corrected on the real board 2026-05-31: the panel's
  // WS2812 is RGB, not GRB. With NEO_GRB the R and G channels were swapped (amber
  // showed green, red showed green). NEO_RGB makes setColor(r,g,b) render true.
  StatusLed() : _px(1, PIN_RGB_LED, NEO_RGB + NEO_KHZ800) {}

  void begin() {
    _px.begin();
    _px.setBrightness(_brightness);  // global cap, applied on top of setPixelColor
    _px.clear();
    _px.show();
  }

  // Set the accent colour (0-255 components, pre-brightness). Idempotent: only
  // pushes to the wire when the colour actually changes, so the render loop can
  // call it every frame for free.
  void setColor(uint8_t r, uint8_t g, uint8_t b) {
    if (r == _r && g == _g && b == _b) return;
    _r = r; _g = g; _b = b;
    _px.setPixelColor(0, _px.Color(r, g, b));
    _px.show();
  }

  void setBrightness(uint8_t b) {
    if (b == _brightness) return;
    _brightness = b;
    _px.setBrightness(b);
    _px.setPixelColor(0, _px.Color(_r, _g, _b));
    _px.show();
  }

  void off() { setColor(0, 0, 0); }
};
