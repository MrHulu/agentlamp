/**
 * sign.test.ts — BUILD-SPEC I2: assert sign.ts reproduces EVERY hmac_vectors.json vector
 * byte-for-byte (canonical string + payload sha256 + HMAC signature). Loaded via Node fs from
 * tests/fixtures/parity (relative ../../tests/fixtures/parity from src/cloud). Pure functions,
 * zero network — runs in the "parity" Node project.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { canonicalString, payloadSha256Hex, sign } from "../src/sign";

const HERE = dirname(fileURLToPath(import.meta.url));
const FX = resolve(HERE, "../../../tests/fixtures/parity");

interface HmacVector {
  kid: string;
  secret_utf8: string;
  collector_id: string;
  method: string;
  path: string;
  timestamp: number;
  nonce: string;
  body_utf8: string;
  payload_sha256: string;
  canonical_string: string;
  signature: string;
}

const vectors: HmacVector[] = JSON.parse(readFileSync(resolve(FX, "hmac_vectors.json"), "utf-8"));

describe("sign.ts parity (hmac_vectors.json)", () => {
  it("has at least one vector", () => {
    expect(vectors.length).toBeGreaterThan(0);
  });

  for (const vec of vectors) {
    describe(`vector kid=${vec.kid}`, () => {
      it("payload sha256 matches the recorded body hash", async () => {
        const sha = await payloadSha256Hex(new TextEncoder().encode(vec.body_utf8));
        expect(sha).toBe(vec.payload_sha256);
      });

      it("canonical string reproduces byte-for-byte", () => {
        const canon = canonicalString(
          vec.method,
          vec.path,
          vec.kid,
          String(vec.timestamp),
          vec.nonce,
          vec.payload_sha256,
        );
        expect(canon).toBe(vec.canonical_string);
        // structural invariant from test_ingest.py::test_canonical_string_exact_shape
        expect([...canon].filter((c) => c === "\n").length).toBe(6);
        expect(canon.endsWith("\n")).toBe(false);
      });

      it("HMAC signature reproduces byte-for-byte", async () => {
        const canon = canonicalString(
          vec.method,
          vec.path,
          vec.kid,
          String(vec.timestamp),
          vec.nonce,
          vec.payload_sha256,
        );
        const sig = await sign(new TextEncoder().encode(vec.secret_utf8), canon);
        expect(sig).toBe(vec.signature);
      });
    });
  }
});
