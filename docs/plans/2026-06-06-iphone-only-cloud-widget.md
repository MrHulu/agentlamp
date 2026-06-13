# AgentLamp · iPhone 组件「云端跨网读取」执行计划 v2（手机 Only）

> **日期**：2026-06-06 · **作者**：ai-center 秘书 · **状态**：草案，待 Boss 批准
> **范围**：只做 iPhone **桌面/锁屏组件（非 App）**，从**云端**读、**不要求同一网段**。
> **本轮明确放弃**：❌ ESP32 实体灯 · ❌ 电脑端 reader（Boss 2026-06-06「不要考虑 esp32，仅做手机的」）。
> **一句话**：砍掉实体灯后，原计划最重的「双推改造」**整块消失**——daemon 直接切 relay 模式即可，剩下只有 **2 条命令 + 1 个手机脚本**，零 dev-loop 代码活。

---

## 0. 一页纸结论（TL;DR）

| 维度 | 现状（实测） | 手机-only 后怎么变 |
|---|---|---|
| 云端 relay（CF Worker + DO + KV） | 已部署、全球可达，`/healthz` 200 | ✅ 不动，直接复用 |
| 读取端点 `GET /device/:id/frame`（Bearer） | 鉴权正常（统一 401） | ✅ 手机组件就连它 |
| 保存的 device token | 实测 `bad_token`，已不在 live registry | ⚠️ 重新 enroll（1 条命令） |
| collector 模式 | **LOCAL**（喂 USB 实体灯），relay 代码已具备但没启用 | 🔁 **直接切 relay**（1 条命令 + 重启 daemon） |
| ~~双推改造（TASK-019）~~ | ~~原计划最重的代码活，半天~~ | ✅ **取消**——不要灯了就不用「同时喂灯 + 推云」 |
| iPhone 组件 | 还没有 | 🆕 Scriptable 脚本（~60 行 JS，免 App、免越狱） |

**手机-only 后真正要做的，就 3 步（按依赖）：**

1. **Step 1（token，P0，现在就能跑）**：重新 enroll device token——additive，1 条 admin 命令，不需要浏览器。
2. **Step 2（数据，P0）**：把这台 Mac 的 collector 从 LOCAL **切到 relay 模式**——1 条 `agentlamp enroll` + 重启 daemon。云里立刻有真实数据。（代码已存在，非新开发。）
3. **Step 3（手机，P1）**：iPhone 装 Scriptable，粘附录 A 脚本，主屏/锁屏加组件。

> **诚实边界**：iOS 桌面组件刷新被系统节流（约 5–15 分钟一次，不是实体灯那种 4 秒实时）。要「秒级感知」就加可选的 Pushcut 推送（§5 Step 4）；组件本身定位「扫一眼概览」。这是 iOS 平台限制，不是能力不足。

---

## 1. 现状基线（全部实测，非记忆）

### 1.1 云端 relay
- 形态：Cloudflare Worker + Durable Object（`RelayDO` 单例，强一致）+ KV（配置/缓存）。
- 读取路由（`src/cloud/src/index.ts`）：
  - `GET /api/v1/device/:id/frame` —— **只读拉帧**，`Authorization: Bearer <token>` + `X-Frame-Schema-Version: 1`，边缘限流 20/min/设备。**这就是手机组件唯一要调的端点。**
  - `POST /api/v1/collectors/:id/events` —— 签名摄取（HMAC + ±300s 时窗 + nonce 防重放 + 幂等 + 云端 sanitize）。**P1 已落地**（13 安全测试 + 160 server 测试绿）。
  - `POST /admin/devices/:id/{revoke,enroll}` —— 常量时间 bearer 闸 + 防重放。**加机器/换 token 只要一条 authed POST，不用 redeploy。**
- 实测：`GET /healthz` → `200 {"ok":true,"service":"agentlamp-relay","v":1}`。
- 实测：`GET /frame`（带保存的 token）→ `401 {"error":"bad_token"}` ⇒ 该 token 已不在 live registry，需重 enroll。

