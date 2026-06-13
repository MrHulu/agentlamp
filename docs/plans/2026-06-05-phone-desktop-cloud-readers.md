# AgentLamp · 手机组件 + 电脑端「云端跨网读取」执行计划书

> **日期**：2026-06-05 · **作者**：ai-center 秘书 · **状态**：草案，待 Boss 批准
> **范围**：agentlamp v1（单 owner）· 给 iPhone 加一个**桌面/锁屏组件（非 App）**+ 电脑端读取，
> 都从**云端**读，**不要求同一网段**。
> **一句话**：云端 relay 后端基本就绪，真正缺的是「让云里有实时数据」+「两个只读客户端」。

---

## 0. 一页纸结论（TL;DR）

| 维度 | 现状（2026-06-05 实测） | 结论 |
|---|---|---|
| 云端 relay（CF Worker + DO + KV） | 已部署、全球可达，`/healthz` 200 | ✅ 在线 |
| 读取端点 `GET /api/v1/device/:id/frame` | Bearer 鉴权正常（统一 401） | ✅ 可用 |
| 保存的 device token | 实测 `bad_token` —— 已不在 live registry | ⚠️ 需重新 enroll（1 条命令） |
| 云里有没有实时数据 | **没有**——本机 collector 是 LOCAL 模式，只喂 USB/局域网实体灯 | ⚠️ 需打通数据链路 |
| relay 模式 vs local 模式 | **互斥**：直接翻 relay 会**停止喂实体灯**，而灯暂时够不到云（固件 TLS=P4 未做） | 🚨 需「双推」改造，否则灯变砖 |
| iPhone 组件 / 电脑端 reader | 还没有 | 🆕 本计划新增（纯只读） |

**所以真正要做的事，按依赖排序：**

1. **Track 0（后端打通，P0）**：① 重新 enroll device token（additive，不动灯，我现在就能跑）；② collector「双推」改造——同时喂本地灯 + 推云（dev-loop 代码活）。
2. **Track A（iPhone 组件）**：Scriptable 脚本（~60 行 JS，免 App，免越狱），Bearer 走 header。
3. **Track B（电脑端 reader）**：在 Worker 上加一个 `/dash` 网页路由（同源 → 免 CORS），任意浏览器打开、粘一次 token、自动轮询。
4. **Track C（即时告警，可选）**：Pushcut 在 WAITING/ERROR 时推一条通知（补 iOS 组件刷新慢的短板）。
5. **Track D（安全收尾）**：token 轮换/吊销演练、CORS 仅按需开、retention/audit 抽查、公开仓库红线复核。

> **诚实边界**：iOS 桌面组件刷新由系统节流（约 5–15 分钟一次，不是实体灯的 4 秒实时）。要「秒级感知」靠 Track C 的推送，组件负责「扫一眼概览」。这是 iOS 平台限制，不是我们能力不足。

---

## 1. 现状基线（全部实测，非记忆）

### 1.1 云端 relay
- 部署形态：Cloudflare Worker + Durable Object（`RelayDO` 单例，强一致状态）+ KV（非紧急配置/缓存）。
- 路由（`src/cloud/src/index.ts`）：
  - `POST /api/v1/collectors/:id/events` —— 签名摄取（HMAC 规范串 + ±300s 时窗 + nonce 防重放 + 幂等 + 云端独立 sanitize 闸）。**P1 已落地**（13 安全测试 + 160 server 测试绿）。
  - `GET /api/v1/device/:id/frame` —— **只读拉帧**，`Authorization: Bearer <token>` + `X-Frame-Schema-Version: 1`，边缘限流 20/min/设备。
  - `GET /api/v1/device/:id/cacerts` —— 固件 CA 轮换刷新（同 `/frame` 鉴权）。
  - `POST /admin/{collectors,devices}/:id/{revoke,enroll}` —— 常量时间 bearer 闸（`AGENTLAMP_ADMIN_TOKEN`，未设=fail-closed 403）+ 限流 + `X-ACO-Timestamp`/`X-ACO-Nonce` 防重放。**I5：加一台机器只要一条 authed POST，不用 redeploy。**
