// AgentLamp — iPhone widget (single-file Scriptable template).
// =============================================================================
// Paste into ONE Scriptable script named "AgentLamp".
//
// Public-repo rule: keep the constants below as placeholders in git. Fill real
// values only on the phone from your local ~/.config/agentlamp/relay-deploy.txt.
//
// Shows live agent status plus real Claude/Codex 5h + 7d quota REMAINING:
//   - % = quota left, not used
//   - green/violet provider bars are healthy
//   - amber <=30% left, red <=10% left
// Best home-screen size: Scriptable large. Medium and small are supported.
// =============================================================================

const RELAY_URL = "{RELAY_URL}";
const DEVICE_ID = "{DEVICE_ID}";
const TOKEN = "{DEVICE_TOKEN}";

const KC_CACHE = "agentlamp_last_frame";
const DEFAULT_WIDGET_FAMILY = "large";

// ---- palette (white theme, unified tone) ------------------------------------
const BG_TOP = "#FFFFFF", BG_BOT = "#EAEEF6";
const C_NAME = "#15171C", C_SUB = "#6B7280", C_DIM = "#A0A8B5", C_TRACK = "#E7EAF1";
const STATUS_COLOR = {
  CODING:"#7C6CF0", THINKING:"#8B7CF0", READING:"#0EA5E9", TESTING:"#0EA371",
  WAITING:"#D97706", DONE:"#0EA371", ERROR:"#E11D48", IDLE:"#2563EB",
  OFFLINE:"#94A3B8", STALE:"#94A3B8", UNKNOWN:"#94A3B8",
};
const ACCENT = {
  purple:"#7C6CF0", cyan:"#0EA5E9", green:"#0EA371", yellow:"#D97706",
  red:"#E11D48", blue:"#2563EB", white:"#6B7280", muted:"#94A3B8",
};
function statusColor(s){ return STATUS_COLOR[String(s||"").toUpperCase()] || "#2563EB"; }
function accentHex(f){ return (f && ACCENT[f.accent]) || statusColor(f && f.primary && f.primary.status); }

const PROVIDER_GRAD = {
  CLAUDE: ["#B79CFF", "#7C6CF0"],
  CODEX:  ["#46E0A8", "#0EA371"],
  MANUAL: ["#AEB7C7", "#64748B"],
};
const PROVIDER_INK = { CLAUDE:"#7C6CF0", CODEX:"#0E9D6C", MANUAL:"#64748B" };
function providerColor(p){ return PROVIDER_INK[String(p||"").toUpperCase()] || "#64748B"; }
function barStops(remaining, provider){
  if (remaining != null){
    if (remaining <= 0.1) return ["#FB7185", "#E11D48"];
    if (remaining <= 0.3) return ["#FBBF24", "#F59E0B"];
  }
  return PROVIDER_GRAD[String(provider||"").toUpperCase()] || PROVIDER_GRAD.MANUAL;
}
function pctInk(remaining, provider){
  if (remaining != null){
    if (remaining <= 0.1) return "#E11D48";
    if (remaining <= 0.3) return "#D97706";
  }
  return providerColor(provider);
}

// ---- Chinese labels ----------------------------------------------------------
const STATUS_ZH = {
  CODING:"编码中", THINKING:"思考中", READING:"阅读中", TESTING:"测试中",
  WAITING:"等待中", DONE:"已完成", ERROR:"出错", IDLE:"空闲",
  OFFLINE:"离线", STALE:"已离线", UNKNOWN:"未知",
};
const HEADLINE_ZH = {
  "ACTION REQUIRED":"需要处理", "ALL CLEAR":"一切正常", "PAIRING REQUIRED":"需要配对",
  "OFFLINE":"离线", "AGENTLAMP":"智能体面板", "SLEEP":"休眠中", "ALERT":"警报",
  "FOCUS":"专注工作", "DIAGNOSTICS":"诊断中", "QUOTA":"额度", "AGENTS":"智能体",
  "STALE":"已离线", "DONE":"已完成", "WAITING":"等待中",
};
const TASK_ZH = {
  implementing:"实现中", planning:"规划中", reading:"阅读中", testing:"测试中",
  thinking:"思考中", debugging:"调试中", reviewing:"审查中", writing:"编写中",
  coding:"编码中", building:"构建中", searching:"搜索中", waiting:"等待中",
};
const ACCOUNT_ZH = { main:"主账号", work:"工作", personal:"个人" };
function zh(map, v, fallback){
  const k = String(v ?? "").trim();
  return map[k] || map[k.toUpperCase()] || map[k.toLowerCase()] || (fallback != null ? fallback : k);
}
function hasCJK(s){ return /[一-鿿]/.test(String(s||"")); }

