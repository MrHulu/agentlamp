# AgentLamp · 多台电脑「云端聚合」执行计划 v3（N 机 → 一份 frame）

> **日期**：2026-06-07 · **作者**：ai-center 秘书 · **状态**：草案，待 Boss 批准
> **范围**：把 v2（单机手机版）扩成**多台电脑都推云端、手机只拿一份聚合 frame**，起步 N=2。
> **本轮明确放弃**：❌ ESP32 实体灯 · ❌ 桌面 reader · ❌ 实时推送（widget 被系统限到 ~5–15 min）· ❌ 多用户/多租户（同一人两台机）· ❌ 重写云端聚合算法。
> **一句话**：核实后发现**云端零改动**——`idFromName("relay")` 决定全局只有一个 RelayDO 单例，所有 collector 的会话天然汇入同一份 frame；真正的工作量只剩「手机怎么分清哪台机器」，靠 alias 约定解决。

---

## 0. 一页纸结论（TL;DR）

| 维度 | 现状（2026-06-07 核实） | 多机后怎么变 |
|---|---|---|
| 云端 RelayDO | 单例（`idFromName("relay")`），所有 ingest + frame 都走它 | ✅ **不动**——多 collector fan-in 是既有能力 |
| 会话聚合 `frame.ts` | `applySanitizedEvent` 写进**共享** `st.sessions`；`buildFrame` 从这份共享态出 frame | ✅ **不改算法**——两台会话本就在一起 |
| collector 身份 | per-collector `kid` + HMAC secret，运行时 enroll 入 DO storage（无需 `wrangler deploy`） | 🆕 第二台机器多 enroll 一个 kid 即可 |
| device token（手机读） | per-device bearer（存哈希），与 collector 身份正交 | ✅ **不动**——手机那套不受加机器影响 |
| 推送代码 `relaypost.py` | 已就绪，本机 daemon 当前是 LOCAL 模式 | 🔁 每台机器切 relay + 重启 daemon |
| ⚠️ 会话 key | `(provider, account_alias, session_id‖project_alias)`，**不含机器维度** | 🚨 两台同账号同项目可能互相覆盖 / fleet 合并成一行 → 靠 alias 区分（§3） |
| ⚠️ collector 心跳 | 全局单值 `last_collector_heartbeat` | 「整队 offline」语义变弱；单台掉线靠每会话 STALE/OFFLINE 衰减处理（符合预期） |
| 显示上限 | `FLEET_MAX_ROWS=5` / `FRAME_BYTE_CAP=2048` | 两台会话翻倍 → 更易触顶，现有 `enforceByteCap` 兜底，需实测 |

**真正要做的，按依赖排序（全是配置/运维，零云端代码）：**

1. **Step 1**：第二台电脑 enroll 成独立 collector（唯一 `collector_id` + kid + secret）。
2. **Step 2**：每台电脑 daemon 切 relay 模式 + 重启。
3. **Step 3**：每台机器设不同 `account_alias`（方案 A）让手机分清哪台。
4. **Step 4**：手机 widget 不变（本来就读聚合 device frame）。

---

## 1. 为什么（痛点 · Boss 原话 2026-06-07）

> "我现在有两台电脑设备，两电脑设备都会在使用我的账号，你有没有考虑这种情况让他们两个都能上传到云端，然后手机就只负责去这个云端去拿取状态就行了。"

Boss 同时在两台电脑上跑 agent（claude / codex），都登录同一个人账号。现状每台 collector 只喂自己那台，手机最多看到一台。目标：**N 台都推云端 → 手机只读一份聚合 frame → 一眼看全部机器上所有 agent**，且跨网络（电脑在公司、手机在外也能看）。

为什么是现在：6/06 砍掉物理灯后不再需要「双推」复杂度；推送代码早已存在；经核实云端本就支持多 collector，落地成本极低。

## 2. 核心结论：聚合天然支持（零云端改动）

`src/cloud/src/index.ts`：

```ts
const id = env.RELAY.idFromName("relay");   // ← 全局唯一名字 → 单例
return env.RELAY.get(id);                    // ← 所有请求都进这一个 DO
```

- **所有 collector** 的 ingest 路由到这**同一个** DO 实例。
- DO 内只有**一份** `this.frame: FrameStateData`；每个事件经 `applySanitizedEvent` 写进共享 `st.sessions`。
- **所有 device frame** 由 `buildFrame(this.frame, deviceId, now)` 从这份共享态生成。