- 实测：`GET /healthz` → `200 {"ok":true,"service":"agentlamp-relay","v":1}`。
- 实测：`GET /frame`（带保存的 token）→ `401 {"error":"bad_token"}`，与乱填 token 同款错误 ⇒ **该 token 不在 live registry**（6/3 之后被轮换/吊销，或 DO 单例 evict 后 env 里没重设）。

### 1.2 本机 collector（这台 Mac）
- 进程：`com.hulu.agentlamp.daemon`（运行中）+ `com.hulu.agentlamp.server`（运行中，loopback `/healthz` 200）。
- 模式：**LOCAL**（无 `~/.config/agentlamp/relay.json`、launchd 无 `AGENTLAMP_RELAY_HOST`）。即 daemon 把事件 POST 到 loopback server，由 server 经 USB 线/局域网喂实体灯。
- 推云能力：**代码已具备**（`src/collector/relaypost.py` 签名推送 + `daemon.py` 认 `config.RELAY_MODE` + `agentlamp enroll` 一键配置 + keyring 存 secret）。只是**没启用**。
- 关键约束（`daemon.py`）：`if config.RELAY_MODE: _post_relay() else: _post_local()` —— **二选一，不是双推**。

### 1.3 实体灯（ESP32-S3）
- 当前靠 **USB 线传输**（local server → `usb_bridge.py` → `/dev/cu.usbmodem*`）+ mDNS 局域网兜底。
- **够不到云**：固件 HTTPS + pinned CA + NTP（TASK-007 **P4**）未做，灯只能在 LAN/USB 上读 HTTP。
- 含义：在 P4 落地前，**云端数据链路与实体灯链路必须并存**，不能用 relay 模式顶替 local 模式。

---

## 2. 目标 / 非目标 / 红线

**目标**
- iPhone 上一个**桌面 + 锁屏组件**显示 agent 概览（fleet/focus/quota/alert），**非 App**。
- 电脑端（任意浏览器，跨网段）打开一个网页看同样的状态。
- 数据走**云**，手机/电脑**不需要和 Mac 在同一网段**。

**非目标**
- ❌ 不做原生 App（Boss 明确「不要做成 App」）。
- ❌ 不改固件协议、不引入第二套后端、不动局域网/USB 链路的现有行为。
- ❌ v1 不做公开注册 / 多租户（单 owner）。

**红线（来自 agentlamp 发布约束 + 本仓规则）**
- 🚨 **token 绝不进 URL / QR / 仓库**。组件把 token 放在**设备本地脚本/Keychain**，网页把 token 放在**浏览器 localStorage**（用户手动粘一次），传输只走 `Authorization` header。
- 🚨 **仓库已公开**（github.com/MrHulu/agentlamp，2026-06-03）：本计划书与任何客户端代码**只用占位符** `{RELAY_URL}` / `{DEVICE_ID}` / `{DEVICE_TOKEN}`；真值只在 `~/.config/agentlamp/relay-deploy.txt`。
  - ⚠️ 注意：`*.workers.dev` 默认子域名里含 CF account id，而该数字**等于 Boss 私人 QQ 号** → 子域名本身算敏感，**不要**写进任何会进公开仓库的文件；优先绑自定义域。
- 🚨 提交作者邮箱红线：公开仓库提交前作者邮箱不得是私人 QQ 邮箱。
- 🚨 未经 Boss 批准不 commit / push；秘书不在下属项目写实现代码（本计划书=规格，落地由 agentlamp dev-loop 执行）。

---

## 3. 目标架构

