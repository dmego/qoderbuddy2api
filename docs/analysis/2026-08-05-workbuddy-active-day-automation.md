# WorkBuddy 成长中心连续活跃天数自动化研究

> 研究日期：2026-08-05
> 研究对象：CodeBuddy OAuth 账号对应的 WorkBuddy 成长中心
> 研究目的：在每日积分签到成功后，增加一次真实 WorkBuddy 使用行为，使成长中心当天点亮、连续活跃天数继续累计，并保留后续 7/14/28 天奖励和抽奖资格。
> 证据等级：真实账号端到端协议验证；代码行为以当前工作区为准；自动化回归使用脱敏 MockTransport。

## 1. 先给结论

这次研究不是把“积分签到”换个名字。WorkBuddy 有两套不同的状态：

1. `/billing/meter/daily-checkin` 处理的是积分/额度签到。
2. 成长中心热力图和连续活跃天数记录的是实际使用行为。

在同一账号上做过前后对照：正式 ACP 对话前，成长中心返回 `active=false`、`streak_days=1`、`score=0`；完成一轮正式 WorkBuddy ACP 对话后，返回 `active=true`、`streak_days=2`、`score=2`。因此当前唯一被真实验证能稳定推进连续活跃天数的方式，是完成一次正式 WorkBuddy ACP 对话。

普通 `/v2/chat/completions` 请求、积分签到接口、本地 usage-log 打点都不能替代这条链路。代码已经将 ACP 活跃日动作接到 CodeBuddy 定时/补执行签到成功路径，并用本地数据库做每日幂等保护。

## 2. 问题定义：积分签到不等于成长中心活跃

用户看到的“昨天签到、今天也签到，但连续登录仍是 1 天”，根因不是单纯的前端显示问题，而是“签到”一词对应了不同业务记录：

| 记录 | 典型接口/位置 | 记录内容 | 是否能点亮成长中心 |
| --- | --- | --- | --- |
| 积分签到 | `POST /billing/meter/daily-checkin` | 每日积分奖励、额度状态 | 未证明，实测不能替代 |
| 成长热力图 | `GET /activity/growth/heatmap` | 每日使用分数、当天活跃状态 | 是 |
| 连续活跃 | `GET /activity/growth/streak` | 当前连续天数、补登卡、兑换状态 | 由实际使用行为计算 |
| 客户端本地日志 | `idleCapabilityRecordActiveDay` → `usage-log.json` | 本地能力/使用日志 | 否，服务端不可见 |

所以，积分签到成功只能说明积分签到接口成功；不能据此断言成长中心当天 `is_active=true`，更不能据此断言 `streak_days` 已增加。

## 3. 成长中心数据模型和奖励关系

### 3.1 热力图、当天状态和连续天数

成长中心的热力图接口返回一年的日期网格。普通日期至少包含：

```json
{
  "date": "2026-08-04",
  "score": 64,
  "has_new_buddy": false
}
```

实测规则：`score > 0` 表示当天有被服务端认可的使用行为；当天节点还会附带 `is_active` 和 `status_text`。`streak` 接口再根据当月连续点亮日期计算连续活跃天数。公告规则显示，连续活跃按月清零，断开后只能使用当月补登卡补救。

这意味着以下三个值的含义不同：

- `today.is_active`：今天是否已经被成长中心点亮。
- `heatmap[date].score`：某日的活跃分数，不是积分余额。
- `streak_days`：从当前日期向前连续点亮的天数，不是历史总活跃天数。

### 3.2 连续活跃档位

| 档位 | 达成条件 | 研究记录中的奖励 |
| --- | --- | --- |
| 入门档 | 连续 7 天 | 能量 +2、补登卡 +1、抽奖 +1 |
| 进阶档 | 连续 14 天 | 积分 +100、能量 +5、补登卡 +2、抽奖 +1 |
| 巅峰档 | 连续 28 天 | 积分 +500、能量 +10、补登卡 +4、抽奖 +1 |

达标后兑换会消耗连续天数，且不能恢复；因此本次实现只负责增加活跃日，不自动兑换档位。抽奖、旅行、Buddy 抽取也没有被本次 ACP 活跃日动作隐式触发。

## 4. 候选方案逐项实测和排除

