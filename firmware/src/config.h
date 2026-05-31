// AgentLamp non-secret config — SAFE TO COMMIT.
//
// This file holds ONLY local-mode defaults that contain no secrets:
//   - FRAME_BASE_URL : default frame source for a fresh device. Overridden at
//                      provisioning time by the value the user types into the
//                      captive portal (stored in NVS). This is just the fallback.
//   - DEVICE_ID      : logical id this orb advertises (orb-01). Not a secret.
//   - DEVICE_TOKEN   : a *local-mode* bearer token. "dev-local-token" is the
//                      shared local default and is NOT a real credential — it
//                      only authenticates to a frame server you run yourself on
//                      your LAN. Real relay tokens are provisioned at pairing
//                      and stored in NVS, never compiled in.
//
// ⚠️ WiFi SSID and password are NEVER stored in source. They live ONLY in NVS
// (Preferences, namespace "agentlamp") after the user enters them through the
// SoftAP captive portal. See provisioning.h and docs/devlog/04-provisioning-impl.md.

#pragma once

#ifndef FRAME_BASE_URL
#define FRAME_BASE_URL "http://192.168.1.148:8787"
#endif

#ifndef DEVICE_ID
#define DEVICE_ID "orb-01"
#endif

#ifndef DEVICE_TOKEN
#define DEVICE_TOKEN "dev-local-token"
#endif