```
                         ┌──────────────────────────────────────────┐
   本机 Mac               │            Cloudflare 边缘（全球）          │
  ┌───────────┐ 双推①loopback│  ┌────────────┐   ┌──────────────────┐ │
  │  collector│────────────┼─▶│ local server│   │  Worker (edge)   │ │
  │  daemon   │            │  └─────┬──────┘   │  限流/鉴权提取     │ │
  │ (hooks→   │ 双推②签名推送 │       │ USB/LAN  │        │           │ │
  │  事件队列) │────────────┼───────┼──签名─────▶│   RelayDO (单例)  │ │
  └───────────┘            │       ▼          │  HMAC验/防重放/    │ │
                           │   ┌────────┐     │  registry/吊销/    │ │
                           │   │实体灯   │     │  物化 frame/审计    │ │
                           │   │ESP32   │     │  └────────┬────────┘ │
                           │   └────────┘     │   KV(配置/缓存)       │
                           └──────────────────┴──────────┼──────────┘
                                                          │ GET /frame (Bearer)
                          ┌───────────────────────────────┼───────────────┐
                          ▼                                ▼               ▼
                  ┌───────────────┐              ┌──────────────┐  ┌──────────────┐
                  │ iPhone 组件    │              │ 电脑网页 reader│  │（已有）实体灯  │
                  │ Scriptable    │              │ Worker /dash  │  │ 走 LAN/USB    │
                  │ 5–15min 刷新   │              │ 5s 轮询       │  │ 4s 实时       │
                  └───────────────┘              └──────────────┘  └──────────────┘
                  + Pushcut 即时告警（WAITING/ERROR）
```

**核心点**：手机/电脑都是 relay `/frame` 的**新只读客户端**，和实体灯平级；后端不用重写，只需「双推①②」让云里有数据。

---

## 4. frame JSON 契约（客户端消费规范）

`GET /api/v1/device/:id/frame`（Bearer + `X-Frame-Schema-Version: 1`）返回 **< 2KB** 的紧凑帧。字段（源自 `src/cloud/src/frame.ts`，已核）：

```jsonc
{
  "v": 1,                       // schema 版本
  "device_id": "<id>",
  "scene": "fleet",             // boot|pairing|fleet|focus|quota|alert|offline|stale|sleep|diagnostics
  "headline": "AGENTS",         // 见下表
  "primary": {                  // 当前最该看的那个会话
    "provider": "Claude",       // Claude | Codex | Manual
    "account": "main",
    "status": "CODING",         // IDLE|THINKING|CODING|READING|TESTING|WAITING|DONE|ERROR|OFFLINE|STALE|UNKNOWN
    "project": "ai-center",     // display_title 或 project_alias
    "task": "<task_label>"
  },
  "fleet": [                    // 按 score 排序，最多 5 行（active-only）
    { "provider": "ai-center", "count": 3, "status": "CODING" }  // ⚠️ 这里 provider 实际是"项目标签"
  ],
  "fleet_more": 2,              // 超出 5 行的折叠计数（可能不存在）
  "quota": [                    // 最多 2 个账号
    { "provider": "Claude", "account": "main", "w5": 0.42, "week": 0.18, "confidence": 2, "estimated": false }
  ],
  "accent": "purple",           // 见下表 → 映射到颜色
  "ttl": 5,                     // 建议刷新间隔（秒）；客户端别低于它
  "seq": 42,                    // 内容变更才自增（可用于"有没有变化"）
  "server_time": 1733400000     // 服务端 unix 秒；客户端据此判 stale
}
```

**scene → headline**：boot=AGENTLAMP, pairing=PAIRING, fleet=AGENTS, focus=FOCUS, quota=QUOTA, **alert=ACTION REQUIRED**, offline=OFFLINE, stale=STALE, sleep=SLEEP, diagnostics=DIAGNOSTICS。

**accent → 建议颜色（深色底）**：