### 1.2 本机 collector（这台 Mac）
- 进程：`com.hulu.agentlamp.daemon` + `com.hulu.agentlamp.server`（均运行中）。
- 模式：**LOCAL**（daemon 把事件 POST 到 loopback server，再经 USB/LAN 喂实体灯）。
- 推云能力：**代码已具备**（`src/collector/relaypost.py` 签名推送 + `daemon.py` 认 `config.RELAY_MODE` + `agentlamp enroll` 一键配置 + keyring 存 secret）。只是没启用。
- 关键：`daemon.py` 是 `if config.RELAY_MODE: _post_relay() else: _post_local()` —— 二选一。**手机-only 下，直接选 relay 这一支即可，无需改成双推。**

### 1.3 实体灯（ESP32）—— 本轮范围外
- 实体灯当前靠 USB/LAN 读本地 server，固件还够不到云（HTTPS/CA/NTP = 老 TASK-007 P4，未做）。
- **本轮 Boss 明确不做实体灯** → daemon 切 relay 后，实体灯会停止收到数据（变静止）。这是预期内、可接受的——不是 bug。日后若要重新点亮实体灯，再回到「双推」方案即可（已在旧计划 `2026-06-05-phone-desktop-cloud-readers.md` 存档）。

---

## 2. 目标 / 非目标 / 红线

**目标**
- iPhone 上一个**桌面 + 锁屏组件**显示 agent 概览（fleet/focus/quota/alert），**非 App**。
- 数据走**云**，手机**不需要和 Mac 在同一网段**。

**非目标（本轮）**
- ❌ 不做原生 App（Boss 明确「不要做成 App」）。
- ❌ 不做实体灯 / 不做双推 / 不碰固件（Boss「不考虑 ESP32」）。
- ❌ 不做电脑端 reader（Boss「仅做手机的」）。
- ❌ 不引入第二套后端、不做公开注册/多租户（单 owner）。

**红线（agentlamp 发布约束 + 本仓规则）**
- 🚨 **token 绝不进 URL / QR / 仓库**。组件把 token 放在**设备本地脚本 / Keychain**，传输只走 `Authorization` header。
- 🚨 **仓库已公开**（github.com/MrHulu/agentlamp）：本计划书与任何客户端代码**只用占位符** `{RELAY_URL}` / `{DEVICE_ID}` / `{DEVICE_TOKEN}`；真值只在 `~/.config/agentlamp/relay-deploy.txt`。
  - ⚠️ `*.workers.dev` 默认子域名含 CF account id，而该数字**等于 Boss 私人 QQ 号** → 子域名本身敏感，不写进任何会进公开仓库的文件；优先绑自定义域。
- 🚨 提交作者邮箱红线：公开仓库提交前作者邮箱不得是私人 QQ 邮箱。
- 🚨 未经 Boss 批准不 commit / push；秘书不在下属项目写实现代码（本计划书=规格）。

---

## 3. 目标架构（手机-only，极简）

```
   本机 Mac                          Cloudflare 边缘（全球）
  ┌───────────┐   签名推送          ┌──────────────────────────┐
  │ collector │ ──(relay 模式)─────▶│ Worker → RelayDO(单例)    │
  │ daemon    │   POST /events      │ HMAC验/防重放/registry/   │
  │ (hooks→   │                     │ 物化 frame/审计 + KV       │
  │  事件队列) │                     └────────────┬─────────────┘
  └───────────┘                                  │ GET /frame (Bearer)
                                                 ▼
                                       ┌──────────────────┐
                                       │ iPhone 组件        │
                                       │ Scriptable        │
                                       │ 5–15min 刷新       │
                                       └──────────────────┘
                                       + (可选) Pushcut 即时告警
```

**核心点**：手机组件是 relay `/frame` 的**只读客户端**。后端不改一行；这台 Mac 的 collector 切到 relay 模式，云里就有数据。整条链上**没有需要新写的服务端代码**。

---

## 4. frame JSON 契约（手机组件消费规范）

`GET /api/v1/device/:id/frame`（Bearer + `X-Frame-Schema-Version: 1`）返回 **< 2KB** 紧凑帧。字段（源自 `src/cloud/src/frame.ts`，已核）：

