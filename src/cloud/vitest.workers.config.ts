import { cloudflareTest } from "@cloudflare/vitest-pool-workers";
import { defineConfig } from "vitest/config";

// "workers" project — the request/DO security tests run inside workerd (SELF fetch +
// real RelayDO). Uses @cloudflare/vitest-pool-workers' cloudflareTest() Vite plugin
// (0.16 API; requires vitest >= 4.1). Reads bindings/DO/KV from ./wrangler.toml.
//
// Test-only secrets are injected via miniflare.bindings here (NOT committed in wrangler.toml,
// which ships placeholders only — build-spec I3). The HMAC key matches the hmac_vectors.json
// k1 vector so the signed-ingest tests reproduce real signatures.
//
// k2 / orb-02 exist ONLY for the I4 revocation tests (revoke a PREVIOUSLY-VALID kid/device →
// a subsequent correctly-signed/correctly-tokened request is rejected). They are kept separate
// from k1 / orb-01 so revoking them never poisons the shared singleton DO state used by the
// happy-path tests.
export default defineConfig({
  test: {
    name: "workers",
    include: ["test/ingest.test.ts"],
  },
  plugins: [
    cloudflareTest({
      wrangler: { configPath: "./wrangler.toml" },
      miniflare: {
        bindings: {
          // k1/orb-01 = happy path; k2/orb-02 = DO-direct revocation tests; k3/orb-03 = PUBLIC
          // /admin route revocation tests (revoke via POST /admin/... then prove rejection).
          AGENTLAMP_COLLECTOR_KEYS: "k1:test-collector-secret,k2:test-collector-secret-2,k3:test-collector-secret-3",
          AGENTLAMP_DEVICE_TOKENS: "orb-01:dev-local-token,orb-02:dev-local-token-2,orb-03:dev-local-token-3",
          // Admin bearer for the in-Worker /admin gate (revoke routes). The fail-CLOSED test
          // (unset token → 403) calls the Worker's fetch() directly with this binding STRIPPED,
          // so it does not need a second config.
          AGENTLAMP_ADMIN_TOKEN: "test-admin-token",
        },
      },
    }),
  ],
});