// ---- view model -------------------------------------------------------------
function buildViewModel(frame){
  frame = frame || {};
  const p = frame.primary || {};
  const fleet = Array.isArray(frame.fleet) ? frame.fleet : [];
  const num = (x) => (typeof x === "number" && isFinite(x)) ? x : null;
  const quotas = (Array.isArray(frame.quota) ? frame.quota : []).map(q => ({
    provider: String(q.provider || "").trim(),
    plan: String(q.plan || "").trim(),
    w5: num(q.w5),
    week: num(q.week),
    w5Reset: num(q.w5_reset),
    weekReset: num(q.week_reset),
    estimated: Boolean(q.estimated),
  })).filter(q => q.provider && (q.w5 != null || q.week != null));

  const rawScene = frame.headline || "AGENTLAMP";
  const acct = (p.account && p.account !== "main") ? zh(ACCOUNT_ZH, p.account) : "";
  const tsk = (p.task && !["unknown", "idle"].includes(String(p.task).toLowerCase()))
    ? zh(TASK_ZH, p.task)
    : "";
  const subParts = [p.provider, acct, tsk].filter(Boolean);
  return {
    scene: zh(HEADLINE_ZH, rawScene),
    accentHex: accentHex(frame),
    name: (frame.brand && String(frame.brand).trim()) || "HULU",
    status: String(p.status || "").toUpperCase() || "—",
    statusLabel: p.status ? zh(STATUS_ZH, p.status) : "—",
    statusColor: statusColor(p.status),
    primaryProvider: String(p.provider || "").trim(),
    sub: subParts.join("  ·  "),
    quotas,
    quotaEstimated: quotas.some(q => q.estimated),
    fleetRows: fleet.map(r => ({
      project: r.provider,
      count: r.count,
      status: String(r.status||"").toUpperCase(),
      statusLabel: zh(STATUS_ZH, r.status),
      color: statusColor(r.status),
    })),
    fleetMore: frame.fleet_more || 0,
  };
}

// ---- HTTP + cache -----------------------------------------------------------
function classifyHttpStatus(code){
  if (code == null || code < 400) return { ok:true, pairingRequired:false, useCache:false };
  if (code === 401 || code === 403 || code === 404) return { ok:false, pairingRequired:true, useCache:false };
  if (code === 429 || code >= 500) return { ok:false, pairingRequired:false, useCache:true };
  return { ok:false, pairingRequired:false, useCache:false };
}
async function getFrame(){
  const r = new Request(`${RELAY_URL}/api/v1/device/${DEVICE_ID}/frame`);
  r.method = "GET";
  r.headers = { "Authorization": `Bearer ${TOKEN}`, "X-Frame-Schema-Version": "1" };
  r.timeoutInterval = 10;
  const j = await r.loadJSON();
  return { json:j, status: r.response ? r.response.statusCode : 0 };
}
function loadCached(){
  if (!Keychain.contains(KC_CACHE)) return null;
  try {
    const c = JSON.parse(Keychain.get(KC_CACHE));
    const ageMin = Math.round((Date.now()-c.at)/60000);
    c.frame.headline = `离线 · ${ageMin} 分钟前`;
    return c.frame;
  } catch(_){ return null; }
}