→ 两台机器的会话本就汇入同一份状态、出现在同一份 frame 的 `fleet` 里。**不用改聚合算法。**

### 身份模型（两套独立凭证，正交）

| 维度 | 凭证 | 路由 | 加机器 |
|---|---|---|---|
| **写入**（collector→云） | per-collector `kid` + HMAC secret | `POST /api/v1/collectors/:id/events` 签名校验 | `POST /admin/collectors/:kid/enroll`，运行时入 DO storage，**无需 deploy** |
| **读取**（手机→云） | per-device bearer token（存哈希） | `GET /api/v1/device/:id/frame` | `POST /admin/devices/:id/enroll` |

加一台电脑 = 多 enroll 一个 collector kid；device token 不动。enroll/revoke 强一致（I4/I5），revoke 立即生效。`COLLECTOR_ID = AGENTLAMP_COLLECTOR_ID env / collector_id 配置 / 否则 ACCOUNT`（`config.py:112`）→ 每台机器设唯一值即可。

## 3. 机器区分（本项目真正的工作量）

`frame.ts` 会话 key = `(provider, account_alias, session_id ‖ project_alias)`，**不含 collector_id / hostname**。后果：

- 两台跑同 provider + 同 `account_alias` + `session_id` 为空（回退 `project_alias`）+ 同项目名 → **互相覆盖**（同 key）。
- 即便 session_id 唯一不覆盖，fleet 按 `displayLabel = display_title ‖ project_alias` 分组 → 同项目两台**合并成一行 count=2**，手机看不出在哪台。

**缓解（二选一）：**

- **方案 A（推荐，零代码起步）**：每台机器设不同 `account_alias`（`studio` / `macbook`）→ key 天然区分 + primary 块显示 account。**iPhone widget 已实现**：渲染 `▸ <account>`（`readers/iphone-widget/`，`frame-view.buildViewModel` 已 surface account，conformance test 覆盖）。
- **方案 B（fleet 行也分开）**：给每台设一个中性「机器别名」（owner 自定，如 `studio`），折进 `project_alias`（`ai-center·studio`）→ fleet 按「项目·机器」分行。⚠️ 用别名，**绝不用真实 hostname**（I3）；本项默认不做，列为待决 D1。

> 是否要在云端/collector 层把 hostname 升为一等字段（而非 alias 约定），列为**待决 D1**。

## 4. 运维手册（计划稿，未经批准不执行）

> ⚠️ 占位符：`{RELAY_URL}` `{COLLECTOR_ID}` `{DEVICE_ID}` `{DEVICE_TOKEN}` `{ADMIN_TOKEN}`。真实值只存机器本地 `~/.config/agentlamp/relay-deploy.txt`，**绝不进仓库 / URL / QR**。

**4.1 每台电脑 enroll 成独立 collector**（唯一 `collector_id`）：

```
# 机器 A
agentlamp enroll --relay-host {RELAY_URL} --collector-id studio  --admin-token-stdin
# 机器 B（换 collector-id）
agentlamp enroll --relay-host {RELAY_URL} --collector-id macbook --admin-token-stdin
```

> `ADMIN_TOKEN` 走 stdin，不进 shell history。enroll → DO 持久化 kid+secret，重启不丢、加机器零部署。

**4.2 每台电脑切 relay 模式 + 重启 daemon**：

```
launchctl kickstart -k gui/$(id -u)/com.hulu.agentlamp.daemon
```

> daemon 互斥：`if config.RELAY_MODE: _post_relay() else: _post_local()`。enroll 写入的 relay 配置让它进 relay 分支。

**4.3 机器区分**：每台设不同 `account_alias`（方案 A）。

**4.4 设备 token（手机读，一次性）**：设备 token 由 admin 提供（**不像** collector secret 那样服务端自动 mint），所以先在本地 mint，再 enroll 到 relay。CLI **暂无 `enroll-device` 子命令**，走 admin curl（与 [`../../readers/iphone-widget/DEPLOY.md`](../../readers/iphone-widget/DEPLOY.md) 完全一致）：

