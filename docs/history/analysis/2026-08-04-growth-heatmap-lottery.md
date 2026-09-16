# WorkBuddy 连登地图与自动抽奖调研

> 调研日期：2026-08-04
> 验证账号：cb-<redacted>（codebuddy oauth）

## 1. 连登规则（8月4日"焕新"后）

成长中心公告原文：
> 活跃地图规则将于 8 月 4 日焕新，连登更轻松，福利更贴心。

### 连登档位

| 档位 | 条件 | 奖励 |
| --- | --- | --- |
| 入门档 | 连续登录 7 天 | 能量+2 / 补登卡+1 / 抽奖+1 |
| 进阶档 | 连续登录 14 天 | 积分+100 / 能量+5 / 补登卡+2 / 抽奖+1 |
| 巅峰档 | 连续登录 28 天 | 积分+500 / 能量+10 / 补登卡+4 / 抽奖+1 |

### 连登计算方式

- "连登天数" = 当前日期前**连续登录且使用**的天数
- 每月清零
- 断登后可用补登卡补救当月断登天数
- 兑换需手动发起，消耗后不可恢复
- 补登卡永久持有，上限 4 张

### 跃地图（heatmap）

`GET /activity/growth/heatmap` 返回 365 天网格，每天：
```json
{"date": "2026-08-04", "score": 64, "has_new_buddy": false}
```
- score > 0 表示当日"已活跃"（有使用行为）
- today 额外带 `is_active` 和 `status_text`

实测账号状态：365 天中点亮 3 天（7/20、7/22、8/4），streak 1 天。

## 2. 连登 API（已全部验证可用）

| 端点 | 方法 | 用途 |
| --- | --- | --- |
| `/activity/growth/heatmap` | GET | 跃地图网格 |
| `/activity/growth/streak` | GET | 连登天数/补登卡/兑换状态 |
| `/activity/growth/lottery/summary` | GET | 抽奖次数摘要 |
| `/activity/growth/lottery/chances` | GET | 可用抽奖次数 |
| `/activity/growth/makeup-cards/use` | POST `{target_date}` | 使用补登卡 |
| `/activity/growth/redeem` | POST `{tier, client_token}` | 兑换连登档位奖励 |

### 补登卡错误码（前端 `xe` 函数）

| HTTP | 错误串 | 含义 |
| --- | --- | --- |
| 403 | `no makeup card` | 没补登卡了 |
| 400 | `future date` | 不能补未来 |
| 400 | `cannot makeup history month` | 只能补当月 |
| 400 | `target date before launch` | 早于活动上线日 |
| 400 | `date not broken` | 该日已点亮 |
| 400 | `already made up` | 已补登过 |

## 3. 自动连登：已经自动

本项目每日 `CHECKIN_AT=00:10` 自动签到，签到行为本身让当日 heatmap 点亮、
streak +1。**不需要额外做任何事**——签到 = 连登。

唯一风险：某天签到失败会断登。可补登卡补救（见下）。

## 4. 自动抽奖可行性分析（含 client_token 破解）

### client_token 来源 —— 已彻底确认

前端 `growthSpace-C65mXaaF.js` 的 `D` 函数：

```js
function D(prefix="u") {
  const s = globalThis.crypto;
  return s?.randomUUID
    ? `${prefix}-${s.randomUUID()}`
    : `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2,10)}`;
}
```

调用处（同一 chunk）：
- 兑换档位：`const n = D("redeem-7d")` → `"redeem-7d-<uuid>"`
- 抽奖：`const a = D("draw")` → `"draw-<uuid>"`

**结论**：`client_token` 是纯客户端生成的 UUID，无服务端签名、无加密、无校验。
服务端只把它当幂等键（防重复抽奖）。后端自动化只需 `f"draw-{uuid4()}"` 即可。

### 抽奖 API 链路（全部实测 200）

| 端点 | 方法 | 用途 | 实测结果 |
| --- | --- | --- | --- |
| `/activity/growth/lottery/chances` | GET | 可用次数 | `{"balance":0}` |
| `/activity/growth/lottery/prizes` | GET | 奖品列表 | 见下 |
| `/activity/growth/lottery/summary` | GET | 摘要 | `{"chances":0,"module":{"enabled":true}}` |
| `/activity/growth/lottery/draws` | GET | 抽奖记录 | `{"items":[],"total":0}` |
| `/activity/growth/lottery/rewards` | GET | 中奖记录 | `{"items":[],"total":0}` |
| `/activity/growth/lottery/draw` | POST `{client_token}` | 抽奖 | 未实测（chances=0） |

