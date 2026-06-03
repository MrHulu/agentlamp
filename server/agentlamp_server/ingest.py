"""Relay-mode signed collector ingest — auth, replay, idempotency, limits.

The authoritative contract is ``docs/api/collector_ingest_api.md`` (canonical string,
HMAC-SHA256, replay window, idempotency) and ``docs/security/security_model.md`` (controls).
This module owns ONLY the security envelope around ingest; the per-event sanitize+apply reuses
the SAME ``sanitize_event``/``FrameState.apply_event`` as local mode (the cloud's independent
sanitization gate, per ``sanitization_policy.md`` → Cloud Requirements). Local mode does NOT
use any of this — events come straight from the in-process collector.

Pure stdlib (``hmac``/``hashlib``/``re``/``time``); the time source + stores are injectable so
the security behaviour is deterministically testable without sleeping.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass, field

# Contract limits (collector_ingest_api.md → Limits / Replay Protection).
COLLECTOR_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
KID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
NONCE_RE = re.compile(r"^[0-9a-f]{16,128}$")          # lowercase hex, 64..512 bits
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
TIMESTAMP_WINDOW_S = 300                                # ±300 s
NONCE_TTL_S = 720                                       # > window + buffer
IDEMPOTENCY_TTL_S = 7 * 24 * 3600                       # 7 days
MAX_EVENTS_PER_REQUEST = 50
MAX_BODY_BYTES = 100 * 1024                             # 100 KB
SUPPORTED_SCHEMA_VERSION = 1


def payload_sha256_hex(raw_body: bytes) -> str:
    return hashlib.sha256(raw_body).hexdigest()


def canonical_string(method: str, path: str, kid: str, timestamp: str,
                     nonce: str, payload_sha256: str) -> str:
    """The authoritative byte construction (collector_ingest_api.md → Canonical Signature):
    fields joined by a single ``\\n``, no trailing newline. Every field is charset-restricted
    upstream so none can contain a newline — the parse is unambiguous."""
    return "\n".join(["v1", method, path, kid, timestamp, nonce, payload_sha256])


def sign(secret: bytes, canonical: str) -> str:
    return hmac.new(secret, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass
class _Expiring:
    """A tiny TTL set/map with lazy eviction (no background thread; checked on access)."""
    ttl_s: float
    _exp: dict = field(default_factory=dict)

    def _evict(self, now: float) -> None:
        if len(self._exp) > 4096:  # bound memory; only sweep when it grows
            for k in [k for k, e in self._exp.items() if e <= now]:
                self._exp.pop(k, None)

    def seen(self, key: str, now: float) -> bool:
        exp = self._exp.get(key)
        return exp is not None and exp > now

    def add(self, key: str, now: float) -> None:
        self._evict(now)
        self._exp[key] = now + self.ttl_s


class NonceStore(_Expiring):
    pass


class IdempotencyStore:
    """Maps an idempotency key to the prior response for ``IDEMPOTENCY_TTL_S`` so a retried
    batch returns the SAME result without re-applying the events."""

    def __init__(self, ttl_s: float = IDEMPOTENCY_TTL_S) -> None:
        self.ttl_s = ttl_s
        self._store: dict[str, tuple[float, dict]] = {}

    def get(self, key: str, now: float) -> dict | None:
        rec = self._store.get(key)
        if rec is None:
            return None
        exp, value = rec
        if exp <= now:
            self._store.pop(key, None)
            return None
        return value

    def put(self, key: str, value: dict, now: float) -> None:
        if len(self._store) > 8192:
            for k, (exp, _) in list(self._store.items()):
                if exp <= now:
                    self._store.pop(k, None)
        self._store[key] = (now + self.ttl_s, value)


class KeyStore:
    """Active collector signing secrets keyed by ``kid`` (rotation: multiple active kids).
    A revoked kid is simply absent → ``bad_signature``/``collector_revoked``. Never logged."""

    def __init__(self, keys: dict[str, bytes] | None = None) -> None:
        self._keys = dict(keys or {})

    def secret(self, kid: str) -> bytes | None:
        return self._keys.get(kid)

    def add(self, kid: str, secret: bytes) -> None:
        self._keys[kid] = secret

    def revoke(self, kid: str) -> None:
        self._keys.pop(kid, None)

    def __bool__(self) -> bool:
        return bool(self._keys)


@dataclass
class VerifyResult:
    ok: bool
    reason: str = ""
    http_status: int = 200
    server_time: int = 0


class IngestVerifier:
    """Stateful per-relay verifier: charset → payload-hash → signature → timestamp window →
    nonce replay → idempotency. Request-level failures map to an HTTP status; per-event
    sanitization happens AFTER, in the route, reusing the shared sanitizer."""

    def __init__(self, keys: KeyStore, *, now=time.time,
                 nonce_store: NonceStore | None = None,
                 idem_store: IdempotencyStore | None = None) -> None:
        self.keys = keys
        self._now = now
        self.nonces = nonce_store or NonceStore(NONCE_TTL_S)
        self.idem = idem_store or IdempotencyStore()

    def verify(self, *, collector_id: str, method: str, path: str, raw_body: bytes,
               kid: str, timestamp: str, nonce: str, payload_sha256: str,
               signature: str) -> VerifyResult:
        now = self._now()
        now_i = int(now)

        # 1. charset BEFORE signature (prevents canonical-string ambiguity/injection).
        if not COLLECTOR_ID_RE.match(collector_id or ""):
            return VerifyResult(False, "bad_collector_id", 400, now_i)
        if not KID_RE.match(kid or ""):
            return VerifyResult(False, "bad_signature", 401, now_i)
        if not NONCE_RE.match(nonce or ""):
            return VerifyResult(False, "bad_signature", 401, now_i)
        if not SHA256_HEX_RE.match(payload_sha256 or ""):
            return VerifyResult(False, "payload_hash_mismatch", 400, now_i)

        # 2. body size + payload hash (over the EXACT raw bytes).
        if len(raw_body) > MAX_BODY_BYTES:
            return VerifyResult(False, "body_too_large", 413, now_i)
        if not hmac.compare_digest(payload_sha256_hex(raw_body), payload_sha256):
            return VerifyResult(False, "payload_hash_mismatch", 400, now_i)

        # 3. signature (constant-time) against the active secret for this kid.
        secret = self.keys.secret(kid)
        if secret is None:
            # Unknown/revoked kid: do NOT distinguish (avoid an oracle). 403 = revoked-or-unknown.
            return VerifyResult(False, "collector_revoked", 403, now_i)
        expected = sign(secret, canonical_string(method, path, kid, timestamp, nonce, payload_sha256))
        if not hmac.compare_digest(expected, (signature or "").lower()):
            return VerifyResult(False, "bad_signature", 401, now_i)

        # 4. timestamp window (signature valid, so the ts is authentic — now check freshness).
        try:
            ts = int(timestamp)
        except (TypeError, ValueError):
            return VerifyResult(False, "stale_timestamp", 401, now_i)
        if abs(now_i - ts) > TIMESTAMP_WINDOW_S:
            # collector must resync from server_time (do not loop) — see contract.
            return VerifyResult(False, "stale_timestamp", 401, now_i)

        # 5. nonce replay (within the TTL that exceeds the full ts window).
        if self.nonces.seen(nonce, now):
            return VerifyResult(False, "reused_nonce", 409, now_i)
        self.nonces.add(nonce, now)

        return VerifyResult(True, "", 200, now_i)


def load_keys_from_env(env: dict) -> KeyStore:
    """Parse ``AGENTLAMP_COLLECTOR_KEYS`` = ``kid1:secret1,kid2:secret2`` into a KeyStore.
    Secrets are utf-8 bytes. Absent/empty → an empty store (the relay then rejects ALL ingest
    with ``collector_revoked`` — a safe default; a relay with no provisioned key accepts nothing).
    A real deployment SHOULD load from a secrets file/manager, never a committed default."""
    raw = (env.get("AGENTLAMP_COLLECTOR_KEYS") or "").strip()
    keys: dict[str, bytes] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        kid, _, secret = pair.partition(":")
        kid, secret = kid.strip(), secret.strip()
        if KID_RE.match(kid) and secret:
            keys[kid] = secret.encode("utf-8")
    return KeyStore(keys)
