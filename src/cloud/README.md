# AgentLamp Cloud Relay (Cloudflare Worker + Durable Object + KV)

Piece C of the relay build (docs/devlog/16). TypeScript. The **VALIDATE-ONLY** cloud gate.

- `src/policy.ts` — embeds the generated `tests/fixtures/parity/policy.json` (enums never hand-retyped).
- `src/sign.ts` — canonical string + WebCrypto HMAC verify (byte-for-byte `server/.../ingest.py`).
- `src/validate.ts` — the I1 validate-only gate (mirrors `validate.py`; REJECT, never coerce).
- `src/frame.ts` — DISPLAY logic port of `state.py` (priority / scene / fleet / quota / 2KB cap).
- `src/relay_do.ts` — `RelayDO` Durable Object (I4: nonce / idempotency / registry / revocation /
  materialized state / purge-audit alarm). KV `CONFIG` = non-urgent config/cache only.
- `src/index.ts` — Worker fetch entry: route + edge rate-limit + uniform auth errors.

## Verify (no cloud auth needed)

```sh
npm install
npx vitest run        # parity (Node) + workers (workerd) projects — all green
npx tsc --noEmit      # typecheck
npx wrangler deploy --dry-run   # build-only (real deploy is owner-gated; see docs/cloud/deploy.md)
```

`wrangler login` + `wrangler deploy` + `wrangler secret put` are owner-gated one-time steps —
do NOT run them autonomously. `wrangler.toml` ships placeholders only (no account/zone/host literals, I3).