// ---- drawing helpers --------------------------------------------------------
function text(stack, s, size, color, opts={}){
  const t = stack.addText(String(s ?? ""));
  t.font = opts.font || (opts.bold ? Font.boldSystemFont(size) : Font.systemFont(size));
  t.textColor = new Color(color);
  if (opts.lines) t.lineLimit = opts.lines;
  if (opts.scale) t.minimumScaleFactor = opts.scale;
  return t;
}
function pill(parent, label, hex, opts={}){
  const fs = opts.size || 12;
  const p = parent.addStack();
  p.backgroundColor = new Color(hex, 0.16);
  p.cornerRadius = opts.radius || 7;
  p.setPadding(opts.py || 4, opts.px || 9, opts.py || 4, opts.px || 9);
  p.centerAlignContent();
  text(p, "●", Math.max(7, fs - 5), hex);
  p.addSpacer(5);
  text(p, label, fs, hex, { bold:true });
}
function chip(parent, label, hex){
  const c = parent.addStack();
  c.backgroundColor = new Color(hex, 0.16);
  c.cornerRadius = 5;
  c.setPadding(1, 6, 1, 6);
  c.centerAlignContent();
  text(c, label, 9, hex, { bold:true });
}
function planLabel(p){
  p = String(p || "").trim().toLowerCase();
  if (!p || p === "unknown") return "";
  const BASE = { max:"Max", pro:"Pro", plus:"Plus", team:"Team", enterprise:"Enterprise", free:"Free" };
  const m = p.match(/^([a-z]+)[_-](\d+)x$/);
  if (m){
    const b = BASE[m[1]] || (m[1].charAt(0).toUpperCase() + m[1].slice(1));
    return `${b} ${m[2]}×`;
  }
  return BASE[p] || (p.charAt(0).toUpperCase() + p.slice(1));
}
function fmtReset(epoch){
  if (epoch == null) return null;
  const d = new Date(epoch * 1000), now = new Date();
  const hh = String(d.getHours()).padStart(2, "0"), mm = String(d.getMinutes()).padStart(2, "0");
  const sameDay = (a, b) =>
    a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  if (sameDay(d, now)) return `今天 ${hh}:${mm}`;
  if (sameDay(d, new Date(now.getTime() + 86400000))) return `明天 ${hh}:${mm}`;
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}
function _rgb(h){
  h = h.replace("#","");
  return [parseInt(h.slice(0,2),16), parseInt(h.slice(2,4),16), parseInt(h.slice(4,6),16)];
}
function _hex(rgb){
  return "#" + rgb.map(v => Math.max(0,Math.min(255,Math.round(v))).toString(16).padStart(2,"0")).join("");
}
function _capInset(x, fw, R){
  let d = null;
  if (x < R) d = R - x;
  else if (x > fw - R) d = x - (fw - R);
  if (d == null) return 0;
  return R - Math.sqrt(Math.max(0, R*R - d*d));
}
function barImage(ratio, stops, w, h){
  const s = 3, W = Math.round(w * s), H = Math.round(h * s), R = H / 2;
  const dc = new DrawContext();
  dc.size = new Size(W, H);
  dc.opaque = false;
  dc.respectScreenScale = false;
  dc.setFillColor(new Color(C_TRACK));
  const tp = new Path(); tp.addRoundedRect(new Rect(0, 0, W, H), R, R);
  dc.addPath(tp); dc.fillPath();
  if (ratio != null && ratio > 0){
    const fw = Math.max(H, Math.round(W * Math.min(1, ratio)));
    const c0 = _rgb(stops[0]), c1 = _rgb(stops[1]);
    for (let x = 0; x < fw; x += 1){
      const t = x / Math.max(1, fw - 1);
      const col = [c0[0]+(c1[0]-c0[0])*t, c0[1]+(c1[1]-c0[1])*t, c0[2]+(c1[2]-c0[2])*t];
      const yo = _capInset(x + 0.5, fw, R);
      dc.setFillColor(new Color(_hex(col)));
      dc.fillRect(new Rect(x, yo, 1.4, H - 2*yo));
    }
  }
  return dc.getImage();
}
function windowLine(parent, label, ratio, provider, barW, resetEpoch){
  const H = 9;
  // Cloud sends USED ratio. The widget displays REMAINING ratio.
  const remaining = ratio == null ? null : Math.max(0, Math.min(1, 1 - ratio));
  const row = parent.addStack(); row.layoutHorizontally(); row.centerAlignContent();
  const lab = row.addStack(); lab.size = new Size(24, 15); lab.centerAlignContent();
  text(lab, label, 10, C_DIM, { font: Font.semiboldSystemFont(10) });
  row.addSpacer(6);
  const img = row.addImage(barImage(remaining, barStops(remaining, provider), barW, H));
  img.imageSize = new Size(barW, H);
  row.addSpacer(7);
  const pc = row.addStack(); pc.size = new Size(36, 15); pc.centerAlignContent();
  text(pc, remaining == null ? "—" : `${Math.round(remaining*100)}%`, 12, pctInk(remaining, provider), { bold:true });
  const rs = fmtReset(resetEpoch);
  if (rs){ row.addSpacer(); text(row, "↻ " + rs, 9, C_DIM, { lines:1, scale:0.75 }); }
}
function providerQuotaRow(parent, q, labW, barW, showReset, resetBelow=false){
  const row = parent.addStack(); row.layoutHorizontally(); row.topAlignContent();
  const left = row.addStack(); left.layoutVertically(); left.size = new Size(labW, 0);
  text(left, q.provider.toUpperCase(), 13, providerColor(q.provider), { bold:true, lines:1, scale:0.6 });
  if (q.plan){
    left.addSpacer(4);
    const cw = left.addStack(); cw.layoutHorizontally();
    chip(cw, planLabel(q.plan), providerColor(q.provider));
    cw.addSpacer();
  }
  row.addSpacer(10);
  const right = row.addStack(); right.layoutVertically();
  windowLine(right, "5时", q.w5, q.provider, barW, showReset && !resetBelow ? q.w5Reset : null);
  right.addSpacer(6);
  windowLine(right, "7天", q.week, q.provider, barW, showReset && !resetBelow ? q.weekReset : null);
  if (showReset && resetBelow){
    const resets = [
      q.w5Reset ? `5时 ${fmtReset(q.w5Reset)}` : null,
      q.weekReset ? `7天 ${fmtReset(q.weekReset)}` : null,
    ].filter(Boolean).join("  ·  ");
    if (resets){
      right.addSpacer(6);
      text(right, "↻ " + resets, 9, C_DIM, { lines:1, scale:0.75 });
    }
  }
}
function providerQuotaStacked(parent, q, barW){
  const h = parent.addStack(); h.layoutHorizontally(); h.centerAlignContent();
  text(h, q.provider.toUpperCase(), 11, providerColor(q.provider), { font: Font.semiboldSystemFont(11) });
  if (q.plan) { h.addSpacer(4); chip(h, planLabel(q.plan), providerColor(q.provider)); }
  parent.addSpacer(4);
  windowLine(parent, "5时", q.w5, q.provider, barW, null);
  parent.addSpacer(4);
  windowLine(parent, "7天", q.week, q.provider, barW, null);
}