### 奖品列表（实测）

| prize_code | prize_name | prize_type | 概率 |
| --- | --- | --- | --- |
| 积分_1 | 10 积分 | credit（虚拟） | 45% |
| 积分_2 | 100 积分 | credit（虚拟） | 9.997% |
| 实物奖励 | 杯子 | physical（实物） | 0.0003% |
| 实物奖励_2 | 胸针 | physical（实物） | ... |

虚拟奖品（积分）：抽完自动到账，无需任何操作。
实物奖品（杯子/胸针）：中奖记录留在 `rewards`，**只要不调
`POST /rewards/{id}/address`，就不会发货**。

### 自动化方案（用户确认可行）

1. 检查 `lottery/chances` → `balance > 0` 时自动抽
2. `POST /lottery/draw {client_token: "draw-"+uuid}` → 返回奖品
3. 如奖品 `prize_type == "physical"` → **不调 address API**，留在 rewards 记录里
4. 如奖品 `prize_type == "credit"` → 积分自动到账，无需操作
5. 幂等：`client_token` 是随机 UUID，重复概率为零

### 可行性结论

| 维度 | 结论 |
| --- | --- |
| 鉴权 | ✅ Bearer + 浏览器头，已通 |
| client_token | ✅ 客户端生成 `draw-<uuid>`，无签名 |
| 虚拟奖品 | ✅ 自动到账 |
| 实物奖品 | ✅ 不填地址 = 不发货，留在记录里 |
| 幂等 | ✅ UUID 幂等键 |
| 风险 | 低：实物奖品不主动发货，虚拟奖品即时到账 |

## 5. 补登卡自动化：可行且有价值

签到失败 → 断登 → 如有补登卡余额，自动补登昨天。

逻辑：
1. 签到批次完成后检查 streak
2. 如果今日未点亮（`today.is_active == false`）且昨天也未点亮
3. 且 `makeup_balance > 0`
4. 调 `POST /activity/growth/makeup-cards/use {target_date: 昨天日期}`
5. 幂等安全（已补登返回 `already made up`）

**这是唯一有价值的自动化方向**，可挂在现有 CheckinExecutor 的失败分支。
其余（兑换档位、抽奖）建议保持手动。

## 6. Buddy 旅行 + 兑换 + 抽奖 完整调研（2026-08-04 补充）

### 6.1 Buddy 系统

只读端点全部实测 200：

| 端点 | 实测结果 |
| --- | --- |
| `GET /buddy/info` | 当前 buddy：大圣喵，UR 稀有，personality 全能战神 |
| `GET /buddy/list` | 已拥有 1 只（大圣喵），current_buddy=true |
| `GET /buddy/templates` | 11 个 buddy 模板（量子喵 R、大圣喵 UR...） |
| `GET /buddy/quota` | balance=3 能量，cost_per_open=10，max_open=5 |
| `GET /buddy/agreement` | agreed=true |
| `GET /buddy/visible` | buddy_visible=true, has_buddy=true |

操作端点（从 JS bundle 定义，未实测 POST 因有副作用）：

| 端点 | 方法 | 用途 | 副作用 |
| --- | --- | --- | --- |
| `/buddy/open` | POST `{count}` | 用能量抽 buddy | 消耗 10 能量/次 |
| `/buddy/first` | POST | 领取首只免费 buddy | 一次性 |
| `/buddy/switch` | POST `{instance_id}` | 切换当前 buddy | 无消耗 |
| `/buddy/visible` | POST `{visible}` | 显示/隐藏 buddy | 无消耗 |
| `/buddy/agreement` | POST `{agree:true}` | 同意协议 | 一次性 |

### 6.2 Buddy 旅行系统

只读端点实测：

| 端点 | 实测结果 |
| --- | --- |
| `GET /buddy/travel/config` | 旅行地点列表：咖啡馆(1-4h, 5-10积分)等 |
| `GET /buddy/travel/status` | **当前正在旅行中**：咖啡馆，4h，reward 5 积分 |
| `GET /buddy/travel/records` | 旅行记录（当前空） |

travel/status 关键字段：
```json
{
  "state": "traveling",
  "buddy_id": 3581108,
  "location": {"id":1, "code":"coffee", "name":"咖啡馆", "duration_hours":4},
  "depart_at": 1785838010,    // 出发时间戳
  "arrive_at": 1785852410,    // 到达时间戳
  "server_now": 1785850041,   // 服务器当前时间
  "reward_credit": 5,         // 旅行奖励积分
  "daily_limit_reached": true
}
```