| accent | hex | 含义 |
|---|---|---|
| purple | `#A78BFA` | THINKING / CODING |
| cyan | `#22D3EE` | READING |
| green | `#34D399` | TESTING / DONE |
| yellow | `#FBBF24` | WAITING |
| red | `#F87171` | ERROR / 配额危险 |
| blue | `#60A5FA` | IDLE |
| white | `#E5E7EB` | STALE |
| muted | `#6B7280` | OFFLINE / UNKNOWN / sleep |

**stale 判定（客户端）**：`now - server_time > 120s` 显示「数据偏旧」；`> 600s` 显示「离线」。云端 frame 自己也会在 collector 心跳丢失时切 `offline` 场景。

---

## 5. 工作分解（WBS）

### Track 0 — 打通数据链路（P0，后端）

#### 0.1 重新 enroll device token（additive，**不动实体灯**，可立即执行）
让手机/电脑能鉴权。两条路径，Boss 在外推荐 **A**（不需要浏览器/wrangler login）：

- **A. 走 admin 路由（I5，无需 redeploy）**：用 `~/.config/agentlamp/relay-deploy.txt` 里的 `ADMIN_TOKEN`，对 `POST /admin/devices/{DEVICE_ID}/enroll` 提交（带 `X-ACO-Timestamp` ±300s + 一次性 `X-ACO-Nonce`，body `{"token":"<device_token>"}`）。脚本见**附录 C-1**。
- **B. 走 wrangler secret（owner，需浏览器一次 OAuth）**：`wrangler secret put AGENTLAMP_DEVICE_TOKENS`，值 `{DEVICE_ID}:{DEVICE_TOKEN}`。

**验收**：`GET /frame` 由 401 变 **200**（即便此刻数据空、scene=sleep 也算通——证明鉴权+渲染管道通了）。

#### 0.2 collector「双推」改造（dev-loop 代码活，**关键**）
> 🚨 现状 `_post_relay` 与 `_post_local` 二选一。直接 `agentlamp enroll` 翻 relay 会**停止喂实体灯**（灯还够不到云）。所以**必须**让 daemon 能同时喂本地灯 + 推云。

- **方案（推荐）**：给 daemon 增加「双 sink」能力——一个 `BOTH` 模式 / `--also-relay` 开关：每条事件同时 `_post_local`（loopback，喂灯）和 `_post_relay`（签名推云，喂手机/电脑）。
  - **删除语义**：只有**两个 sink 都 ack** 才删队列记录；任一失败 → 保留 + 重试（云侧靠 Idempotency-Key 防重复 apply，已有；loopback `/admin/event` 是 last-writer 状态，重复 apply 无害）。
  - 失败隔离：云侧 401/429/transient → 不影响本地灯继续被喂；本地 server 挂 → 不影响推云。
  - 路由给 cto-vogels 定接口，fullstack-dhh 实现，qa-bach 补「双推 + 单 sink 故障」测试。
- **配置**：`agentlamp enroll --relay-host "$AGENTLAMP_RELAY_HOST" --collector-id <id> --admin-token-stdin`（mint kid+secret、存 keyring、admin 路由注册 kid、写 `relay.json`/`relay.env`）。见**附录 C-2**。
- **重启**：`launchctl kickstart -k gui/$(id -u)/com.hulu.agentlamp.daemon`。

**验收**：① 实体灯继续正常（USB/LAN，`agentlamp doctor` 绿）；② 云端 `GET /frame` 返回**真实 fleet/focus**，多刷几次 `seq` 随活动自增；③ 断网→恢复后云帧自动跟上（无 tight loop）。

> **过渡替代（若先不做双推、只想今天先在手机上看到东西）**：先只做 0.1 重 enroll token，手机组件会成功鉴权并显示 SLEEP（空）——管道验证通过；等双推落地再显示真实数据。**不要**为了演示把唯一的 daemon 翻成 relay-only（会熄灯）。

---

### Track A — iPhone Scriptable 组件（非 App）

