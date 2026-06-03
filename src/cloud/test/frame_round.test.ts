/**
 * frame_round.test.ts — verifier-found LOW defect #3: frame.ts round2() must match Python's
 * built-in round() (round-half-to-even / banker's rounding) on the TRUE IEEE-754 value, NOT
 * half-away-from-zero. The quota block feeds used_ratio through round2 and the frame signature
 * is byte-stable, so a 1-cent divergence would desync the frame seq vs the Python reference.
 *
 * The .x25 / .x35 boundaries are the canonical demonstrations: 0.125 is EXACTLY representable so
 * it ties-to-even DOWN (→0.12); 0.135's stored double is 0.13500000000000001 → rounds UP (→0.14);
 * 0.145's stored double is 0.1449999… → rounds DOWN (→0.14, NOT a tie); 2.675 → 2.67. Naive
 * half-away gave 0.13 / 0.14 / 0.15 / 2.68 — exactly the silent divergence this guards.
 *
 * Reference column generated from CPython: `python3 -c "print(round(x,2))"`.
 */
import { describe, expect, it } from "vitest";

import { round2 } from "../src/frame";

describe("round2 = Python round(x, 2) banker's rounding (defect #3)", () => {
  // [input, Python round(input, 2)]
  const cases: Array<[number, number]> = [
    [0.125, 0.12], // exact tie → to-even DOWN (half-away would give 0.13)
    [0.135, 0.14], // stored double rounds UP
    [0.145, 0.14], // stored double is 0.1449… → DOWN (half-away would give 0.15)
    [0.155, 0.15], // stored double rounds DOWN (half-away would give 0.16)
    [0.005, 0.01],
    [0.015, 0.01], // stored double → DOWN (half-away would give 0.02)
    [0.025, 0.03],
    [0.115, 0.12],
    [2.675, 2.67], // classic float gotcha (half-away would give 2.68)
    [-0.125, -0.12], // sign-symmetric to-even
    [-0.135, -0.14],
    [0.0, 0.0],
    [1.0, 1.0],
    [0.1, 0.1],
    [0.9999, 1.0],
    [0.45, 0.45],
    [0.46, 0.46],
  ];

  for (const [input, expected] of cases) {
    it(`round2(${input}) === ${expected}`, () => {
      expect(round2(input)).toBe(expected);
    });
  }

  it("explicitly: 0.125 -> 0.12 and 0.135 -> 0.14 (the named .x25 boundary)", () => {
    expect(round2(0.125)).toBe(0.12);
    expect(round2(0.135)).toBe(0.14);
  });

  it("preserves +0 (never returns -0)", () => {
    expect(Object.is(round2(0), 0)).toBe(true);
    expect(Object.is(round2(0.004), 0)).toBe(true); // rounds to 0, must be +0 not -0
  });

  it("passes through non-finite values unchanged", () => {
    expect(Number.isNaN(round2(NaN))).toBe(true);
    expect(round2(Infinity)).toBe(Infinity);
  });
});