```jsonc
{
  "v": 1,
  "device_id": "<id>",
  "scene": "fleet",             // boot|pairing|fleet|focus|quota|alert|offline|stale|sleep|diagnostics
  "headline": "AGENTS",
  "primary": {                  // 当前最该看的会话
    "provider": "Claude",       // Claude | Codex | Manual
    "account": "main",
    "status": "CODING",         // IDLE|THINKING|CODING|READING|TESTING|WAITING|DONE|ERROR|OFFLINE|STALE|UNKNOWN
    "project": "ai-center",
    "task": "<task_label>"
  },
  "fleet": [ { "provider": "ai-center", "count": 3, "status": "CODING" } ],  // 按 score 排序，最多 5 行
  "fleet_more": 2,
  "quota": [ { "provider": "Claude", "account": "main", "w5": 0.42, "week": 0.18, "confidence": 2, "estimated": false } ],
  "accent": "purple",
  "ttl": 5,                     // 建议刷新间隔（秒），客户端别低于它
  "seq": 42,                    // 内容变更才自增
  "server_time": 1733400000     // 服务端 unix 秒，客户端据此判 stale
}
```

**scene → headline**：boot=AGENTLAMP, pairing=PAIRING, fleet=AGENTS, focus=FOCUS, quota=QUOTA, **alert=ACTION REQUIRED**, offline=OFFLINE, stale=STALE, sleep=SLEEP, diagnostics=DIAGNOSTICS。

**accent → 颜色（深色底）**：purple `#A78BFA`(THINKING/CODING) · cyan `#22D3EE`(READING) · green `#34D399`(TESTING/DONE) · yellow `#FBBF24`(WAITING) · red `#F87171`(ERROR/配额危险) · blue `#60A5FA`(IDLE) · white `#E5E7EB`(STALE) · muted `#6B7280`(OFFLINE/UNKNOWN/sleep)。

**stale 判定（客户端）**：`now - server_time > 120s` → 显示「数据偏旧」；`> 600s` → 显示「离线」。

---

## 5. 工作分解（就 3 步 + 1 可选）

### Step 1 — 重新 enroll device token（P0，现在就能跑，不需要浏览器）
让手机能鉴权。走 admin 路由（无需 redeploy / 无需 wrangler login）：用 `relay-deploy.txt` 里的 `ADMIN_TOKEN`，对 `POST /admin/devices/{DEVICE_ID}/enroll` 提交（带 `X-ACO-Timestamp` ±300s + 一次性 `X-ACO-Nonce`，body `{"token":"<device_token>"}`）。命令见**附录 C-1**。
**验收**：`GET /frame` 由 401 变 **200**（即便此刻数据空、scene=sleep 也算通——证明鉴权+渲染管道通了）。

### Step 2 — collector 切 relay 模式（P0，1 条命令，**非新开发**）
> 手机-only 后这一步取代了原「双推」。代码已存在，只是没启用。

```sh
export AGENTLAMP_RELAY_HOST="<完整 https URL>"     # 见 relay-deploy.txt
printf '%s' "$ADMIN_TOKEN" | agentlamp enroll \
  --relay-host "$AGENTLAMP_RELAY_HOST" --collector-id <this-mac> --admin-token-stdin
launchctl kickstart -k gui/$(id -u)/com.hulu.agentlamp.daemon
agentlamp status && agentlamp doctor
```

- 效果：daemon 走 `_post_relay()`，每条事件签名推云。云端 `/frame` 开始返回真实 fleet/focus。
- 代价（已接受）：本地 server 不再收到事件 → 实体灯静止。**本轮不要灯，OK。**
- **验收**：云端 `GET /frame` 返回真实 fleet/focus；多刷几次 `seq` 随活动自增；断网→恢复后云帧自动跟上（无 tight loop）。

### Step 3 — iPhone Scriptable 组件（非 App）
> Scriptable = App Store 免费通用宿主，免越狱、免开发者账号，把 `Request.loadJSON()` 渲染成主屏/锁屏 `ListWidget`，2026 仍现役。Bearer 走 header → 合「token 不进 URL/QR」红线。

