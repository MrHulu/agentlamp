// AgentLamp — iPhone alert notifier (P2 / TASK-021). NOT a widget.
// =============================================================================
// Beats iOS's 5–15 min widget throttle: when the frame's scene is `alert` (an agent is
// WAITING for you or hit an ERROR), fire a notification. Latency is bounded by how often
// you run this script — NOT "instant" — so it's as fast as your automation interval.
//
// 🚨 HOW TO RUN — read carefully, the obvious recipe does NOT work:
//   Stock iOS Shortcuts "Time of Day" automations only repeat Daily/Weekly/Monthly —
//   there is NO built-in "every 5 minutes" trigger. For true sub-daily polling use ONE of:
//     • Pushcut "Automation Server" (an old iPad/spare device runs the script on a timer), or
//     • the server-side Worker-cron variant below (fires even when the phone is off), or
//     • several staggered Personal-Automation triggers as a coarse approximation.
//   See ./DEPLOY.md "Scheduling the alert" for the tested setup. Read-only + idempotent.
//
// DEDUP: uses the shared frame-view.shouldAlert — the SAME standing alert never double-
// notifies; an alert that clears and returns fires again; a changed task re-fires (it's in
// the key). We hash the alert identity, NOT seq — the live frame DOES carry a volatile `seq`,
// but it bumps on any content change (e.g. a fleet count), too coarse for "is this a NEW alert".
//
// 🚨 NEEDS frame-view.js saved as a Scriptable script too (shared logic, DRY).
// 🔒 RED LINE: PUBLIC file — placeholders only; real values on-device / Keychain.
//
// Server-side alternative (owner-gated, NOT implemented here): a Cloudflare Worker
// `scheduled` (cron) handler that watches the frame and POSTs to a Pushcut webhook —
// fires even when the phone is fully off. It touches the security-critical relay
// (invariants I1–I5) + needs a deploy + a stored webhook secret, so it stays a
// documented option, not code. See docs/plans/2026-06-06 §5 Step 4.
// =============================================================================

const RELAY_URL = "{RELAY_URL}";        // e.g. https://relay.example.com
const DEVICE_ID = "{DEVICE_ID}";
let   TOKEN     = "{DEVICE_TOKEN}";
const PUSHCUT_WEBHOOK = "";             // optional: a Pushcut webhook URL → push even when locked; "" = local notification only

const USE_KEYCHAIN = false;
const KC_TOKEN = "agentlamp_token";
const KC_LASTKEY = "agentlamp_alert_lastkey";
if (USE_KEYCHAIN) {
  if (Keychain.contains(KC_TOKEN)) TOKEN = Keychain.get(KC_TOKEN);
  else Keychain.set(KC_TOKEN, TOKEN);
}

const fv = importModule("frame-view");  // shared single-source-of-truth logic (DRY)

// Returns { json, status }; status=0 means the transport itself failed (no HTTP response).
async function getFrame() {
  const r = new Request(`${RELAY_URL}/api/v1/device/${DEVICE_ID}/frame`);
  r.method = "GET";
  r.headers = { "Authorization": `Bearer ${TOKEN}`, "X-Frame-Schema-Version": "1" };
  r.timeoutInterval = 10;
  const j = await r.loadJSON();
  return { json: j, status: r.response ? r.response.statusCode : 0 };
}

// Local notification + optional Pushcut webhook, each best-effort and INDEPENDENT, so one failing
// never suppresses the other. Returns true iff AT LEAST ONE delivery path succeeded — the caller
// persists the dedup key only on success, so a totally failed send retries next run (C4).
async function fire(title, body) {
  let delivered = false;
  try {
    const n = new Notification();
    n.title = title;
    n.body = body;
    n.sound = "default";
    await n.schedule();
    delivered = true;
  } catch (_) { /* local notification failed; still try the webhook */ }
  if (PUSHCUT_WEBHOOK) {                // optional: push even when phone is locked / app closed
    try {
      const r = new Request(PUSHCUT_WEBHOOK);
      r.method = "POST";
      r.headers = { "content-type": "application/json" };
      r.body = JSON.stringify({ title, text: body });
      await r.load();
      delivered = true;
    } catch (_) { /* webhook is best-effort */ }
  }
  return delivered;
}

const lastKey = Keychain.contains(KC_LASTKEY) ? Keychain.get(KC_LASTKEY) : "";
let result = { json: null, status: 0 };
try {
  result = await getFrame();
} catch (e) {
  // Transport failure → treat as transient; do NOT notify (a network blip is not an agent alert).
}
const cls = fv.classifyHttpStatus(result.status);

if (cls.pairingRequired) {
  // The relay rejects us (revoked / bad token / unknown device). The alerter's whole job is to tell
  // the owner when they must act — being unable to read state IS such a case. Fire ONCE (deduped
  // against KC_LASTKEY), persisting only after delivery so a failed send retries.
  const pkey = `pairing|${result.status}`;
  if (pkey !== lastKey) {
    const delivered = await fire("AgentLamp · re-pair needed", `relay rejected this device (HTTP ${result.status})`);
    if (delivered) Keychain.set(KC_LASTKEY, pkey);
  }
} else if (cls.ok && result.json) {
  const frame = result.json;
  const { alert, key } = fv.shouldAlert(frame, lastKey);
  if (!alert) {
    Keychain.set(KC_LASTKEY, key);     // key="" when no alert stands → resets dedup; persist immediately
  } else {
    const p = frame.primary || {};
    const title = `AgentLamp · ${p.status || "ALERT"}`;
    const body = [p.provider, p.account, p.project].filter(Boolean).join(" · ") || "agent needs you";
    const delivered = await fire(title, body);
    if (delivered) Keychain.set(KC_LASTKEY, key);   // persist ONLY after a delivery succeeds (C4)
  }
}
// transient (429 / 5xx / transport) → do nothing; lastKey untouched so a standing alert still
// fires once we recover.
Script.complete();
