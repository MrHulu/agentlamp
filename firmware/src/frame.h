// AgentLamp frame model + JSON parser.
//
// A compact, fixed-size mirror of the schema v1 frame (docs/api/device_frame_api.md).
// We copy only the fields the firmware renders into bounded char buffers so the
// render path never touches the heap and a hostile/garbage body can't blow RAM.
// Unknown JSON fields are ignored (forward-compatible); unknown `v` is rejected.

#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>
#include "theme.h"

static constexpr size_t FRAME_MAX_BYTES = 2048;   // hard cap from the contract
static constexpr int    FRAME_SCHEMA_V  = 1;      // max supported schema version

// small bounded string copies — provider/account/project/task are short labels.
// `provider` holds the CLEAN project label (server no longer bakes " xN" in). It must
// hold the server's MAX label (sanitize.py ALIAS_MAX_LEN = 40) so a long-but-valid label
// reaches drawFit intact and is shrunk/ellipsized cleanly — a buffer shorter than 41 would
// HARD-truncate (e.g. mid-word, no ".." marker) before the renderer ever sees it.
struct FleetRow {
  char    provider[41];   // ALIAS_MAX_LEN (40) + NUL
  uint8_t count;
  Status  status;
};

struct Frame {
  bool    valid = false;
  int     v = 0;
  Scene   scene = Scene::UNKNOWN;
  Accent  accent = Accent::MUTED;

  char    headline[40];

  // primary session
  char    provider[16];
  char    account[16];
  char    project[41];   // ALIAS_MAX_LEN (40) + NUL — drawFit ellipsizes; never hard-cut
  char    task[24];
  Status  status = Status::UNKNOWN;

  // fleet (cap 6 per contract)
  FleetRow fleet[6];
  uint8_t  fleetCount = 0;
  int      fleetMore = 0;

  // quota (cap 2). w5/week are 0..1 fractions; <0 means "window absent".
  struct QuotaRow {
    char  provider[16];
    char  account[16];
    float w5;
    float week;
    int   confidence;
    bool  estimated;
  } quota[2];
  uint8_t quotaCount = 0;

  unsigned long ttl = 5;       // seconds
  unsigned long seq = 0;
  unsigned long serverTime = 0;
};

// safe bounded copy of a JSON string field into a fixed buffer
static inline void copyField(char* dst, size_t cap, JsonVariantConst v) {
  const char* s = v.is<const char*>() ? v.as<const char*>() : nullptr;
  if (!s) { dst[0] = '\0'; return; }
  strlcpy(dst, s, cap);
}

// Parse a frame body into `out`. Returns false on any rejection (bad size,
// bad JSON, unsupported `v`). Does NOT print the token or full body.
//
// `len` is the raw body length; caller has already enforced the 2 KB cap, but
// we re-check defensively.
static inline bool parseFrame(const char* body, size_t len, Frame& out) {
  out.valid = false;
  if (len == 0 || len > FRAME_MAX_BYTES) return false;

  // Bounded document. v7 JsonDocument grows on the heap but we keep input small
  // (≤2 KB) and parse outside the render loop, then copy into the static Frame.
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, body, len);
  if (err) return false;

  JsonObjectConst root = doc.as<JsonObjectConst>();
  if (root.isNull()) return false;

  // reject unknown schema version (ignore unknown fields, but not unknown v)
  int v = root["v"] | 0;
  if (v != FRAME_SCHEMA_V) return false;
  out.v = v;

  out.scene  = parseScene(root["scene"]  | "");
  out.accent = parseAccent(root["accent"] | "");
  copyField(out.headline, sizeof(out.headline), root["headline"]);

  JsonObjectConst pri = root["primary"];
  copyField(out.provider, sizeof(out.provider), pri["provider"]);
  copyField(out.account,  sizeof(out.account),  pri["account"]);
  copyField(out.project,  sizeof(out.project),  pri["project"]);
  copyField(out.task,     sizeof(out.task),     pri["task"]);
  out.status = parseStatus(pri["status"] | "");

  // fleet (cap 6)
  out.fleetCount = 0;
  JsonArrayConst fleet = root["fleet"];
  for (JsonObjectConst row : fleet) {
    if (out.fleetCount >= 6) break;
    FleetRow& fr = out.fleet[out.fleetCount];
    copyField(fr.provider, sizeof(fr.provider), row["provider"]);
    fr.count  = (uint8_t)(row["count"] | 0);
    fr.status = parseStatus(row["status"] | "");
    out.fleetCount++;
  }
  out.fleetMore = root["fleet_more"] | 0;

  // quota (cap 2)
  out.quotaCount = 0;
  JsonArrayConst quota = root["quota"];
  for (JsonObjectConst row : quota) {
    if (out.quotaCount >= 2) break;
    auto& q = out.quota[out.quotaCount];
    copyField(q.provider, sizeof(q.provider), row["provider"]);
    copyField(q.account,  sizeof(q.account),  row["account"]);
    // a window with no data is OMITTED (never null) -> -1 sentinel
    q.w5   = row["w5"].is<float>()   ? (float)row["w5"]   : -1.0f;
    q.week = row["week"].is<float>() ? (float)row["week"] : -1.0f;
    q.confidence = row["confidence"] | 0;
    q.estimated  = row["estimated"]  | false;
    out.quotaCount++;
  }

  out.ttl        = root["ttl"] | 5UL;
  out.seq        = root["seq"] | 0UL;
  out.serverTime = root["server_time"] | 0UL;

  if (out.ttl == 0) out.ttl = 5;   // guard against div/zero-ttl nonsense

  out.valid = true;
  return true;
}