// ---- render -----------------------------------------------------------------
function render(vm, footer, family){
  const small = family === "small";
  const large = family === "large";
  const w = new ListWidget();
  const g = new LinearGradient();
  g.colors = [new Color(BG_TOP), new Color(BG_BOT)];
  g.locations = [0, 1];
  w.backgroundGradient = g;
  w.setPadding(small ? 13 : (large ? 20 : 15), small ? 14 : (large ? 20 : 17),
               small ? 13 : (large ? 20 : 15), small ? 14 : (large ? 20 : 17));

  const head = w.addStack();
  head.layoutHorizontally();
  head.centerAlignContent();
  const left = head.addStack();
  left.layoutVertically();
  const sceneSize = large ? 10 : 9;
  text(left, hasCJK(vm.scene) ? vm.scene : vm.scene.toUpperCase(), sceneSize, vm.accentHex, {
    font: Font.semiboldSystemFont(sceneSize),
  });
  left.addSpacer(2);
  text(left, vm.name, small ? 17 : (large ? 28 : 20), C_NAME, { bold:true, lines:1, scale:0.6 });
  head.addSpacer();
  pill(head, vm.statusLabel, vm.statusColor, large ? { size:14, px:11, py:5, radius:8 } : {});

  if (large && vm.sub){
    w.addSpacer(5);
    text(w, vm.sub, 12, C_SUB, { lines:1, scale:0.8 });
  }

  w.addSpacer(small ? 8 : (large ? 18 : 11));
  if (small){
    const q = vm.quotas.find(x => x.provider.toUpperCase() === vm.primaryProvider.toUpperCase()) || vm.quotas[0];
    if (q) providerQuotaStacked(w, q, 78);
  } else {
    const barW = large ? 116 : 80;
    const labW = large ? 82 : 70;
    const provs = vm.quotas.slice(0, large ? 3 : 2);
    provs.forEach((q, i) => {
      if (i) w.addSpacer(large ? 18 : 11);
      providerQuotaRow(w, q, labW, barW, true, large);
    });
    if (!provs.length) text(w, "暂无额度数据", 11, C_DIM);
  }

  const cap = small ? 0 : (large ? 5 : 0);
  if (cap > 0 && vm.fleetRows.length){
    w.addSpacer(16);
    text(w, "会话", 9, C_DIM, { font: Font.semiboldSystemFont(9) });
    w.addSpacer(5);
    for (const row of vm.fleetRows.slice(0, cap)){
      const fr = w.addStack();
      fr.layoutHorizontally();
      fr.centerAlignContent();
      text(fr, "●", 8, row.color);
      fr.addSpacer(6);
      text(fr, row.project, 12, "#374151", { lines:1, scale:0.7 });
      if (row.count > 1){
        fr.addSpacer(6);
        text(fr, `×${row.count}`, 11, C_SUB, { font: Font.semiboldSystemFont(11) });
      }
      fr.addSpacer();
      text(fr, row.statusLabel, 11, row.color, { font: Font.semiboldSystemFont(11) });
    }
    const extra = vm.fleetMore + Math.max(0, vm.fleetRows.length - cap);
    if (extra > 0) { w.addSpacer(3); text(w, `还有 ${extra} 个`, 10, C_DIM); }
  }

  w.addSpacer();
  const ft = w.addStack();
  ft.layoutHorizontally();
  ft.centerAlignContent();
  text(ft, footer.text, large ? 10 : 9, footer.color);
  ft.addSpacer();
  if (vm.quotaEstimated && vm.quotas.length) text(ft, "≈ 估算", 9, C_DIM);

  w.refreshAfterDate = new Date(Date.now() + 5*60*1000);
  return w;
}