| 候选方案 | 实测结果 | 排除/采用原因 |
| --- | --- | --- |
| 普通 `/v2/chat/completions` | 能完成模型对话，但不能稳定使成长中心当天 `active` 变为 true | 代理聊天链路和成长中心活跃打点不是同一业务事件；不能作为连续天数依据 |
| `POST /billing/meter/daily-checkin` | 处理积分签到；不能替代成长热力图活跃 | 保留为签到动作，不承担成长中心职责 |
| 客户端 `idleCapabilityRecordActiveDay` | 只写本地 `usage-log.json` | 服务端成长中心看不到本地文件；不能用于账号池自动化 |
| 直接调用成长中心 `heatmap`/`streak` | 可读取状态；没有“把今天标为活跃”的写接口 | 只能验证结果，不能制造使用行为 |
| 成长任务 `accept`/`claim` | 接受任务和领取已有奖励可调用；不会把聊天行为伪造成已完成 | 可作为后续低风险任务自动化，但不是活跃日打点 |
| 正式 WorkBuddy ACP 对话 | 前后对照确认 `active=false → true`、`streak_days=1 → 2`、`score=0 → 2` | 当前唯一采用的活跃日入口 |

研究过程中还验证了 WorkBuddy 成长中心 API 使用 Bearer 令牌加浏览器请求头即可访问；这证明状态查询可独立于网页 Cookie，但没有发现可安全替代真实使用的“写活跃日”接口。

## 5. 从前端 bundle 追到正式 ACP 协议

逆向路径分成两条：

1. 成长中心 SPA bundle 暴露了 `profile`、`tasks`、`streak`、`heatmap`、`lottery` 等状态/操作接口，确认连续天数由服务端使用行为计算。
2. WorkBuddy 聊天页面 bundle 使用 ACP（Agent Client Protocol）建立会话；跟随会话创建、session link 和 SSE 连接，可以复现网页端的正式对话生命周期。

临时 bundle 提取脚本仅用于研究，不属于仓库功能代码，也不应提交：它们可能包含前端构建上下文、无关页面代码或过期协议线索。仓库文档只保留脱敏后的路径、字段和结论。

## 6. 已验证的正式 ACP 流程

### 6.1 创建 WorkBuddy 对话

首先使用 CodeBuddy OAuth access token 调用：

```http
POST /console/as/conversations/
Authorization: Bearer <redacted>
Content-Type: application/json
```

请求体采用网页端实际使用的最小 payload：

```json
{
  "prompt": "你好",
  "model": "hy3",
  "plugins": [
    {
      "name": "weixinpay",
      "marketplace": "codebuddy-builtin"
    }
  ]
}
```

响应可能直接返回 `id`，也可能包在 `data.id` 或 `data.conversationId`；客户端只接受非空字符串会话 ID，不把上游正文写入日志。

### 6.2 获取 session link

```http
GET /console/as/conversations/{conversation_id}/session
Authorization: Bearer <redacted>
```

响应中需要 `link`，通常还会提供 ACP 会话 token 和工作目录：

```json
{
  "data": {
    "link": "https://<acp-host>/<opaque-session-link>",
    "token": "<redacted>",
    "cwd": "/workspace"
  }
}
```

`link` 是临时会话地址，不是固定 API 路径；完整值和会话 ID 不应进入日志、审计或前端存储。

### 6.3 用 GET 建立 Streamable HTTP SSE

对 `link` 发起 GET，接受 `text/event-stream`，并读取响应头：

```http
GET <link>
Authorization: Bearer <session-token>
Accept: application/json, text/event-stream
x-codebuddy-request: 1
```

必须保存：

- `Acp-Connection-Id`：后续 JSON-RPC 请求的连接关联 ID。
- `acp-session-token`：如果服务端返回，后续 ACP 请求必须带上。

因此，ACP 请求头中会同时出现会话 Authorization、`Acp-Connection-Id` 和 `acp-session-token`；这些值都只在内存中使用。

### 6.4 在同一个 link 上发送 JSON-RPC

