// AgentLamp pinned root CA bundle (relay mode TLS).
//
// firmware_contract.md §TLS: pin a LONG-LIVED ROOT (not an intermediate — Let's Encrypt
// rotates intermediates, and pinning one risks bricking a device that is offline across a
// rotation). Bundle 2+ roots for resilience so a single CA rotation does not brick the orb.
//
// The relay terminates TLS at the Cloudflare edge. Cloudflare edge certificates chain up to
// a small set of well-known long-lived roots; this file pins those roots so the same firmware
// verifies a Cloudflare-fronted relay. mbedTLS (the ESP32 TLS stack behind WiFiClientSecure)
// parses a CONCATENATED PEM blob as a trust store: setCACert() on the concatenation below
// trusts ALL of these roots. This is the "pinned bundle" — NOT setInsecure().
//
//   1. ISRG Root X1            — Let's Encrypt root, valid to 2035-06-04. RSA-4096.
//   2. DigiCert Global Root G2 — modern Cloudflare / DigiCert edge root, valid to 2038-01-15.
//   3. (optional) Baltimore CyberTrust Root — legacy Cloudflare edge chain.
//
// ┌─────────────────────────────────────────────────────────────────────────────────────┐
// │ ⚠️ PROVENANCE — these PEM bodies are NOT typed from memory.                            │
// │ Run  firmware/scripts/fetch_ca_bundle.sh  to (re)generate this file from the CAs'      │
// │ PUBLISHED roots, verified by SHA-256 fingerprint against the values in that script.    │
// │ Shipping a hand-typed / corrupted PEM would silently fail to parse → caBundleValid()   │
// │ returns false → relay handshake is REFUSED (fail-closed, never insecure). The build is │
// │ gated on CA_BUNDLE_POPULATED below.                                                    │
// └─────────────────────────────────────────────────────────────────────────────────────┘
//
// These are PUBLIC root certificates (published by the CAs); safe to commit. They are NOT a
// single-machine literal (I3) — every Cloudflare-fronted relay on earth chains to these.

#pragma once

#include <Arduino.h>

// ───────────────────────────────────────────────────────────────────────────────────────
// CA_BUNDLE_POPULATED selects whether the committed, fingerprint-verified roots
// (src/ca/*.pem.inc) are #include'd as the compiled trust anchor.
//
// DEFAULT POLICY (devlog 16 verifier fix):
//   - A RELAY_MODE build defaults this to 1. The pinned roots ARE committed to the repo
//     (src/ca/*.pem.inc, SHA-256-verified by fetch_ca_bundle.sh / --check), so a stock
//     `pio run -e relay` MUST yield a working device — not one bricked behind an empty
//     trust store (RELAY_ERR_NO_CA forever, with /cacerts unable to bootstrap because the
//     refresh path itself needs an existing anchor to verify the new bundle securely).
//   - A non-relay (local) build defaults this to 0: local mode talks plain HTTP and never
//     loads this bundle, so there is nothing to populate.
//
// FAIL-CLOSED IS PRESERVED AT RUNTIME, NOT AT BUILD TIME:
//   Even with CA_BUNDLE_POPULATED=1, if the included PEM is genuinely absent or corrupt
//   (didn't parse) pemLooksValid() returns false for every root → loadCaBundle() yields
//   no trust anchor → the transport REFUSES the handshake (fail-closed → Diagnostics),
//   never setInsecure(). So the default is "use the committed roots", and the only way a
//   relay device talks plaintext-trust is never — a broken bundle still fails closed.
//
// An explicit -D CA_BUNDLE_POPULATED=0 still forces the empty placeholder (e.g. to prove
// the fail-closed path on a build that deliberately omits the roots).
#ifndef CA_BUNDLE_POPULATED
#if defined(RELAY_MODE) && RELAY_MODE
// Relay build: the committed roots are the default trust anchor (working out of the box).
#define CA_BUNDLE_POPULATED 1
#else
// Local build: bundle unused; keep it empty.
#define CA_BUNDLE_POPULATED 0
#endif
#endif

// ---- ISRG Root X1 (Let's Encrypt) — valid to 2035-06-04 ----
// SHA-256: 96:BC:EC:06:26:49:76:F3:74:60:77:9A:CF:28:C5:A7:CF:E8:A3:C0:AA:E1:1A:8F:FC:EE:05:C0:BD:DF:08:C6
// NOTE on the #include layout below: the .pem.inc files emit a bare raw-string literal
// ( R"PEM(...)PEM" ) with NO trailing semicolon, so the `;` lives AFTER the #endif and
// terminates whichever branch (populated literal OR placeholder "") was selected. Putting
// the `;` inside a branch would leave the populated branch unterminated.
static const char ROOT_CA_ISRG_X1[] =
#if CA_BUNDLE_POPULATED
#include "ca/isrg_root_x1.pem.inc"   // populated + SHA-256-verified by fetch_ca_bundle.sh
#else
    ""   // PLACEHOLDER — fetch_ca_bundle.sh not yet run; relay handshake fails closed
#endif
    ;

// ---- DigiCert Global Root G2 — Cloudflare / DigiCert edge root, valid to 2038-01-15 ----
// SHA-256: CB:3C:CB:B7:60:31:E5:E0:13:8F:8D:D3:9A:23:F9:DE:47:FF:C3:5E:43:C1:14:4C:EA:27:D4:6A:5A:B1:CB:5F
static const char ROOT_CA_DIGICERT_G2[] =
#if CA_BUNDLE_POPULATED
#include "ca/digicert_global_root_g2.pem.inc"
#else
    ""
#endif
    ;

// ---- Baltimore CyberTrust Root — legacy Cloudflare edge chain (optional 3rd root) ----
// SHA-256: 16:AF:57:A9:F6:76:B0:AB:12:60:95:AA:5E:BA:DE:F2:2A:B3:11:19:D6:44:AC:95:CD:4B:93:DB:F3:F2:6A:EB
static const char ROOT_CA_BALTIMORE[] =
#if CA_BUNDLE_POPULATED
#include "ca/baltimore_cybertrust_root.pem.inc"
#else
    ""
#endif
    ;

// Cheap structural validity check: a real PEM root starts with the BEGIN marker. The relay
// transport calls this BEFORE the first handshake and REFUSES (fail-closed) if it returns
// false — so a placeholder / corrupted bundle can never downgrade to an unverified connection.
static inline bool pemLooksValid(const char* pem) {
  return pem && strstr(pem, "-----BEGIN CERTIFICATE-----") != nullptr &&
         strstr(pem, "-----END CERTIFICATE-----") != nullptr;
}