> 选型理由见 §附录调研。Scriptable = App Store 免费通用宿主，免越狱、免开发者账号，支持把 `Request.loadJSON()` 的结果渲染成主屏/锁屏 `ListWidget`，2026 仍现役。Bearer 放 header → 合「token 不进 URL/QR」红线。

- **A1**：iPhone 装 Scriptable（App Store）。
- **A2**：新建脚本 `AgentLamp`，粘贴**附录 A** 代码；填 `{RELAY_URL}`/`{DEVICE_ID}`/`{DEVICE_TOKEN}`（建议用 Scriptable Keychain API 存 token，附录 A 给了开关）。
- **A3**：主屏长按 → 加「Scriptable」组件（中号）→ 选脚本 `AgentLamp`；锁屏同理加小组件。
- **A4**：刷新节奏——脚本里 `widget.refreshAfterDate` 设 5 分钟，但**最终由 iOS 调度**（约 5–15 min，每天 ~40–70 次预算）。够「概览」，不够「实时」。

**验收**：组件显示当前 scene/primary/fleet；Mac 上跑起真实 agent 后，下次刷新组件能反映（手动点组件可强制刷新）；断网/401 时组件显示 OFFLINE 而不是崩。

---

### Track B — 电脑端网页 reader（跨网段，免 CORS）

> 决策：**让 Worker 自己托管 dashboard 网页**（新增 `GET /dash` 路由）。因为页面与 `/frame` API **同源** → 浏览器 fetch **不需要 CORS**，省一整类麻烦。token 由用户在页面粘一次、存浏览器 localStorage，**绝不**写进 Worker 源码（仓库公开）。

- **B1**：Worker 加 `GET /` 或 `GET /dash` 返回**附录 B** 的 HTML（`text/html`, `cache-control: no-store`）。patch 见附录 B-2。
- **B2**：HTML 内置 JS：首次提示输入 `device_id` + `token`（存 localStorage）→ 每 5s（≥ttl）`fetch('/api/v1/device/<id>/frame', {headers:{Authorization:'Bearer '+token, 'X-Frame-Schema-Version':'1'}})` → 渲染 scene/primary/fleet/quota + accent 配色 + stale 提示。
- **B3**（可选）：加 `manifest.json` + service worker → 浏览器「添加到 Dock/主屏」变 PWA（电脑 + 手机浏览器都能装；注意 PWA 在 iOS 只能做 App 图标，**做不了主屏组件**——主屏组件仍由 Track A 的 Scriptable 负责，两者分工）。
- **B4**（备选路线，不推荐先做）：菜单栏小程序（类 CodexBar）。原生、能做托盘常驻，但工作量大、要分发签名；网页 reader 已覆盖「跨网段任意电脑打开」需求，菜单栏留作 vNext。

**验收**：换一个**不同网段**的电脑/手机浏览器打开 `{RELAY_URL}/dash`，粘 token 后能看到与组件一致的状态；token 只在该浏览器 localStorage；刷新频率不触发 429。

---

### Track C — 即时告警（可选，补 iOS 刷新慢）

- **Pushcut**（App Store 免费档）：建一个 Webhook/通知；当 scene=`alert`（WAITING/ERROR）时推送。
- 触发源二选一：① collector 双推时若 cloud 帧进入 alert，由一个小 cron/Worker `Cron Trigger` 检测 `seq` 变化 + alert → 调 Pushcut webhook；② 简化版：Scriptable 单独跑一个「通知脚本」（非组件），用快捷指令自动化定时拉帧，命中 alert 就发本地通知。
- **验收**：制造一个 WAITING（agent 等输入）→ 手机几十秒内收到推送。

> 这条是「秒级感知」的补丁；不做也不影响组件/网页可用，只是感知延迟回到 iOS 组件节流的 5–15 min。

---

### Track D — 安全与发布收尾（沿用 TASK-007 P3 思路）

