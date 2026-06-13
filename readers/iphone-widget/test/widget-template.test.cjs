// Static checks for the public Scriptable widget template.
// The live on-phone script may contain private values, but the repo template must
// stay pasteable as one file and must never carry a real relay URL or token.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const WIDGET = path.join(__dirname, "..", "agentlamp-widget.js");
const src = fs.readFileSync(WIDGET, "utf8");

test("widget template is one-file Scriptable paste, not a hidden multi-file setup", () => {
  assert.match(src, /single-file/i);
  assert.doesNotMatch(src, /importModule\(["']frame-view["']\)/);
});

test("widget template keeps all private values as placeholders", () => {
  assert.match(src, /const RELAY_URL = "\{RELAY_URL\}"/);
  assert.match(src, /const DEVICE_ID = "\{DEVICE_ID\}"/);
  assert.match(src, /const TOKEN\s+= "\{DEVICE_TOKEN\}"/);
  assert.doesNotMatch(src, /agentlamp-relay\.[^"']+\.workers\.dev/);
  assert.doesNotMatch(src, /K-[A-Za-z0-9_-]{20,}/);
});

test("widget template contains the current quota presentation features", () => {
  assert.match(src, /planLabel/);
  assert.match(src, /fmtReset/);
  assert.match(src, /barImage/);
  assert.match(src, /剩余|REMAINING/);
  assert.match(src, /\[2\]}×/);
  assert.match(src, /5时/);
  assert.match(src, /7天/);
  assert.match(src, /HULU/);
});
