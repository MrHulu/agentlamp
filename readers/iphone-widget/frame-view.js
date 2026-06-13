// frame-view.js — AgentLamp reader shared logic (PURE: no Scriptable / no Node I/O).
// =============================================================================
// SINGLE SOURCE OF TRUTH (DRY) for everything that interprets a schema-v1 device
// frame. Consumed three ways, unchanged:
//   • agentlamp-alert.js        → Scriptable `importModule("frame-view")`
//   • test/frame-view.test.cjs  → Node `require("../frame-view.js")`
// The home-screen widget is intentionally single-file for easier phone deployment.
//
// SOLID boundary: this module is PURE transform — it must never touch Request /
// Keychain / Notification / fetch / fs / Date-based scheduling. I/O + UI live in the
// renderer scripts; deterministic frame→view logic lives here so it stays testable on
// any machine and identical across both readers.
//
// Canonical schema-v1 frame (served by BOTH the local server and the cloud relay; the static
// parity goldens in tests/fixtures/parity/frame_vectors.json STRIP the two volatile fields
// `seq` + `server_time` before comparison, but live frames DO carry them):
//   { v, device_id, scene: focus|fleet|alert|sleep, headline, accent, ttl,
//     primary:{ provider, account, status, project, task },
//     fleet:[{ provider, count, status }],                    // fleet[].provider = displayLabel
//     quota:[{ provider, account, w5, week, estimated, confidence }],  // w5 + week both when known
//     seq, server_time }                                      // volatile; this reader IGNORES both
//   on purpose: seq bumps on ANY content change (too coarse for alert identity → we hash content
//   instead); staleness is measured from local fetch time (like firmware millis()), never server_time.
// =============================================================================

const ACCENT = {
  purple: "#A78BFA", cyan: "#22D3EE", green: "#34D399", yellow: "#FBBF24",
  red: "#F87171", blue: "#60A5FA", white: "#E5E7EB", muted: "#6B7280",
};
const DEFAULT_ACCENT = "#60A5FA";

function accentHex(frame) {
  return (frame && ACCENT[frame.accent]) || DEFAULT_ACCENT;
}

// Pure: schema-v1 frame → flat view-model the renderers map to text rows.
// Surfaces primary.account — the multi-device disambiguator (which machine/identity an
// agent runs on). It is null on a single-machine setup, so renderers can omit it cleanly.
function buildViewModel(frame) {
  frame = frame || {};
  const p = frame.primary || {};
  const fleet = Array.isArray(frame.fleet) ? frame.fleet : [];
  const quota = Array.isArray(frame.quota) ? frame.quota : [];
  const q0 = quota[0];
  // A quota row carries BOTH w5 (5-hour) and week windows when known; surface the HIGHER-risk one
  // so a near-full weekly cap is never hidden behind a calm w5 (and vice versa). Null if neither.
  let w = null;
  if (q0) {
    const windows = [q0.w5, q0.week].filter((x) => x != null);
    w = windows.length ? Math.max.apply(null, windows) : null;
  }
  const pct = w == null ? null : Math.round(w * 100);
  return {
    headline: frame.headline || "AGENTLAMP",
    accentHex: accentHex(frame),
    statusLine: `${p.provider || "—"} · ${p.status || ""}`.trim(),
    account: p.account || null,                 // multi-device disambiguator (null = single machine)
    project: p.project || "—",
    task: p.task || null,
    fleetRows: fleet.slice(0, 3).map((r) => ({
      label: `${r.provider}  ×${r.count}  ${r.status}`,
    })),
    // server's fleet_more counts agents beyond ITS 5-row cap; add the rows we drop locally (>3)
    // so "+N more" reflects everything actually hidden on this small widget.
    fleetMore: (frame.fleet_more || 0) + Math.max(0, fleet.length - 3),
    quota: pct == null ? null : { text: `quota ${q0.provider || ""} ${pct}%`.trim(), critical: pct >= 90 },
  };
}

// Pure: dedup key for an alert frame. We hash the alert IDENTITY (not the volatile seq, which
// bumps on any content change) — the SAME standing alert keeps one key; a genuinely new alert
// gets a different one. `task` IS part of the identity: a changed WAITING/ERROR task on the same
// provider/account/project/status is a NEW thing the owner must see, so it must re-fire.
function alertKey(frame) {
  const p = (frame && frame.primary) || {};
  return [frame && frame.scene, p.provider, p.status, p.project, p.account, p.task].join("|");
}

// Pure: should this frame fire a push, and what key should the caller persist?
// Returns key="" when there is no alert, which RESETS dedup — so an alert that clears and
// later returns will fire again, while a still-standing alert never double-fires.
// Caller contract: ALWAYS persist the returned `key`; fire a notification iff `alert` is true.
function shouldAlert(frame, lastKey) {
  const isAlert = !!frame && frame.scene === "alert";
  const key = isAlert ? alertKey(frame) : "";
  return { alert: isAlert && key !== lastKey, key };
}

// Pure: map an HTTP status from /frame to a reader action, shared by both readers (DRY) so the
// auth-vs-transient decision can never drift between them. Mirrors device_frame_api.md Error
// Responses: 401/403/404 are retry:false → the device must STOP and re-pair, and must NEVER serve
// cache (a revoked phone would otherwise keep rendering last-good agent data). 429 + 5xx are
// transient → keep showing the last good frame. Other 4xx are surfaced as errors, not masked.
function classifyHttpStatus(code) {
  if (code == null || code < 400) return { ok: true, pairingRequired: false, useCache: false };
  if (code === 401 || code === 403 || code === 404) return { ok: false, pairingRequired: true, useCache: false };
  if (code === 429 || code >= 500) return { ok: false, pairingRequired: false, useCache: true };
  return { ok: false, pairingRequired: false, useCache: false };
}

module.exports = { ACCENT, DEFAULT_ACCENT, accentHex, buildViewModel, alertKey, shouldAlert, classifyHttpStatus };