- **D1**：device token 轮换/吊销演练——`POST /admin/devices/:id/revoke` 后确认组件/网页**立即** 401（DO 强一致吊销）。
- **D2**：CORS——dashboard 走同源**不开** CORS；若将来要做独立托管的前端，再**仅**对 `/frame` 加 `Access-Control-Allow-Origin: <白名单 origin>`，**绝不** `*`。
- **D3**：retention/audit——确认 `RETENTION_DAYS=30` purge alarm 在跑；审计环只记 reason+hash，不记原值。
- **D4**：公开仓库复核——客户端代码 + 本计划书**零真值**；commit 作者邮箱非 QQ；`relay-deploy.txt` 不进仓库（已在 `~/.config`）。
- **D5**（可选硬化）：把 `/dash` 与 `/admin` 一起挂 Cloudflare Access（TOTP/邮箱 OTP），多一层边缘鉴权（无 ESP32 在此路径，纯人/浏览器，安全加固划算）。

---

## 6. 执行顺序 / 依赖 / 里程碑

```
M0 今天可做（无浏览器、不动灯）：Track 0.1 重 enroll device token
      └─▶ 手机组件/网页能鉴权（显示 SLEEP）  ← "管道通了"里程碑

M1 后端打通（dev-loop）：Track 0.2 双推改造 + enroll collector + 重启 daemon
      └─▶ 云里有真实数据；实体灯不受影响       ← "云端有数据"里程碑
      依赖：M0 的 token 有效

M2 客户端（可与 M1 并行开发，M1 完成后联调）：
      ├─ Track A iPhone Scriptable 组件
      └─ Track B Worker /dash 网页 reader
      └─▶ 手机+电脑跨网段看到真实状态           ← "Boss 的目标达成"里程碑

M3 增强（可选）：Track C Pushcut 告警 + Track D 安全收尾
```

**关键路径**：M0 → M1(0.2 双推) → M2 联调。M1 的双推是唯一「重」一点的代码活，其余都是只读客户端。

**粗估**（dev-loop 投入，非挂钟）：0.1≈10min；0.2 双推≈半天（含测试）；Track A≈1–2h；Track B≈半天；Track C≈1–2h；Track D≈1–2h。

---

## 7. 验收标准汇总（每 Track 的 gate）

| Track | Gate |
|---|---|
| 0.1 | `GET /frame` 401→200；revoke 后立即 401 |
| 0.2 | 灯正常(doctor 绿) + 云帧出现真实 fleet + seq 自增 + 断网恢复跟上 + 单 sink 故障不互相拖垮 + 新测试绿 |
| A | 组件显示真实 scene/primary/fleet；断网/401 显 OFFLINE 不崩；锁屏+主屏都能加 |
| B | 异网段浏览器粘 token 即见状态；token 只在 localStorage；不触发 429 |
| C | 制造 WAITING → 手机几十秒内收推送 |
| D | revoke 立即生效；无 `*` CORS；审计只存 hash；零真值入公开仓库 |

---

## 8. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| **iOS 组件刷新被系统节流** | 组件非实时（5–15min） | 诚实告知 Boss；秒级靠 Track C 推送；组件定位为「扫一眼」 |
| **双推删除语义写错 → 丢事件或重复** | 状态漂移 | 两 sink 都 ack 才删；云侧幂等键已有；qa-bach 专测 |
| **翻 relay 误熄实体灯** | 灯变砖 | 死守「双推」，绝不 relay-only 顶替；过渡期只做 0.1 |
| **token 泄露** | 他人读状态（只读、可吊销、低敏） | header-only + Keychain/localStorage；revoke 即失效；可加 CF Access |
| **workers.dev 子域含 QQ 号** | 私人信息泄露 | 计划书/客户端只用占位符；优先绑自定义域 |
| **device 端点 20/min 限流** | 轮询过快 429 | 组件 5–15min、网页 5s 均远低于；附录代码内置退避 |
| **DO evict 后 env token 丢失**（本次 401 疑因） | 又变 bad_token | 优先用 admin enroll（持久化进 DO storage）而非仅 env；或确保 `AGENTLAMP_DEVICE_TOKENS` secret 已设 |

