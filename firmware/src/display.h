// AgentLamp display — LovyanGFX panel for the Waveshare ESP32-S3-LCD-1.47B.
//
// Panel: ST7789, 172x320 IPS (portrait). The ST7789 controller has a 240x320
// GRAM but the glass exposes only a 172-wide window centred in that GRAM, so a
// COLUMN OFFSET of 34 is mandatory — without it the image is shifted left and
// the right 34 px column is clipped / wraps. invert=true and rgb_order=false
// (BGR) are required or colours come out inverted / channel-swapped.
//
// All values below are copied verbatim from a proven working config:
//   - github.com/ahmadrezarazian/Waveshare_ESP32-S3-LCD1.47_3D-Box (LovyanGFX)
//   - esp-cpp/espp ws-s3-lcd-1-47 board support package (pin map cross-check)
// Do NOT invent these numbers.

#pragma once

#define LGFX_USE_V1
#include <LovyanGFX.hpp>

class AgentLampDisplay : public lgfx::LGFX_Device {
  lgfx::Panel_ST7789 _panel;
  lgfx::Bus_SPI      _bus;
  lgfx::Light_PWM    _light;

 public:
  AgentLampDisplay() {
    // --- SPI bus ---
    // SPI3_HOST (== the "USE_HSPI_PORT" fix other libs need on this board).
    // 80 MHz write clock is what the reference runs the cube demo at.
    {
      auto cfg = _bus.config();
      cfg.spi_host   = SPI3_HOST;
      cfg.spi_mode   = 0;
      cfg.freq_write = 80000000;   // 80 MHz
      cfg.freq_read  = 16000000;
      cfg.spi_3wire  = false;
      cfg.use_lock   = true;
      cfg.dma_channel = SPI_DMA_CH_AUTO;
      cfg.pin_sclk   = 40;  // PIN_LCD_SCLK
      cfg.pin_mosi   = 45;  // PIN_LCD_MOSI
      cfg.pin_miso   = -1;  // unused
      cfg.pin_dc     = 41;  // PIN_LCD_DC
      _bus.config(cfg);
      _panel.setBus(&_bus);
    }

    // --- Panel geometry + colour (the offsets are the whole point) ---
    {
      auto cfg = _panel.config();
      cfg.pin_cs   = 42;   // PIN_LCD_CS
      cfg.pin_rst  = 39;   // PIN_LCD_RST
      cfg.pin_busy = -1;

      // ST7789 GRAM is 240x320; this glass is a 172-wide window inside it.
      cfg.memory_width  = 320;   // controller native (landscape) memory
      cfg.memory_height = 172;
      cfg.panel_width   = 172;   // visible portrait panel
      cfg.panel_height  = 320;
      cfg.offset_x      = 34;    // <-- COLUMN OFFSET: (240-172)/2 = 34. mandatory.
      cfg.offset_y      = 0;
      cfg.offset_rotation = 0;

      cfg.dummy_read_pixel = 8;
      cfg.dummy_read_bits  = 1;
      cfg.readable    = false;
      cfg.invert      = true;    // ST7789 on this board needs inversion ON
      cfg.rgb_order   = false;   // false = BGR (confirmed by TFT_eSPI users + reference)
      cfg.dlen_16bit  = false;
      cfg.bus_shared  = false;
      _panel.config(cfg);
    }

    // --- Backlight (active-high PWM on GPIO46) ---
    // EMPIRICALLY CONFIRMED on the real ESP32-S3-LCD-1.47B: a pin sweep showed the
    // backlight is GPIO46, NOT 48. The espp/non-B docs say 48 and an earlier review
    // "corrected" 46->48 — that broke the backlight (screen stayed dark). The B variant
    // uses 46. Driving 46 HIGH lights the panel; active-high (invert=false).
    {
      auto cfg = _light.config();
      cfg.pin_bl      = 46;   // PIN_LCD_BL — verified by hardware sweep 2026-05-30
      cfg.invert      = false;
      cfg.freq        = 44100;
      cfg.pwm_channel = 7;
      _light.config(cfg);
      _panel.setLight(&_light);
    }

    setPanel(&_panel);
  }
};