- **3a**：iPhone 装 Scriptable（App Store）。
- **3b**：新建脚本 `AgentLamp`，粘**附录 A** 代码；填 `{RELAY_URL}`/`{DEVICE_ID}`/`{DEVICE_TOKEN}`（建议开 Keychain 开关存 token）。
- **3c**：主屏长按 → 加「Scriptable」中号组件 → 选 `AgentLamp`；锁屏同理加小组件。
- **3d**：刷新——脚本里 `widget.refreshAfterDate` 设 5 分钟，**最终由 iOS 调度**（约 5–15 min）。够「概览」，不够「实时」。
- **验收**：组件显示当前 scene/primary/fleet；Mac 跑起真实 agent 后下次刷新能反映（手动点组件可强刷）；断网/401 显 OFFLINE 而不崩。

### Step 4 — 即时告警（可选，补 iOS 刷新慢）
- **Pushcut**（App Store 免费档）：建 Webhook；scene=`alert`（WAITING/ERROR）时推一条通知。
- 触发：简化版用 Scriptable 单独跑一个「通知脚本」（非组件）+ 快捷指令定时拉帧，命中 alert 发本地通知；或 Worker `Cron Trigger` 检测 `seq`+alert 调 Pushcut webhook。
- **验收**：制造一个 WAITING → 手机几十秒内收到推送。不做也不影响组件，只是感知延迟回到 5–15 min。

> **以后想要电脑端**：同一个 `/frame` 帧可以用一个单文件网页（Worker 加 `GET /dash` 同源路由，免 CORS，粘一次 token 存 localStorage）秒变电脑/任意浏览器 reader。本轮不做；要的话半天能加，规格在旧计划存档里。

---

## 6. 执行顺序 / 里程碑

```
M0 今天可做（不需要浏览器）：Step 1 重 enroll token
      └─▶ 手机组件能鉴权（显示 SLEEP）        ← "管道通了"

M1 数据（1 条命令 + 重启）：Step 2 切 relay 模式
      └─▶ 云里有真实数据（实体灯静止，本轮接受） ← "云端有数据"
      依赖：M0 token 有效

M2 手机：Step 3 Scriptable 组件
      └─▶ 手机跨网段看到真实状态              ← "Boss 目标达成"

M3 可选：Step 4 Pushcut 告警
```

**关键路径**：M0 → M1 → M2，全是配置 + 粘脚本，**无 dev-loop 代码活**。
**粗估**：Step 1≈10min；Step 2≈10min（+ 观察）；Step 3≈1–2h（含装 App、调样式）；Step 4≈1–2h。

---

## 7. 验收标准汇总

| Step | Gate |
|---|---|
| 1 | `GET /frame` 401→200；revoke 后立即 401 |
| 2 | 云帧出现真实 fleet + seq 自增 + 断网恢复跟上；`agentlamp doctor` 绿 |
| 3 | 组件显示真实 scene/primary/fleet；断网/401 显 OFFLINE 不崩；锁屏+主屏都能加 |
| 4 | 制造 WAITING → 手机几十秒内收推送 |

---

## 8. 风险与缓解（手机-only 后大幅收窄）

| 风险 | 影响 | 缓解 |
|---|---|---|
| **iOS 组件刷新被系统节流** | 组件非实时（5–15min） | 诚实告知；秒级靠 Step 4 推送；组件定位「扫一眼」 |
| **token 泄露** | 他人读状态（只读、可吊销、低敏） | header-only + Keychain；revoke 即失效；可加 CF Access |
| **device 端点 20/min 限流** | 轮询过快 429 | 组件 5–15min 远低于限流；附录代码内置退避 |
| **DO evict 后 env token 丢失**（本次 401 疑因） | 又变 bad_token | 用 admin enroll（持久化进 DO storage）而非仅 env |
| ~~翻 relay 误熄实体灯~~ | ~~灯变砖~~ | **本轮不要灯 → 风险消失** |

---

## 9. 建议落成的 agentlamp TASK

> 由 Boss 批准后写入 `agentlamp/TASKS.md`：