---

## 9. 建议落成的 agentlamp TASK

> 由 Boss 批准后写入 `agentlamp/TASKS.md`：

- **TASK-019 (P0)**：collector 双推（loopback 喂灯 + 签名推云并存）；含双 sink 删除语义 + 单 sink 故障隔离 + 测试。
- **TASK-020 (P1)**：iPhone Scriptable 只读组件（主屏/锁屏），凭证走 Keychain，断网降级。
- **TASK-021 (P1)**：Worker `/dash` 同源网页 reader（+ 可选 PWA manifest）。
- **TASK-022 (P2)**：Pushcut alert 推送 + Track D 安全收尾（revoke 演练 / CF Access / 审计抽查）。

---

## 10. 我（秘书）能立刻代跑的（待 Boss 一句话）

- ✅ **Track 0.1**：用 admin token 重新 enroll device token（**additive，不碰实体灯，不需要浏览器**）。跑完手机组件/网页今天就能鉴权（先显示 SLEEP）。
- ⏸️ **Track 0.2 双推 / enroll collector / 重启 daemon**：会改动正在跑的系统 + 是下属项目代码活 → 走 agentlamp dev-loop，**等 Boss 批准**。
- ⏸️ **commit/push 客户端代码到公开仓库**：等 Boss 批准（且先过作者邮箱/零真值红线）。

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

## 附录 B — 电脑端 dashboard

### B-1 单页 HTML（Worker 返回）

```html
<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AgentLamp</title>
<style>
  :root{--bg:#0B0F14;--card:#11161D;--mut:#6B7280;--fg:#E5E7EB}
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);
    font:15px/1.5 -apple-system,system-ui,"PingFang SC",sans-serif;display:flex;
    min-height:100vh;align-items:center;justify-content:center}
  .card{background:var(--card);border-radius:18px;padding:24px 28px;min-width:320px;
    box-shadow:0 8px 40px rgba(0,0,0,.5)}
  .headline{font-weight:700;letter-spacing:.08em;font-size:13px}
  .status{font-size:24px;font-weight:700;margin:8px 0 2px}
  .proj{color:#9CA3AF} .task{color:var(--mut);font-size:13px;margin-top:2px}
  .fleet div{font-size:13px;color:#D1D5DB;margin-top:4px}
  .foot{color:var(--mut);font-size:11px;margin-top:14px}
  input{background:#0b0f14;border:1px solid #283143;color:var(--fg);border-radius:8px;
    padding:8px;width:100%;margin:6px 0} button{background:#2563EB;border:0;color:#fff;
    border-radius:8px;padding:8px 14px;cursor:pointer}
</style></head>
<body><div class="card" id="root"><div class="headline">LOADING…</div></div>
<script>
const ACCENT={purple:"#A78BFA",cyan:"#22D3EE",green:"#34D399",yellow:"#FBBF24",
  red:"#F87171",blue:"#60A5FA",white:"#E5E7EB",muted:"#6B7280"};
const root=document.getElementById("root");
function need(){
  root.innerHTML=`<div class="headline">AGENTLAMP</div>
    <input id="d" placeholder="device id">
    <input id="t" type="password" placeholder="device token">
    <button onclick="save()">连接</button>`;
}
function save(){
  localStorage.aldev=document.getElementById("d").value.trim();
  localStorage.altok=document.getElementById("t").value.trim();
  tick();
}
function render(f,stale){
  const a=ACCENT[f.accent]||"#60A5FA",p=f.primary||{};
  root.innerHTML=`<div class="headline" style="color:${a}">${f.headline||"AGENTLAMP"}</div>
    <div class="status">${p.provider||"—"} · ${p.status||""}</div>
    <div class="proj">${p.project||"—"}</div>
    ${p.task?`<div class="task">${p.task}</div>`:""}
    <div class="fleet">${(f.fleet||[]).slice(0,5).map(r=>
      `<div>${r.provider} ×${r.count} ${r.status}</div>`).join("")}
      ${f.fleet_more?`<div style="color:var(--mut)">+${f.fleet_more} more</div>`:""}</div>
    <div class="foot">${stale?"⚠︎ stale · ":""}updated ${new Date().toLocaleTimeString()}
      · <a style="color:var(--mut)" href="#" onclick="localStorage.clear();need()">换 token</a></div>`;
}
async function tick(){
  const id=localStorage.aldev,tok=localStorage.altok;
  if(!id||!tok){need();return;}
  try{
    const r=await fetch(`/api/v1/device/${id}/frame`,
      {headers:{Authorization:"Bearer "+tok,"X-Frame-Schema-Version":"1"}});
    if(r.status===401){root.innerHTML='<div class="headline" style="color:#F87171">401 bad token</div>';
      setTimeout(need,1500);return;}
    if(!r.ok)throw new Error("HTTP "+r.status);
    const f=await r.json();
    render(f, f.server_time ? (Date.now()/1000-f.server_time)>120 : false);
  }catch(e){
    render({headline:"OFFLINE",accent:"muted",primary:{provider:"—",status:String(e).slice(0,30)},fleet:[]},true);
  }
}
tick(); setInterval(tick,5000);   // ≥ ttl(5)
</script></body></html>
```

