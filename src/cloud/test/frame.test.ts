/**
 * frame.test.ts — BUILD-SPEC I2: assert frame.ts reproduces EVERY frame_vectors.json golden
 * frame. Mirrors server/tests/test_parity.py::test_frame_vector_matches:
 *   for each vector: apply events -> build frame -> strip server_time + seq -> deep-equal.
 *
 * 🚨 I1: the events are ALREADY-SANITIZED envelopes; we VALIDATE (validate.ts) then apply the
 * DISPLAY logic (applySanitizedEvent) — we NEVER re-run the sanitizer transforms. The vectors'
 * values are canonical, so validation passes and the Session matches the Python reference.
 *
 * Pure functions, zero network — runs in the "parity" Node project. A FIXED `now` is used so
 * liveness ages are 0 (matching the back-to-back apply/build in the Python reference).
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { applySanitizedEvent, buildFrame, newStateData, setQuota } from "../src/frame";
import { validateQuotaEvent, validateSanitizedEvent } from "../src/validate";

const HERE = dirname(fileURLToPath(import.meta.url));
const FX = resolve(HERE, "../../../tests/fixtures/parity");

interface FrameVectorItem {
  kind: "session" | "quota";
  event: Record<string, unknown>;
}

interface FrameVector {
  name: string;
  events: FrameVectorItem[];
  expect_frame: Record<string, unknown>;
}

const vectors: FrameVector[] = JSON.parse(readFileSync(resolve(FX, "frame_vectors.json"), "utf-8"));

const FIXED_NOW = 1_780_000_100; // > every event_time, but well within STALE_AFTER_S of apply

describe("frame.ts parity (frame_vectors.json)", () => {
  it("has vectors", () => {
    expect(vectors.length).toBeGreaterThan(0);
  });

  for (const vec of vectors) {
    it(`${vec.name} → golden frame`, () => {
      const st = newStateData(FIXED_NOW);
      for (const { kind, event } of vec.events) {
        if (kind === "quota") {
          // I1 CRITICAL: validate the quota.window event through the SAME gate, then set_quota
          // (proves the validated quota path lands on the device frame, not a raw value).
          const q = validateQuotaEvent(event);
          setQuota(st, q, FIXED_NOW);
        } else {
          // I1: validate the already-sanitized session event, then apply DISPLAY logic only.
          const clean = validateSanitizedEvent(event);
          applySanitizedEvent(st, clean, FIXED_NOW);
        }
      }
      const frame = buildFrame(st, "orb-01", FIXED_NOW);
      delete frame["server_time"];
      delete frame["seq"];
      expect(frame).toEqual(vec.expect_frame);
    });
  }
});
