// AgentLamp DIAG v3 — per-pin backlight identification.
// The panel/SPI is confirmed working (white+text rendered in v2). So let the SCREEN
// report the answer: cycle each candidate GPIO HIGH for 3s while the framebuffer shows
// "PIN <N>". The screen is only VISIBLE when the real backlight pin is the one driven —
// so when it lights, the number on screen IS the backlight pin. (48 excluded: proven
// dark alone in v1, and its Light_PWM/LEDC would fight a manual digitalWrite.)
#include <Arduino.h>
#include "display.h"
#include "led.h"

static AgentLampDisplay gfx;
static StatusLed led;
static const int CANDS[] = {47, 46, 21, 18, 17, 16, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 2, 1};
static const int N = sizeof(CANDS) / sizeof(CANDS[0]);

void setup() {
  Serial.begin(115200);
  delay(800);
  Serial.println();
  Serial.println("==== DIAG v3: per-pin backlight cycle ====");
  led.begin();
  led.setColor(30, 30, 30);
  for (int i = 0; i < N; i++) { pinMode(CANDS[i], OUTPUT); digitalWrite(CANDS[i], LOW); }
  bool ok = gfx.init();
  Serial.printf("gfx.init=%d (backlight driven manually; setBrightness NOT used)\n", ok ? 1 : 0);
  gfx.setRotation(0);
  Serial.println("Cycling each candidate HIGH 3s; screen shows 'PIN <N>'.");
  Serial.println("READ THE NUMBER ON SCREEN at the moment it LIGHTS UP -> that's the backlight pin.");
}

void loop() {
  for (int i = 0; i < N; i++) {
    for (int j = 0; j < N; j++) digitalWrite(CANDS[j], LOW);   // all off
    // render the label first (only visible once the right pin powers the backlight)
    gfx.fillScreen(0x0008);
    gfx.setTextColor(0xFFFF, 0x0008);
    gfx.setTextSize(4);
    gfx.setCursor(28, 110);
    gfx.print("PIN");
    gfx.setTextSize(7);
    gfx.setCursor(46, 165);
    gfx.printf("%d", CANDS[i]);
    digitalWrite(CANDS[i], HIGH);   // drive this candidate
    Serial.printf("  testing GPIO %d (3s)\n", CANDS[i]);
    delay(3000);
  }
}
