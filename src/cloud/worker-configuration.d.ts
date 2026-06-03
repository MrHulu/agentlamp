// Ambient types for the cloud Worker + its tests.
//
//  - "@cloudflare/vitest-pool-workers/types" declares the `cloudflare:test` module
//    (SELF, runInDurableObject, runDurableObjectAlarm, env, ...) used by test/ingest.test.ts.
//  - The ProvidedEnv augmentation types the test env bindings so `cloudflare:test`'s `env`
//    is strongly typed (the DO/KV bindings + the test-only secret vars).
/// <reference types="@cloudflare/vitest-pool-workers/types" />

declare module "cloudflare:test" {
  interface ProvidedEnv {
    RELAY: DurableObjectNamespace;
    CONFIG?: KVNamespace;
    RELAY_HOST?: string;
    AGENTLAMP_COLLECTOR_KEYS?: string;
    AGENTLAMP_DEVICE_TOKENS?: string;
    RETENTION_DAYS?: string;
    DEVICE_RATE_PER_MIN?: string;
    COLLECTOR_RATE_PER_MIN?: string;
    CA_BUNDLE?: string;
    AGENTLAMP_ADMIN_TOKEN?: string;
  }
}