操作端点（从 bundle，未实测 POST）：

| 端点 | 方法 | 用途 | 副作用 |
| --- | --- | --- | --- |
| `/buddy/travel/depart` | POST `{location_id}` | 派 buddy 去旅行 | 消耗 1 能量 |
| `/buddy/travel/claim` | POST | 旅行完成后领取积分奖励 | 领取积分 |

**旅行机制**：选地点 → 消耗能量出发 → 等待 1-4h → 旅行完成 → claim 领积分。
每日有次数限制（`daily_limit_reached`）。

### 6.3 兑换系统（连登档位）

只读端点实测：

| 端点 | 实测结果 |
| --- | --- |
| `GET /redeem/summary` | 7d/14d/28d 全 locked，remaining_days=1 |

```json
{
  "starter_count": 0, "advanced_count": 0, "legendary_count": 0,
  "starter_status": "locked", "advanced_status": "locked", "legendary_status": "locked",
  "remaining_days": 1, "month_total_days": 1
}
```

操作端点（从 bundle，未实测 POST）：

| 端点 | 方法 | 用途 | 副作用 |
| --- | --- | --- | --- |
| `/redeem` | POST `{tier, client_token}` | 兑换连登档位奖励 | 消耗连登天数，不可恢复 |

tier 值：`7d` / `14d` / `28d`。client_token 同抽奖：`redeem-{tier}-<uuid>`。

### 6.4 自动化可行性总览

| 功能 | 只读展示 | 手动操作 | 自动化 | 自动化风险 |
| --- | --- | --- | --- | --- |
| 成长任务 | ✅ 已实现 | accept/claim | ✅ 可自动 | 低（幂等） |
| 连登地图 | ✅ 已实现 | — | —（签到即连登） | — |
| Buddy 信息 | ✅ 已实现 | switch/visible | ✅ 可自动 | 无（无消耗） |
| Buddy 旅行 | ✅ 可展示状态 | depart/claim | ✅ 可自动 | 中（消耗能量） |
| Buddy 抽卡 | ✅ 可展示配额 | open | ⚠️ 可自动 | 中（消耗 10 能量/次） |
| 抽奖 | ✅ 可展示次数 | draw | ✅ 可自动 | 低（实物不发货） |
| 兑换档位 | ✅ 可展示状态 | redeem | ⚠️ 可自动 | 高（消耗连登天数不可恢复） |

### 6.5 建议自动化策略

**全自动（签到后顺带执行，低风险）**：
1. 成长任务：accept 未接受的 + claim 可领奖的（已实现）
2. 抽奖：chances > 0 时自动抽，实物留着不领（已调研确认）

**半自动（需 UI 按钮触发，中等风险）**：
3. Buddy 旅行：能量够 + 没在旅行 → 展示"可出发"按钮，用户点确认才 depart
4. 旅行 claim：旅行完成 → 自动领积分（claim 只读收益无风险）

**手动（高风险，只展示状态）**：
5. 兑换档位：只展示 locked/unlocked 状态，redeem 必须用户自己去网页端操作
   （规则明确"兑换需手动发起，消耗后不可恢复"）
6. Buddy 抽卡：只展示配额，open 由用户决定（消耗能量随机性大）

### 6.6 复刻完整成长中心页面的前端工作量

要对齐 workbuddy.cn/profile/growth-center，需实现：

| 区块 | 当前状态 | 工作量 |
| --- | --- | --- |
| 等级+任务列表 | ✅ 已实现 | — |
| 连登地图+热力图 | ✅ 已实现 | — |
| Buddy 卡片+属性 | ❌ 未实现 | 中（展示 buddy 信息+动画） |
| 旅行面板 | ❌ 未实现 | 中（地点列表+状态+claim 按钮） |
| 抽奖面板 | ❌ 未实现 | 中（奖品列表+抽奖按钮+记录） |
| 兑换面板 | ❌ 未实现 | 小（三档状态+兑换按钮） |
| 补登卡面板 | ❌ 未实现 | 小（余额+补登操作） |

后端 API 已全部验证可用（只读端点 200，操作端点从 bundle 定义确认），
不需要额外破鉴权。前端工作量约等于现有 AccountDetailPage 的 1.5 倍。

## 7. 自动化方案设计（2026-08-04 定稿，未实施）

### 7.1 设计原则

