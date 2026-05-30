# Tests

Required test groups:

- `unit`: sanitizer, HMAC canonicalization, frame generation.
- `integration`: collector ingest, replay protection, idempotency, device frame endpoint.
- `e2e`: admin simulator screenshots, weak-network/manual firmware checklist.

No provider adapter is allowed until sanitizer tests cover paths, tokens, cookies, prompt text, and source snippets.