// ---- main -------------------------------------------------------------------
let frame = null, footerState = "ok";
try {
  const { json, status } = await getFrame();
  const cls = classifyHttpStatus(status);
  if (cls.ok){
    frame = json;
    Keychain.set(KC_CACHE, JSON.stringify({ frame, at: Date.now() }));
  } else if (cls.pairingRequired){
    footerState = "pairing";
    if (Keychain.contains(KC_CACHE)) Keychain.remove(KC_CACHE);
    frame = {
      scene:"alert", accent:"red", headline:"PAIRING REQUIRED",
      primary:{ provider:"AgentLamp", account:"", status:"ERROR", project:"需重新配对", task:`HTTP ${status}` },
      fleet:[], quota:[],
    };
  } else {
    footerState = "stale";
    frame = loadCached();
  }
} catch(e){
  footerState = "stale";
  frame = loadCached();
}
if (frame == null && footerState !== "pairing"){
  frame = {
    scene:"sleep", accent:"muted", headline:"OFFLINE",
    primary:{ provider:"—", account:"", status:"OFFLINE", project:"暂无数据", task:"" },
    fleet:[], quota:[],
  };
}

const stamp = new Date().toLocaleTimeString([], { hour:"2-digit", minute:"2-digit" });
const footer = {
  ok:{ text:`更新于 ${stamp}`, color:C_DIM },
  stale:{ text:`⚠︎ 已离线 · ${stamp}`, color:"#D97706" },
  pairing:{ text:`⚠︎ 需重新配对 · ${stamp}`, color:"#E11D48" },
}[footerState];

const family = (typeof config !== "undefined" && config.widgetFamily) ? config.widgetFamily : DEFAULT_WIDGET_FAMILY;
const widget = render(buildViewModel(frame), footer, family);
if (config.runsInWidget) Script.setWidget(widget);
else if (family === "large") widget.presentLarge();
else if (family === "small") widget.presentSmall();
else widget.presentMedium();
Script.complete();