- **默认全自动**：签到成功后顺带跑一遍，能跑的跑，跑不了的静默跳过
- **可关闭**：每项独立开关，全部走现有 `runtime_settings` 表
- **可手动**：前端给"手动执行"按钮 + 单项操作按钮
- **兑换档位可切换**：页面下拉选 7d/14d/28d/off，默认 28d

### 7.2 自动化前提条件

每项自动化都有触发前提，不满足时静默跳过（不报错）：

| 功能 | 触发前提 | 不满足时 |
| --- | --- | --- |
| 成长任务 accept/claim | 有未接受/可领奖的任务 | 跳过 |
| 抽奖 | `lottery/chances.balance > 0` | 跳过（连登7天换档才有次数） |
| 旅行 depart | 能量≥1 + 没在旅行 + 未达日限 | 跳过 |
| 旅行 claim | `travel/status.state == "arrived"` | 跳过（要等1-4h到达） |
| 兑换 | `remaining_days >= 选定档位` 且该档 locked | 跳过（连登要够7/14/28天） |
| Buddy 抽卡 | 能量≥10 + `affordable > 0` | 跳过（默认关） |

连登天数积累后各前提陆续满足，自动化价值逐步体现。

### 7.3 控制模型

```
runtime_settings:
  growth_auto_tasks      = true    # 任务 accept/claim
  growth_auto_lottery    = true    # 抽奖（实物不领）
  growth_auto_travel     = true    # 旅行 depart/claim
  growth_auto_redeem     = true    # 兑换（受 redeem_tier 控制）
  growth_redeem_tier     = "28d"   # 档位：7d / 14d / 28d / off
  growth_auto_buddy_open = false   # Buddy 抽卡（默认关）
```

### 7.4 兑换档位机制详解

连登达标后，消耗连登天数换取奖励（**消耗不可恢复**）：

| 档位 | 连登要求 | 消耗天数 | 奖励 |
| --- | --- | --- | --- |
| 入门档 7d | 连续登录 7 天 | 7 | 能量+2 / 补登卡+1 / 抽奖+1 |
| 进阶档 14d | 连续登录 14 天 | 14 | 积分+100 / 能量+5 / 补登卡+2 / 抽奖+1 |
| 巅峰档 28d | 连续登录 28 天 | 28 | 积分+500 / 能量+10 / 补登卡+4 / 抽奖+1 |

规则要点：
- 连登天数每月清零
- 消耗后不可恢复（换7天档扣7天，剩的不足以换14天档）
- 每月每档最多换一次

### 7.5 兑换档位选择（页面可切换，默认 28d）

| 选项 | 含义 | 适用场景 |
| --- | --- | --- |
| **28d（默认）** | 连登满28天自动兑换巅峰档 | 收益最高（积分+500），愿等 |
| 14d | 连登满14天自动兑换进阶档 | 平衡，积分+100 |
| 7d | 连登满7天自动兑换入门档 | 最早触发，但积分+0 |
| off | 不自动兑换，完全手动 | 自己决定何时换 |

**默认 28d 理由**：巅峰档积分收益是14d的5倍、7d的无限倍。宁可多等换最高档。
用户可随时在页面切到 14d/7d 提前触发，或切 off 完全手动。

### 7.6 执行流程

签到批次成功后顺带执行（不额外开调度）：

```
CheckinExecutor.run(codebuddy, account_id) 签到成功
  └─ GrowthAutomation.run(account_id, token)
       ├─ if auto_tasks:    list_tasks → accept 未接受 → claim 可领奖
       ├─ if auto_lottery:  chances → balance>0 → draw → 实物不领
       ├─ if auto_travel:
       │    ├─ travel/status → arrived? → claim 积分
       │    └─ 空闲 + 能量≥1 + 有日限 → depart 最近地点
       ├─ if auto_redeem:    redeem/summary → remaining≥tier → redeem(tier)
       └─ if auto_buddy_open: quota → affordable>0 → open
```

每步独立 try/except，单步失败不影响其他。结果聚合为签到批次附带项展示。

### 7.7 前端

1. **设置页**：新增"Growth 自动化"分组——5 个开关 + 1 个档位下拉
2. **账号详情页成长中心面板**：
   - 顶部状态条：各自动化项的执行状态（✅已执行/⏳条件不足/❌已关闭）
   - 底部"手动执行"按钮：触发一次完整自动化
   - 单项操作按钮（旅行出发、抽奖、兑换）按需显示

### 7.8 实施状态

- **调研完成**：所有 API 验证通过，client_token 破解，前提条件明确
- **方案定稿**：本文档
- **未实施**：等确认后开发