```
# 1. 本地 mint 一个高熵设备 token，存进 ~/.config/agentlamp/relay-deploy.txt（绝不进仓库）
python3 -c "import secrets; print(secrets.token_hex(32))"        # → {DEVICE_TOKEN}

# 2. enroll 到 relay —— admin 路由要求 bearer + 新鲜度头（X-ACO-Timestamp ±300s + 单次 X-ACO-Nonce）
TS=$(date +%s); NONCE=$(python3 -c "import secrets;print(secrets.token_hex(16))")
curl -fsS -X POST "{RELAY_URL}/admin/devices/{DEVICE_ID}/enroll" \
  -H "Authorization: Bearer {ADMIN_TOKEN}" \
  -H "X-ACO-Timestamp: $TS" -H "X-ACO-Nonce: $NONCE" \
  -H "content-type: application/json" \
  -d "{\"token\":\"{DEVICE_TOKEN}\"}"
# DO 持久化 token 的 HASH（永不存明文）；{DEVICE_TOKEN} 只存手机端 + 本地
```

**4.5 手机 widget**：指向聚合 frame，**无需改 widget**（reader 代码 + 真机部署步骤见 [`../../readers/iphone-widget/`](../../readers/iphone-widget/)）：

```
GET {RELAY_URL}/api/v1/device/{DEVICE_ID}/frame
Authorization: Bearer {DEVICE_TOKEN}
X-Frame-Schema-Version: 1
```

## 5. 验收

1. 两台 daemon 都在 relay 模式，`/admin` 看到两个 active collector kid。
2. 两台同时跑 agent → 手机一份 frame 的 `fleet` 同时出现两台的会话。
3. 关一台 → ~10 min 后该机会话 STALE→OFFLINE，**另一台不受影响**。
4. 手机能区分「这个 agent 在哪台机」（方案 A 生效）。
5. 手机蜂窝网也能拉到 frame。

## 6. 回滚

- 单机回滚：该机 daemon 切回 local + 重启。
- 撤销某机器：`agentlamp revoke --kid <kid>`（admin revoke，立即失效）。`<kid>` 是 enroll 时 mint/指定的 **key id**（≠ `collector_id` 显示名）；本机 kid 用 `agentlamp status` 查。

## 7. 决策 + 待决

| ID | 决策 | 理由 |
|---|---|---|
| DD1 | 复用单例 DO 聚合，不改云端算法 | 已核实天然支持；改动=风险 |
| DD2 | 每台机器独立 collector kid + 唯一 collector_id | 写入审计 / 单独 revoke / 不互顶 |
| DD3 | 机器区分先靠 alias 约定（方案 A） | 零代码、可逆；不够再升 D1 |

**Rejected**：每台机一个独立 device（违背「手机只拿一份聚合」）· 多 DO + 读时合并（单例已强一致，多实例只增复杂度）· collector 间 P2P/选主（单例已是单点聚合）· 首轮就重写 sessionKey 加机器维度（先验证 alias 够不够）。

> DD1 诚实边界：复用单例 DO 在**单 owner 尺度**是对的（强一致零配置 fan-in），但单 DO 有 ~1k req/s 软上限（CF 官方）。本场景（几台机 + 手机 5–15min 轮询）远在天花板下，singleton 保留；真要长成 fleet/多租户再按 `idFromName(owner_id)` 分片。详见 architecture.md「Scaling envelope」。

**待决**：D1 hostname 升一等字段？· D2 per-collector 心跳？· D3 多机会话翻倍下 5 行/2KB 是否需分组折叠？· D4 collector_id 命名规范？· D5 何时从单例 DO 切到 per-owner 分片（触发条件 = 多 owner 或 ingest 逼近 1k req/s）？

## 8. 关联

- 上游 v2：`2026-06-06-iphone-only-cloud-widget.md`（单机版，本计划扩成多机；widget 代码已抽出到 `readers/iphone-widget/`）。
- 已废 v1：`2026-06-05-phone-desktop-cloud-readers.md`（含 ESP32 + 桌面 reader）。
- 调研日记：`../devlog/17-multi-device-cloud-investigation.md`。
- 读取端目录 / 部署：[`../../readers/`](../../readers/) · [`../../readers/iphone-widget/DEPLOY.md`](../../readers/iphone-widget/DEPLOY.md)。

## 红线

- 仓库 PUBLIC → 真实 host / token / kid 一律占位符，只存机器本地，绝不进仓库 / URL / QR。
- `*.workers.dev` 子域含 CF account id（敏感）→ 只用 `{RELAY_URL}`，优先自定义域名。
- 未经 Boss 批准不 commit / push、不跑 §4 的 enroll / 切 relay（触碰运行中 daemon + 云端 admin 面）。