- ~~**TASK-019**：collector 双推~~ → **手机-only 下取消**（改为「切 relay 模式」配置，无代码）。
- **TASK-020 (P1)**：iPhone Scriptable 只读组件（主屏/锁屏），凭证走 Keychain，断网降级。
- **TASK-022 (P2，可选)**：Pushcut alert 推送 + 安全收尾（revoke 演练 / CF Access / 审计抽查）。

---

## 10. 我（秘书）能立刻代跑的（待 Boss 一句话）

- ✅ **Step 1**：admin token 重新 enroll device token（additive，不需要浏览器）。跑完手机组件今天就能鉴权（先显示 SLEEP）。
- ⏸️ **Step 2 切 relay + 重启 daemon**：改动正在跑的系统（且会让实体灯静止）→ 等 Boss 一句话再执行。手机-only 下零代码、近零风险，但仍由你拍板。
- ⏸️ **commit/push 任何客户端代码到公开仓库**：等 Boss 批准（先过作者邮箱 / 零真值红线）。

---

## 附录 A — iPhone Scriptable 组件完整代码

> 粘进 Scriptable 新脚本。先填三个常量（或用底部 Keychain 开关）。Scriptable 的 `Request` 不是浏览器，无 CORS 限制，可直连。

```javascript
// AgentLamp — Scriptable home/lock-screen widget (read-only).
// 填这三个；token 建议用 Keychain（见底部 USE_KEYCHAIN）。
const RELAY_URL = "{RELAY_URL}";        // 例 https://relay.example.com（真值见 relay-deploy.txt）
const DEVICE_ID = "{DEVICE_ID}";
let   TOKEN     = "{DEVICE_TOKEN}";

const USE_KEYCHAIN = false;             // true: 首次运行存进 Keychain，之后从 Keychain 读
const KC_KEY = "agentlamp_token";
if (USE_KEYCHAIN) {
  if (Keychain.contains(KC_KEY)) TOKEN = Keychain.get(KC_KEY);
  else Keychain.set(KC_KEY, TOKEN);
}

const ACCENT = { purple:"#A78BFA", cyan:"#22D3EE", green:"#34D399", yellow:"#FBBF24",
                 red:"#F87171", blue:"#60A5FA", white:"#E5E7EB", muted:"#6B7280" };

async function getFrame() {
  const r = new Request(`${RELAY_URL}/api/v1/device/${DEVICE_ID}/frame`);
  r.method = "GET";
  r.headers = { "Authorization": `Bearer ${TOKEN}`, "X-Frame-Schema-Version": "1" };
  r.timeoutInterval = 10;
  const j = await r.loadJSON();
  if (r.response && r.response.statusCode >= 400) throw new Error(`HTTP ${r.response.statusCode}`);
  return j;
}

function txt(w, s, size, color, opts={}) {
  const t = w.addText(String(s ?? ""));
  t.font = opts.bold ? Font.boldSystemFont(size) : Font.systemFont(size);
  t.textColor = new Color(color);
  if (opts.lines) t.lineLimit = opts.lines;
  return t;
}

function build(frame, stale) {
  const w = new ListWidget();
  w.backgroundColor = new Color("#0B0F14");
  w.setPadding(12, 14, 12, 14);
  const accent = ACCENT[frame.accent] || "#60A5FA";

  txt(w, frame.headline || "AGENTLAMP", 12, accent, {bold:true});
  w.addSpacer(4);

  const p = frame.primary || {};
  txt(w, `${p.provider || "—"} · ${p.status || ""}`, 16, "#FFFFFF", {bold:true});
  txt(w, p.project || "—", 13, "#9CA3AF", {lines:1});
  if (p.task) txt(w, p.task, 11, "#6B7280", {lines:2});

  w.addSpacer(6);
  for (const row of (frame.fleet || []).slice(0, 3)) {
    txt(w, `${row.provider}  ×${row.count}  ${row.status}`, 11, "#D1D5DB", {lines:1});
  }
  if (frame.fleet_more) txt(w, `+${frame.fleet_more} more`, 10, "#6B7280");
  for (const q of (frame.quota || []).slice(0, 1)) {
    const pct = Math.round((q.w5 ?? q.week ?? 0) * 100);
    txt(w, `quota ${q.provider} ${pct}%`, 10, pct >= 90 ? "#F87171" : "#6B7280");
  }

  w.addSpacer();
  const stamp = new Date().toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});
  txt(w, stale ? `⚠︎ stale · ${stamp}` : `updated ${stamp}`, 9, stale ? "#FBBF24" : "#4B5563");

  w.refreshAfterDate = new Date(Date.now() + 5 * 60 * 1000); // 建议；最终由 iOS 调度
  return w;
}

let frame, stale = false;
try {
  frame = await getFrame();
  if (frame.server_time) stale = (Date.now()/1000 - frame.server_time) > 120;
} catch (e) {
  frame = { headline: "OFFLINE", accent: "muted",
            primary: { provider: "—", status: "ERR", project: String(e).slice(0,40) }, fleet: [] };
  stale = true;
}
const widget = build(frame, stale);
if (config.runsInWidget) Script.setWidget(widget);
else widget.presentMedium();
Script.complete();
```