### B-2 Worker 路由 patch（`src/cloud/src/index.ts`，在末尾 404 之前加）

```typescript
// GET / or /dash — 同源只读 dashboard（无 secret；token 由浏览器端用户提供并存 localStorage）。
if (request.method === "GET" && (url.pathname === "/" || url.pathname === "/dash")) {
  return new Response(DASH_HTML, {
    headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" },
  });
}
```
（`DASH_HTML` = B-1 的字符串常量，单独放 `src/dash.ts` 导出，保持 index.ts 整洁。）

---

## 附录 C — 凭证 / 命令速查（占位符；真值见 `~/.config/agentlamp/relay-deploy.txt`）

### C-1 重新 enroll device token（admin 路由，无需浏览器）
```sh
# 从 relay-deploy.txt 读 RELAY_URL / ADMIN_TOKEN / DEVICE_ID / DEVICE_TOKEN（不要回显）。
# admin enroll 需带新鲜度头：X-ACO-Timestamp(±300s) + 一次性 X-ACO-Nonce。
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
> 备选（owner，需 `wrangler login`）：`wrangler secret put AGENTLAMP_DEVICE_TOKENS` → `<DEVICE_ID>:<DEVICE_TOKEN>`。

### C-2 collector 进 relay（配合 Track 0.2 双推改造后）
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

- [ ] 0.1 后：`/frame` 200，scene=sleep（数据空也算通）
- [ ] 0.2 后：实体灯仍正常（`agentlamp doctor` 绿）+ 云帧出现真实 fleet
- [ ] 0.2 后：单 sink 故障注入（停 local server / 断网）→ 另一路不受影响、记录不丢
- [ ] A：飞行模式开/关 → 组件 OFFLINE↔正常切换不崩
- [ ] A：制造 ≥2 个活跃 agent → 组件 scene=AGENTS 显示 fleet 行
- [ ] B：手机热点（异网段）开电脑浏览器 `/dash` → 粘 token 见状态
- [ ] B：连点刷新不触发 429（5s 间隔安全）
- [ ] D：revoke → 组件 + 网页都立即 401
- [ ] D：grep 客户端代码 + 本计划 → 零真值（无 RELAY_URL/token/QQ 号）

---

*本计划书由 ai-center 秘书基于 2026-06-05 对 agentlamp 仓库 + live relay 的实测编写；所有端点行为、模式互斥、token 状态均已验证，非记忆推断。落地执行需 Boss 批准并走 agentlamp dev-loop。*
