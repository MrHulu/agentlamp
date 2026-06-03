import { defineConfig } from "vitest/config";

// Two projects (build-spec I2: parity verification must run with ZERO cloud auth / network):
//
//   1. "parity" — pure-function corpus tests (sign / validate / frame). They load the
//      generated fixtures in tests/fixtures/parity via Node `fs` and assert byte-for-byte
//      against the Python reference. Plain Node environment so `fs` + WebCrypto are native
//      and NO workerd runtime is needed — the load-bearing I2 assertions are always
//      verifiable offline. (Defined inline here.)
//
//   2. "workers" — the request/DO security tests (test/ingest.test.ts). These exercise the
//      real Worker + Durable Object inside workerd via @cloudflare/vitest-pool-workers'
//      cloudflareTest() Vite plugin. Defined in ./vitest.workers.config.ts so its plugin /
//      module-resolution overrides stay isolated from the Node project.
export default defineConfig({
  test: {
    projects: [
      {
        test: {
          name: "parity",
          environment: "node",
          include: [
            "test/sign.test.ts",
            "test/validate.test.ts",
            "test/quota.test.ts",
            "test/frame.test.ts",
            "test/frame_round.test.ts",
          ],
        },
      },
      "./vitest.workers.config.ts",
    ],
  },
});
