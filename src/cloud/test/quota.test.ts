/**
 * quota.test.ts — BUILD-SPEC I1 + I2: assert validateQuotaEvent reproduces EVERY
 * quota_corpus.json accept/reject decision + reason string (the CRITICAL second gate the
 * quota.window ingest branch previously BYPASSED — it called setQuota directly with the
 * attacker-controlled account_alias/provider, serving e.g. "/Users/.../secret" to the device).
 *
 * Plus a CORPUS-INDEPENDENT NaN/Infinity assertion: a literal NaN used_ratio can't round-trip
 * through JSON, so the corpus models the non-finite case with "x"/1.5; here we feed validate()
 * a literal NaN/Infinity directly and assert REJECT — the exact divergence the fix closes (TS
 * previously fed Number(...)→NaN straight into setQuota where Python's float() rejected).
 *
 * Pure functions, zero network — runs in the "parity" Node project.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { SanitizationError, validateQuotaEvent } from "../src/validate";

const HERE = dirname(fileURLToPath(import.meta.url));
const FX = resolve(HERE, "../../../tests/fixtures/parity");

interface Case {
  name: string;
  event: unknown;
  expect: "accepted" | "rejected";
  reason?: string;
}

const corpus: Case[] = JSON.parse(readFileSync(resolve(FX, "quota_corpus.json"), "utf-8"));

describe("validateQuotaEvent parity (quota_corpus.json)", () => {
  it("has both accept and reject cases", () => {
    expect(corpus.some((c) => c.expect === "accepted")).toBe(true);
    expect(corpus.some((c) => c.expect === "rejected")).toBe(true);
  });

  for (const c of corpus) {
    it(`${c.name} → ${c.expect}${c.reason ? ` (${c.reason})` : ""}`, () => {
      if (c.expect === "accepted") {
        expect(() => validateQuotaEvent(c.event)).not.toThrow();
      } else {
        let thrown: unknown;
        try {
          validateQuotaEvent(c.event);
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
// CORPUS-INDEPENDENT non-finite used_ratio reject (the NaN divergence FIX, literal inputs).
//
// NaN / Infinity are not JSON-representable, so the loaded corpus CANNOT carry them — it models
// the case with "x" (Number("x")→NaN) and 1.5 (out of range). This block feeds validate() LITERAL
// NaN / Infinity and asserts rejection directly, matching Python float() which produces nan/inf
// that the math.isfinite gate rejects. If the Number.isFinite gate is ever removed, validate()
// stops rejecting and these fail regardless of any corpus regeneration.
describe("quota non-finite used_ratio rejected (corpus-independent, literal NaN/Infinity)", () => {
  function quotaEv(usedRatio: unknown): Record<string, unknown> {
    return {
      event_id: "q",
      event_type: "quota.window",
      provider: "claude",
      account_alias: "main",
      payload: { window_type: "5h", used_ratio: usedRatio },
    };
  }

  it("rejects literal NaN used_ratio", () => {
    expect(() => validateQuotaEvent(quotaEv(Number.NaN))).toThrow(SanitizationError);
  });

  it("rejects literal Infinity used_ratio", () => {
    expect(() => validateQuotaEvent(quotaEv(Number.POSITIVE_INFINITY))).toThrow(SanitizationError);
  });

  it("accepts a finite in-range used_ratio (sanity)", () => {
    expect(() => validateQuotaEvent(quotaEv(0.95))).not.toThrow();
  });
});
