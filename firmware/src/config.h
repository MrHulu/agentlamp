// AgentLamp non-secret config — SAFE TO COMMIT.
//
// This file holds ONLY non-secret defaults:
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
//
// ───────────────────────────────────────────────────────────────────────────────────────
// I3 (relay-cloudflare-build-spec.md): NO single-machine / single-network hardcodes in the
// relay path. The two single-machine literals below — the 192.168.1.148 LAN IP and the
// "yangzhenzhous-macbook-air" mDNS host — are LOCAL-MODE ONLY. They are compiled out of a
// RELAY_MODE build (-D RELAY_MODE=1, see [env:relay] in platformio.ini): a relay image gets
// NO compiled base URL and NO mDNS host, so its relay URL + device token + CA bundle come
// from NVS provisioning ONLY. Local mode (the LAN fallback) keeps its literals, clearly
// bounded behind #if !RELAY_MODE, and is NOT deleted.

#pragma once

// RELAY_MODE selects the transport. Default 0 = local mode (LAN http + mDNS). A relay build
// sets -D RELAY_MODE=1; in that build the single-machine LAN literals MUST NOT appear.
#ifndef RELAY_MODE
#define RELAY_MODE 0
#endif

#ifndef FRAME_BASE_URL
#if RELAY_MODE
// Relay build: NO single-machine default. Empty base url forces NVS provisioning — an
// un-provisioned relay orb shows the pairing/setup scene rather than dialing a hardcoded host.
#define FRAME_BASE_URL ""
#else
// Local mode only: LAN fallback IP. Overridden by the captive-portal value in NVS.
#define FRAME_BASE_URL "http://192.168.1.148:8787"
#endif
#endif

// mDNS service discovery — local-mode robustness for "the server's DHCP IP changed and the
// orb went offline". The frame server's host advertises <LocalHostName>.local on the LAN, and
// macOS/Bonjour keeps that name mapped to the host's CURRENT IP automatically. The firmware
// resolves this name at boot AND re-resolves on transport failure, so a DHCP-reassigned
// server IP is followed with no reflash. Empty string ("") disables mDNS entirely.
//
// I3: a RELAY build has NO mDNS host literal — the relay URL is a FIXED https endpoint from
// NVS, discovery is not a relay concern, and "yangzhenzhous-macbook-air" must never compile in.
#ifndef FRAME_MDNS_HOST
#if RELAY_MODE
#define FRAME_MDNS_HOST ""
#else
// Local mode only. If you rename the host or move machines, set this to the new
// `scutil --get LocalHostName` value (local LAN discovery only — never used in relay mode).
#define FRAME_MDNS_HOST "yangzhenzhous-macbook-air"
#endif
#endif

// Frame server port — MUST match the server bind port (AGENTLAMP_BIND, default 8787);
// used to rebuild the poll URL after mDNS resolves the host's current IP.
#ifndef FRAME_SERVER_PORT
#define FRAME_SERVER_PORT 8787
#endif

#ifndef DEVICE_ID
#define DEVICE_ID "orb-01"
#endif

#ifndef DEVICE_TOKEN
#if RELAY_MODE
// Relay build: NO compiled token. The real relay device token is provisioned at pairing into
// NVS (Provisioning::Creds.token). An empty default makes an un-provisioned relay orb fail the
// pairing gate rather than ship a bogus credential. (I3 + firmware_contract.md.)
#define DEVICE_TOKEN ""
#else
#define DEVICE_TOKEN "dev-local-token"
#endif
#endif

// ── I3 compile-time guard ────────────────────────────────────────────────────────────────
// A RELAY_MODE image must contain NO single-machine literal in the relay path. These static
// asserts fail the build the instant someone re-adds the LAN IP / mDNS host to a relay build.
#if RELAY_MODE
#include <string.h>
// constexpr string-equality so the check runs at compile time (C++14: single return stmt).
static constexpr bool _ceq(const char* a, const char* b) {
  return (*a == *b) && (*a == '\0' ? true : _ceq(a + 1, b + 1));
}
static_assert(!_ceq(FRAME_BASE_URL,  "http://192.168.1.148:8787"),
              "I3 violation: single-machine FRAME_BASE_URL must not compile into a relay build");
static_assert(!_ceq(FRAME_MDNS_HOST, "yangzhenzhous-macbook-air"),
              "I3 violation: single-machine FRAME_MDNS_HOST must not compile into a relay build");
#endif
