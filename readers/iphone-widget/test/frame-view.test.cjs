// Conformance tests for the iPhone reader's shared logic (frame-view.js).
// Zero-dep, cross-platform: Node's built-in runner only.   Run (explicit file — `--test <dir>`
// is flaky on Node ≥ 25):
//   node --test readers/iphone-widget/test/frame-view.test.cjs
// Asserts the reader interprets the SAME schema-v1 frames the relay actually emits, by loading
// the canonical parity fixtures (the single source of truth the cloud is tested against). The
// fixtures cover the happy paths; the synthetic frames below cover the edge cases the fixtures
// don't (dual-window quota, task-only alert change, local fleet recap, HTTP status routing).

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const fv = require("../frame-view.js");

const FIX = path.join(__dirname, "..", "..", "..", "tests", "fixtures", "parity", "frame_vectors.json");
const vectors = JSON.parse(fs.readFileSync(FIX, "utf8"));
const frames = vectors.map((v) => v.expect_frame).filter(Boolean);

test("fixtures load and cover multiple scenes", () => {
  assert.ok(frames.length >= 3, "expected several frame vectors");
  const scenes = new Set(frames.map((f) => f.scene));
  assert.ok(scenes.has("alert"), "fixtures should include an alert scene");
});

test("buildViewModel renders every fixture frame without throwing", () => {
  for (const f of frames) {
    const vm = fv.buildViewModel(f);
    assert.ok(typeof vm.headline === "string" && vm.headline.length > 0);
    assert.match(vm.accentHex, /^#[0-9A-Fa-f]{6}$/);
    assert.equal(typeof vm.statusLine, "string");
    assert.ok(Array.isArray(vm.fleetRows));
    assert.ok(vm.fleetRows.length <= 3, "fleet rows capped at 3 for the widget");
  }
});

test("buildViewModel surfaces primary.account (multi-device disambiguator)", () => {
  const withAccount = frames.find((f) => f.primary && f.primary.account);
  assert.ok(withAccount, "fixtures should include a frame with primary.account");
  assert.equal(fv.buildViewModel(withAccount).account, withAccount.primary.account);
});

test("buildViewModel is defensive on empty / garbage input", () => {
  assert.doesNotThrow(() => fv.buildViewModel(null));
  assert.doesNotThrow(() => fv.buildViewModel(undefined));
  const vm = fv.buildViewModel({});
  assert.equal(vm.account, null);
  assert.deepEqual(vm.fleetRows, []);
  assert.equal(vm.quota, null);
});

test("accentHex maps known accents and falls back safely", () => {
  assert.equal(fv.accentHex({ accent: "purple" }), "#A78BFA");
  assert.equal(fv.accentHex({ accent: "does-not-exist" }), fv.DEFAULT_ACCENT);
  assert.equal(fv.accentHex({}), fv.DEFAULT_ACCENT);
  assert.equal(fv.accentHex(null), fv.DEFAULT_ACCENT);
});

test("buildViewModel formats quota when the frame carries it", () => {
  const withQuota = frames.find((f) => Array.isArray(f.quota) && f.quota.length);
  if (!withQuota) return; // not all fixtures have quota; skip cleanly if none
  const vm = fv.buildViewModel(withQuota);
  assert.ok(vm.quota && /quota/.test(vm.quota.text));
  assert.equal(typeof vm.quota.critical, "boolean");
});

test("shouldAlert: fires once per distinct alert, resets when cleared, re-fires on return", () => {
  const alertFrame = frames.find((f) => f.scene === "alert");
  assert.ok(alertFrame);

  const first = fv.shouldAlert(alertFrame, "");
  assert.equal(first.alert, true);
  assert.ok(first.key.length > 0);

  // same standing alert → no double-notify
  assert.equal(fv.shouldAlert(alertFrame, first.key).alert, false);

  // non-alert scene → no alert, key resets to ""
  const nonAlert = frames.find((f) => f.scene !== "alert") || { scene: "focus", primary: {} };
  const cleared = fv.shouldAlert(nonAlert, first.key);
  assert.equal(cleared.alert, false);
  assert.equal(cleared.key, "");

  // alert returns after clearing → fires again
  assert.equal(fv.shouldAlert(alertFrame, cleared.key).alert, true);
});

test("shouldAlert: a genuinely different alert fires even back-to-back", () => {
  const a = { scene: "alert", primary: { provider: "claude", status: "WAITING", project: "p1", account: "main" } };
  const b = { scene: "alert", primary: { provider: "codex", status: "ERROR", project: "p2", account: "main" } };
  const ra = fv.shouldAlert(a, "");
  assert.equal(ra.alert, true);
  const rb = fv.shouldAlert(b, ra.key);
  assert.equal(rb.alert, true);
  assert.notEqual(ra.key, rb.key);
});

test("shouldAlert: SAME identity but a CHANGED task re-fires (task is part of the key)", () => {
  // regression for the alertKey-omits-task bug: two alerts identical except primary.task.
  const base = { provider: "claude", status: "WAITING", project: "p1", account: "main" };
  const t1 = { scene: "alert", primary: { ...base, task: "waiting-on-review" } };
  const t2 = { scene: "alert", primary: { ...base, task: "waiting-on-approval" } };
  const r1 = fv.shouldAlert(t1, "");
  assert.equal(r1.alert, true);
  const r2 = fv.shouldAlert(t2, r1.key);   // back-to-back, only task differs
  assert.equal(r2.alert, true, "a changed task on the same agent must re-fire");
  assert.notEqual(r1.key, r2.key);
});

test("buildViewModel: quota surfaces the HIGHER-risk window (week vs w5), never hides it", () => {
  // w5 calm (72%) but weekly cap nearly full (95%) → must read 95% + critical, not 72%.
  const vm = fv.buildViewModel({ quota: [{ provider: "Codex", account: "main", w5: 0.72, week: 0.95 }] });
  assert.ok(vm.quota, "quota row should render");
  assert.match(vm.quota.text, /95%/);
  assert.equal(vm.quota.critical, true);
  // reverse: w5 hot, week unknown → use w5.
  const vm2 = fv.buildViewModel({ quota: [{ provider: "Codex", w5: 0.93 }] });
  assert.match(vm2.quota.text, /93%/);
  assert.equal(vm2.quota.critical, true);
  // neither window present → no quota line (not a phantom 0%).
  const vm3 = fv.buildViewModel({ quota: [{ provider: "Codex", account: "main" }] });
  assert.equal(vm3.quota, null);
});

test("buildViewModel: fleetMore recounts rows dropped by the local 3-row cap", () => {
  // server sent 5 rows with fleet_more=2 (agents beyond ITS 5-cap); the widget shows 3, so the
  // "+N more" must be 2 (server) + 2 (rows 4-5 dropped locally) = 4.
  const fleet = ["a", "b", "c", "d", "e"].map((p, i) => ({ provider: p, count: i + 1, status: "CODING" }));
  const vm = fv.buildViewModel({ fleet, fleet_more: 2 });
  assert.equal(vm.fleetRows.length, 3);
  assert.equal(vm.fleetMore, 4);
  // exactly 3 rows, no server overflow → no "+more".
  assert.equal(fv.buildViewModel({ fleet: fleet.slice(0, 3) }).fleetMore, 0);
});

test("classifyHttpStatus: auth failures force re-pair (never cache); transient → cache", () => {
  for (const code of [401, 403, 404]) {
    const c = fv.classifyHttpStatus(code);
    assert.deepEqual(c, { ok: false, pairingRequired: true, useCache: false }, `HTTP ${code} must require re-pair`);
  }
  for (const code of [429, 500, 502, 503]) {
    assert.equal(fv.classifyHttpStatus(code).useCache, true, `HTTP ${code} should fall back to cache`);
    assert.equal(fv.classifyHttpStatus(code).pairingRequired, false);
  }
  // healthy + transport-failure sentinel (0/null) → ok (caller decides).
  assert.equal(fv.classifyHttpStatus(200).ok, true);
  assert.equal(fv.classifyHttpStatus(0).ok, true);
  assert.equal(fv.classifyHttpStatus(null).ok, true);
  // an odd 4xx (e.g. 400) is shown as an error, not masked by cache and not a re-pair.
  assert.deepEqual(fv.classifyHttpStatus(400), { ok: false, pairingRequired: false, useCache: false });
});