---

## 附录 C — 凭证 / 命令速查（占位符；真值见 `~/.config/agentlamp/relay-deploy.txt`）

### C-1 重新 enroll device token（admin 路由，无需浏览器）
```sh
# 从 relay-deploy.txt 读 RELAY_URL / ADMIN_TOKEN / DEVICE_ID / DEVICE_TOKEN（不要回显）。
TS=$(date +%s); NONCE=$(python3 -c "import secrets;print(secrets.token_hex(16))")
curl -sS -X POST "$RELAY_URL/admin/devices/$DEVICE_ID/enroll" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "X-ACO-Timestamp: $TS" -H "X-ACO-Nonce: $NONCE" \
  -H "content-type: application/json" \
  -d "{\"token\":\"$DEVICE_TOKEN\"}"
# 验证：
curl -s -o /dev/null -w "%{http_code}\n" \
  -H "Authorization: Bearer $DEVICE_TOKEN" -H "X-Frame-Schema-Version: 1" \
  "$RELAY_URL/api/v1/device/$DEVICE_ID/frame"   # 期望 200
```

### C-2 collector 切 relay 模式
```sh
export AGENTLAMP_RELAY_HOST="<完整 https URL>"     # 见 relay-deploy.txt
printf '%s' "$ADMIN_TOKEN" | agentlamp enroll \
  --relay-host "$AGENTLAMP_RELAY_HOST" --collector-id <this-mac> --admin-token-stdin
[ -f ~/.config/agentlamp/relay.env ] && . ~/.config/agentlamp/relay.env
agentlamp status && agentlamp doctor             # 确认签名推送已配置 + 健康
launchctl kickstart -k gui/$(id -u)/com.hulu.agentlamp.daemon
```

### C-3 吊销（演练 / 应急）
```sh
TS=$(date +%s); NONCE=$(python3 -c "import secrets;print(secrets.token_hex(16))")
curl -sS -X POST "$RELAY_URL/admin/devices/$DEVICE_ID/revoke" \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "X-ACO-Timestamp: $TS" -H "X-ACO-Nonce: $NONCE" \
  -H "content-type: application/json" -d '{}'
# 之后 /frame 立即 401（DO 强一致吊销）。
```

---

## 附录 D — 端到端测试清单

- [ ] Step 1 后：`/frame` 200，scene=sleep（数据空也算通）
- [ ] Step 2 后：云帧出现真实 fleet + seq 自增；`agentlamp doctor` 绿
- [ ] Step 3：飞行模式开/关 → 组件 OFFLINE↔正常切换不崩
- [ ] Step 3：制造 ≥2 个活跃 agent → 组件 scene=AGENTS 显示 fleet 行
- [ ] Step 4（可选）：制造 WAITING → 手机几十秒内收推送
- [ ] revoke → 组件立即 401
- [ ] grep 客户端代码 + 本计划 → 零真值（无 RELAY_URL/token/QQ 号）

---

*本计划书由 ai-center 秘书基于 2026-06-05/06 对 agentlamp 仓库 + live relay 的实测编写，并按 Boss 2026-06-06「不考虑 ESP32、仅做手机」的范围收窄重写。落地执行需 Boss 批准并走 agentlamp dev-loop。*