依次发送以下方法：

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientInfo":{"name":"qb2api","version":"1"},"clientCapabilities":{"fs":{"readTextFile":false,"writeTextFile":false}}}}
```

```json
{"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"/workspace","mcpServers":[]}}
```

从 `session/new` 的 `result.sessionId` 取会话 ID，然后固定模型：

```json
{"jsonrpc":"2.0","id":3,"method":"session/set_model","params":{"sessionId":"<redacted>","modelId":"hy3"}}
```

最后发送正式 prompt：

```json
{"jsonrpc":"2.0","id":4,"method":"session/prompt","params":{"sessionId":"<redacted>","prompt":[{"type":"text","text":"你好"}]}}
```

### 6.5 不能把 202 当作完成

真实协议中 `session/prompt` 的 POST 可能返回 `202 Accepted` 且没有正文。这只表示服务端接受了异步工作，不表示模型回合已经完成，更不表示成长中心已经记账。

正确做法是继续消费：

- 初始 GET SSE；
- 如果 POST 自己返回 `text/event-stream`，也消费该响应；
- 直到收到明确的回合结束事件。

客户端识别以下终态位置：

```json
{"method":"session_end_turn"}
```

或：

```json
{"method":"session/update","params":{"update":{"sessionUpdate":"session_end_turn"}}}
```

也兼容服务端使用 `session/endTurn` 的命名。只有收到上述终态后，本地一次 ACP 活跃日才记为 `succeeded`。

### 6.6 SSE 分片和连接清理

SSE 事件可能跨 TCP chunk 分片，不能假设一次读取就是一条 JSON。客户端按空行切分事件，将多个 `data:` 行拼接后再解析 JSON；响应结束时还会处理未以空行结尾的最后一帧。

完整生命周期如下：

```text
create conversation
        │
        ▼
get session link/token
        │
        ▼
GET link → Acp-Connection-Id + SSE consumer
        │
        ├─ initialize
        ├─ session/new
        ├─ session/set_model(hy3)
        └─ session/prompt(你好)
                │
                ▼
       consume SSE until end turn
                │
                ▼
finally: cancel streams → close contexts → DELETE link
```

`finally` 中无论成功、超时还是协议错误都会取消后台 SSE 任务、关闭 HTTP stream，并尝试 DELETE 临时 link，避免服务端会话和本地任务泄漏。

## 7. 错误协议尝试及其排除依据

早期尝试过把 session link 当作普通连接接口：

| 请求 | 结果 | 结论 |
| --- | --- | --- |
| `POST {link}/connect`，不带鉴权 | `401` | 缺鉴权，但不能证明 `/connect` 是正式协议 |
| `POST {link}/connect`，带会话 token | `404` | 路径不存在；不能继续使用 `/connect` 方案 |
| `GET {link}` + JSON-RPC POST | 成功收到 ACP 终态事件 | 当前采用的 Streamable HTTP 方案 |

这组结果很重要：不能因为某个临时 link 返回 401 就继续猜测路径，也不能把 404 当成“token 错误”反复重试。

## 8. 当前代码落点和行为边界

| 文件/模块 | 作用 |
| --- | --- |
| `src/qb2api/checkin/active_day.py` | ACP 会话创建、SSE 消费、JSON-RPC、终态判断、会话清理 |
| `src/qb2api/accounts/schema.py` | `workbuddy_active_days` 表和主键 |
| `src/qb2api/accounts/repo_growth.py` | 每日 claim、成功/失败状态更新、状态读取 |
| `src/qb2api/accounts/repository.py` | 注入 mixin，schema 版本升至 6 |
| `src/qb2api/checkin/growth_automation.py` | `run_active_day()` 编排和错误隔离 |
| `src/qb2api/checkin/service.py` | 传递运行日期和时区 |
| `src/qb2api/checkin/service_execution.py` | 将活跃日动作限制在自动签到成功边界 |
| `src/qb2api/runtime.py` | 注入 repository |
| `src/qb2api/config.py`、`src/qb2api/control/settings.py` | 配置字段和运行设置映射 |
| `src/qb2api/admin/settings_routes.py`、`frontend/src/pages/SettingsPage.vue` | 管理台设置 `growth.auto_active_day` |
| `.env.example` | `GROWTH_AUTO_ACTIVE_DAY=true` 示例 |

触发矩阵：

| 场景 | 积分签到 | 成长任务自动化 | ACP 活跃日 |
| --- | --- | --- | --- |
| `scheduler` | 是 | 可执行 | 执行 |
| `catch_up` | 是 | 可执行 | 执行 |
| `manual` | 是 | 不触发成长副作用 | 不执行 |
| `verify` | 只做验证 | 不触发成长副作用 | 不执行 |
| Qoder | 按 Qoder 流程 | 不执行 WorkBuddy 自动化 | 不执行 |
| 环境变量账号 | 只读/按现有规则 | 不进入自动化 | 不执行 |

成长自动化异常只返回安全状态或错误类型，不会把签到批次改成失败。ACP 活跃日失败也不会阻断积分签到结果。

## 9. 每日幂等设计和失败取舍

`workbuddy_active_days` 使用以下组合主键：

```text
provider + account_id + local_date + timezone
```

claim 时通过 SQLite 唯一约束和 `ON CONFLICT DO NOTHING` 原子预留：

```text
首次调用：running → 允许创建一次正式 ACP 会话
并发/重复调用：不再创建会话，返回 already_claimed
ACP 完成：running → succeeded
ACP 异常：running → failed + 安全 error_code
```

当前策略是“失败当天不自动重试”。原因是 ACP 的网络失败可能发生在服务端已经接受 prompt 之后；盲目重试可能重复产生真实模型调用或积分消耗。若以后要允许重试，应先增加明确的失败可重试分类、次数上限和状态查询，不能直接删除唯一约束。

本地 `succeeded` 的语义也必须准确：它只代表客户端收到正式回合结束事件，不代表客户端已经读取并确认成长中心的最终 heatmap。最终是否点亮仍以 WorkBuddy 服务端的 `heatmap`/`streak` 查询为准。

## 10. 安全边界

- access token 只从已有凭据解密结果传入内存，不写入源码、日志、SQLite、审计或前端持久化。
- 日志只允许记录账号上下文、状态和错误类型，例如 `rpc_timeout`、`http:401`；不记录 Authorization、Cookie、上游响应正文或完整会话 link。
- ACP 的 `Acp-Connection-Id`、`acp-session-token` 和临时 session token 只在请求生命周期内使用。
- 不接受不可信输入拼接 shell 命令或 SQL；日期、时区和账号键由现有签到上下文传入，数据库写入使用参数化查询。
- 只有 CodeBuddy 的持久化账号进入 WorkBuddy ACP；Qoder 和环境变量账号不会误用 CodeBuddy token。
- `session/prompt` 发送的文本固定为低成本、无文件读写能力的“你好”；initialize 明确关闭文件读写能力。

## 11. 当前验证证据

代码测试覆盖：

- SSE `data:` JSON 解析和跨 chunk 分片。
- `initialize`、`session/new`、`session/set_model`、`session/prompt` 方法顺序。
- `202` 空响应后继续等待 SSE 终态。
- `acp-session-token` 和 `Acp-Connection-Id` 传递。
- 终态后 DELETE session link。
- 每日 claim 幂等、失败状态保存和安全错误码。
- scheduler/catch_up 触发，manual/verify/Qoder 不触发。
- schema 版本 6 迁移。

历史工作区验证曾达到后端 `334 passed`、前端 `39 passed`，并通过 Ruff、compileall、代码限制检查和 `git diff --check`。本次补充 `acp-session-token` 断言和研究文档后，必须以当前工作区重新执行门禁；历史数字不能替代本轮输出。

真实上游 A/B 验证已经证明协议能改变成长中心状态，但当前自动化测试不再调用真实账号，只使用脱敏 MockTransport，避免测试重复消耗账号资源。

## 12. 还没有验证、因此没有声称完成的部分

1. 当前进程实际运行后，定时签到是否在用户账号当日成功执行，需要查看运行日志和 `workbuddy_active_days` 记录。代码测试不能替代运行实例检查。
2. ACP 回合结束后，成长中心 heatmap 的最终落库存在服务端异步延迟；本地 `succeeded` 不等于立即读取到 `today.is_active=true`。
3. 上游协议若改变 session link、响应头或终态事件名称，需要重新做真实网页抓包；本客户端只兼容当前已验证的字段。
4. 失败重试、ACP 失败后的服务端状态查询、成长中心历史展示尚未实现。

## 13. 后续可选增强（本次刻意不扩张）

### 低风险

- 在签到结果详情中展示最近一次 `active_day` 状态、日期和安全错误码。
- 增加一个只读的 `workbuddy_active_days` 历史接口，便于确认“签到成功但成长活跃失败”的差异。
- 成功后延迟读取一次 `streak`/`heatmap`，只做观测，不把查询失败当成签到失败。

### 需要额外设计

- 失败后的同日有限重试：需要服务端幂等/状态查询，否则可能重复真实对话。
- 独立成长调度器：会引入新的触发、锁和进程生命周期，当前签到成功后的挂钩已满足需求，不应为“以后可能需要”提前重构。
- 完整 GrowthPage、Buddy 旅行和兑换 UI：属于另一个产品范围，本次只落盘协议和自动化研究，没有实现页面。

## 14. 研究资料和仓库边界

已保留的长期研究文档：

- `docs/analysis/2026-08-04-workbuddy-growth-center.md`：成长中心 API、任务生命周期和鉴权。
- `docs/analysis/2026-08-04-growth-heatmap-lottery.md`：热力图、连续天数、抽奖、补登卡和兑换风险。
- 本文：正式 ACP 活跃日协议、真实 A/B 证据、代码接入边界和验证限制。

逆向 bundle 中有协议价值的材料已归档到 `docs/analysis/archive/workbuddy-reverse/`，并附带 SHA-256 清单；导入壳、无关项目服务和产品配置包装已删除。归档内容不是产品代码，不参与构建、测试或运行时加载。`docs/growth-page-refactor-plan.md` 和 `docs/issues/` 是旧研究遗留方案，也不代表本次已经承诺实现。

## 15. 当前本机运行快照（2026-08-06）

为区分“代码已经写好”和“运行实例已经执行”，我又检查了当前工作区的 `data/qb2api.sqlite3`：

| 检查项 | 当前结果 | 含义 |
| --- | --- | --- |
| `schema_meta.schema_version` | `5` | 这份运行数据库还没有应用本次新增的 schema 6 |
| `checkin_runs` 的 2026-08-05 记录 | `catch_up / finished` | 当天积分签到批次确实完成 |
| 2026-08-05 CodeBuddy 签到 | 4 个账号均为 `CLAIMED` | 积分签到成功，不等于成长活跃已记账 |
| `workbuddy_active_days` 表 | 当前不存在 | 新代码尚未在这份旧运行库上完成迁移 |
| 当前 2api 进程 | 未发现运行中的 `qb2api`/uvicorn 进程 | 没有正在执行新代码的服务实例 |

因此截至本次研究收尾，不能声称“今天已经通过新自动化增加了连续活跃天数”。准确状态是：真实账号已经验证 ACP 协议本身能够使 `active=false → true` 和 `streak_days=1 → 2`；实现、schema 迁移、生命周期接入和测试已经落盘；但要让当前本机账号池实际使用它，还需要用新代码启动一次服务，让 schema 6 迁移生效，并观察 `workbuddy_active_days` 和成长中心 `heatmap/streak`。这一步涉及真实上游副作用，本次没有擅自触发。

## 16. 最终判断

要让“积分签到成功”同时带来“成长中心连续活跃天数增加”，不能继续重复调用积分签到接口，也不能伪造本地 usage-log。最小、已被真实账号验证的闭环是：

```text
CodeBuddy 自动签到成功
        ↓
同一账号创建一次正式 WorkBuddy ACP 对话
        ↓
等待 session_end_turn / session/endTurn
        ↓
本地记录当日 ACP 已执行（幂等）
        ↓
以成长中心 heatmap/streak 作为最终结果依据
```

这就是本次研究真正落盘的核心收获：业务含义已拆开，错误方案有实测排除证据，正式协议有可复现流程，代码触发边界和安全边界明确，当前运行库的迁移状态也已核对，剩余未知项没有被伪装成已完成。

## 17. 2026-08-07 更新：登录自动化与签到解耦

本文 §8 的触发矩阵和 §13 的"独立成长调度器"边界已被本次改动替换：**活跃日（登录自动化）从签到批次中拆出，成为成长中心一个独立自动化步骤，由独立的 GrowthScheduler 调度。**

现状与改动：

| 项 | 之前 | 现在 |
| --- | --- | --- |
| 活跃日触发 | 仅挂签到批次成功钩子（trigger=scheduler/catch_up 且当次 CLAIMED） | 独立步骤 `active_day`，进 GrowthScheduler 循环（启动即跑 + 每 `growth.scheduler_interval_seconds` 间隔，幂等一天一次） |
| 与签到关系 | 手动签到会抢占自动签到 → 活跃日在手动签到当天永不执行 | 完全解耦，不再依赖签到结果或触发类型 |
| 开关 | `growth.auto_active_day`（只影响签到钩子） | 同样开关，作用于调度/执行全部步骤 |
| 管理台 | 成长页无卡片 | GrowthPage 新增"登录自动化"卡片；`growth/execute` 与 `growth/run/{step}` 均带日期/时区上下文 |
| 调度定时 | — | 仍沿用成长调度共享间隔；因幂等 claim，'一天一次'不需 per-task 定时器 |

触发语义（更新版触发矩阵）：

| 场景 | ACP 活跃日 |
| --- | --- |
| GrowthScheduler tick（含程序启动后的首轮） | 执行；每日首次 tick 发起 ACP，其余 tick 幂等跳过 |
| 成长页手动"执行全部" | 按 `growth.auto_active_day` 开关 |
| 成长页手动单步 active_day | 强制执行（不受开关限制，与其他步骤一致） |
| 签到（任意 trigger） | 不再影响活跃日 |

关键不变量保持原文 §9：`workbuddy_active_days` 唯一键 + `ON CONFLICT DO NOTHING` 原子预留，重复调用返回 `already_claimed`，绝不重复创建 ACP 会话。移除签到钩子的回归测试见 `tests/integration/test_checkin_service.py::test_scheduled_checkin_does_not_claim_active_day`。

## 18. 2026-08-07 更新：前置检查 + 后置确认 + 手动重跑

针对"本地 ACP succeeded 但上游未记账"的暴露出的空白（今天 cb-d5352301964b 实际是上游异步延迟，约 40 分钟后补记），补充了三段式机制（schema 升至 7，`workbuddy_active_days` 增加 `confirmed`/`confirmed_at`/`confirm_attempts` 列）：

| 机制 | 行为 | 代码落点 |
| --- | --- | --- |
| 前置检查 | 当天首次 claim 时，若成长中心 `today.is_active`/当日 score>0 已被外部点亮，则记 `skipped_external`（confirmed=lit）并跳过 ACP，不消耗调用 | `growth_automation.run_active_day`（复用 run() 已拉取的 overview） |
| 后置确认 | 每次 GrowthScheduler tick，对 `status=succeeded` 且未确认的当天记录拉取 overview：点亮→`confirmed=lit`；未点亮→累加 `confirm_attempts`，达到 `growth.active_day_confirm_attempts`（默认 3，`GROWTH_ACTIVE_DAY_CONFIRM_ATTEMPTS`）仍无→`confirmed=not_lit` | `growth_automation.confirm_active_day` + `growth_scheduler._run_for_account` |
| 手动重跑 | `POST /accounts/{provider}/{account_id}/growth/active-day/rerun`：绕过幂等锁强制再发一次 ACP（真实扣费），重写当天结果为 succeeded/failed 并重置确认状态；前端成长页 login 卡片显示"今日:已点亮/未点亮"与"今日重试"按钮 | `admin/account_routes.py::growth_active_day_rerun`、`growth_automation.rerun_active_day`、`repo.replace_workbuddy_active_day_result` |

行为边界：
- 本地幂等锁仍是一天最多一次自动 ACP 的硬闸；前置检查只在"当天尚无任何本地尝试"时读取上游，滞后窗口不会诱发重复调用。
- 后置确认是有上限的观测：达到尝试上限标记 `not_lit`，不再无限拉取上游。
- 手动重跑是唯一绕过当日锁的入口，必须由管理员显式发起。

验证：schema v7 迁移（含旧库 `_ensure_column`）、前置跳过、确认走向（lit/pending/not_lit）、强制重跑与前端卡片均已纳入测试；今日 4 账号经重启后全部 `confirmed=lit`，含先前"未记账"的 cb-d5352301964b（上游延迟 40 分钟补记）。
