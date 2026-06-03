/**
 * validate.test.ts — BUILD-SPEC I1 + I2: assert validate.ts reproduces EVERY
 * sanitize_corpus.json accept/reject decision (the cloud VALIDATE-ONLY gate). When a reason is
 * recorded, also assert the exact rejection reason string matches (proves first-violation ORDER
 * matches the Python reference, not just accept/reject). Pure functions, zero network — runs in
 * the "parity" Node project.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { SanitizationError, validateSanitizedEvent } from "../src/validate";

const HERE = dirname(fileURLToPath(import.meta.url));
const FX = resolve(HERE, "../../../tests/fixtures/parity");

interface Case {
  name: string;
  event: unknown;
  expect: "accepted" | "rejected";
  reason?: string;
}

const corpus: Case[] = JSON.parse(readFileSync(resolve(FX, "sanitize_corpus.json"), "utf-8"));

describe("validate.ts parity (sanitize_corpus.json)", () => {
  it("has both accept and reject cases", () => {
    expect(corpus.some((c) => c.expect === "accepted")).toBe(true);
    expect(corpus.some((c) => c.expect === "rejected")).toBe(true);
  });

  for (const c of corpus) {
    it(`${c.name} → ${c.expect}${c.reason ? ` (${c.reason})` : ""}`, () => {
      if (c.expect === "accepted") {
        expect(() => validateSanitizedEvent(c.event)).not.toThrow();
      } else {
        let thrown: unknown;
        try {
          validateSanitizedEvent(c.event);
        } catch (e) {
          thrown = e;
        }
        expect(thrown).toBeInstanceOf(SanitizationError);
        if (c.reason !== undefined) {
          expect((thrown as SanitizationError).reason).toBe(c.reason);
        }
      }
    });
  }
});

// ---------------------------------------------------------------------------------------------
// Case-insensitive bearer/cookie leak — HARDCODED, CORPUS-INDEPENDENT assertion.
//
// A reviewer proved a corpus co-drift hole: dropping `re.IGNORECASE` from sanitize.py's
// `\bBearer\s` / `\bCookie:` patterns (and the matching `ignorecase` flag the generator emits
// into policy.json) AND regenerating the corpus makes BOTH the Python and TS parity suites pass
// while a real LOWERCASE leak ships — the loaded corpus co-drifts with the code, so it cannot
// guard against this regression.
//
// This block reads NO fixture decision: it feeds validate() LITERAL lowercase inputs and asserts
// rejection directly. If `ignorecase` is ever dropped from the bearer/cookie patterns, validate()
// stops rejecting `bearer abc` / `cookie: secret` and this test fails regardless of any corpus
// regeneration. (Mirrored on the Python side in server/tests/test_sanitization_fixtures.py.)
describe("lowercase bearer/cookie rejected (corpus-independent, literal inputs)", () => {
  // Minimal valid envelope shell; the forbidden leaf is injected via provider_event_name (an
  // allowed envelope key, so it reaches the leaf scan rather than tripping the key allowlist).
  function evWith(leak: string): Record<string, unknown> {
    return {
      schema_version: 1,
      provider: "claude",
      provider_event_name: leak,
      provider_session_id: "hmac:abc123",
      event_time: 1_716_900_398,
      payload: { status: "CODING", task_label: "implementing" },
      sanitization: { policy_version: 1 },
    };
  }

  it("rejects lowercase 'bearer abc'", () => {
    let thrown: unknown;
    try {
      validateSanitizedEvent(evWith("bearer abc"));
    } catch (e) {
      thrown = e;
    }
    expect(thrown).toBeInstanceOf(SanitizationError);
  });

  it("rejects lowercase 'cookie: secret'", () => {
    let thrown: unknown;
    try {
      validateSanitizedEvent(evWith("cookie: secret"));
    } catch (e) {
      thrown = e;
    }
    expect(thrown).toBeInstanceOf(SanitizationError);
  });

  it("still rejects the upper/mixed-case forms (sanity)", () => {
    expect(() => validateSanitizedEvent(evWith("Bearer abc"))).toThrow(SanitizationError);
    expect(() => validateSanitizedEvent(evWith("Cookie: secret"))).toThrow(SanitizationError);
  });
});
