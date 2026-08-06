# Growth Page 重构计划

> 创建于 2026-08-05
> 状态：决策点已全部确认，可开始实施

## 背景

AccountDetailPage（489 行，逼近 500 行硬上限）承载过多职责：账号管理、积分明细、成长中心、连登地图全挤在一个页面。成长自动化执行结果仅展示一行压缩字符串（如 `任务 accepted:0 claimed:12 抽奖 no_chances 旅行 daily_limit_reached 兑换 insufficient:1/28 Buddy skipped`），信息量极低且无法单步控制。

### 当前自动化实现的问题

当前 growth 自动化**不是独立调度**，而是**签到成功后顺带执行一次**：

```
签到调度器 (每天定时)
  → CheckinBatchExecutor.execute()
    → 对每个 codebuddy 账号签到
    → 签到成功后调用 _run_growth(target)  ← growth 绑死在这里
      → GrowthAutomation.run(token)  → 5 个步骤各受全局开关控制
```

问题：
1. 一天只跑一次（跟签到绑定），无法及时响应（如抽奖次数白天有了也得等签到时才抽）
2. 用户完全看不到执行过程和结果
3. 开关埋在 SettingsPage 里，无法按步骤手动触发
4. 签到失败时 growth 也不会执行

## 核心痛点

1. **AccountDetailPage 过载** - 489 行，积分明细 + 成长中心 + 连登地图全挤在账号详情页
2. **自动化展示极差** - `growth/execute` 返回 `dict[str, str]`，前端只展示一行压缩字符串
3. **缺乏手动控制** - 5 个自动化开关只在 SettingsPage 全局配置，无法按步骤手动触发
4. **成长中心孤立** - growth 数据只在 AccountDetailPage 内嵌展示，没有独立入口
5. **积分明细位置不合理** - 积分明细埋在账号详情页里，与积分监控割裂
6. **自动化绑死签到** - growth 自动化跟签到耦合，一天只跑一次，无法独立调度

## 决策点（已全部确认）

1. ✅ **积分明细处理**：方案 D - 积分明细移入 CreditsPage，点击账号展开积分包详情，AccountDetailPage 不再展示任何积分/配额相关内容
2. ✅ **自动化开关**：每个功能独立开关（自动任务/自动抽奖/自动旅行/自动兑换/自动Buddy），在 GrowthPage 前端可直接切换，同时支持手动单步触发
3. ✅ **历史记录**：本轮做（Wave 4）
4. ✅ **导航位置**：放"自动化"组，和签到并列
5. ✅ **解耦签到**：签到后不再触发 growth 自动化，growth 有自己的独立调度器

## 目标

| # | 目标 | 解决的痛点 |
|---|------|-----------|
| A | 新建 `GrowthPage.vue` 独立菜单页，复刻 WorkBuddy 成长中心 UI | 1, 4 |
| B | 从 `AccountDetailPage` 移除成长中心 + 连登地图 + 积分明细 + 积分与配额 | 1 |
| C | 重构自动化结果展示 - 从一行字符串变为结构化卡片 | 2 |
| D | 每个自动化功能独立开关 + 手动单步触发 | 2, 3 |
| E | 支持按账号查看 growth overview，多账号切换 | 4 |
| F | 积分明细移入 CreditsPage，点击账号展开积分包详情 | 5 |
| G | 解耦 growth 自动化与签到，growth 独立调度 | 6 |
| H | 自动化执行历史记录 | 2 |

## 整体架构变更

```
前端导航变化:
  "账号池" 组
    ├ 账号
    ├ 积分监控（增强：点击账号展开积分包详情）
    ├ 凭据
  "自动化" 组
    ├ 签到
    └ 成长中心 ← 新增

AccountDetailPage 瘦身:
  移除: 成长中心 section (line 385)
  移除: 连登地图 section (line 386)
  移除: 积分明细 section (line 384, points-detail-section)
  移除: 积分与配额 section (line 383, detail-main-grid 左侧)
  保留: 账号操作、用途与路由、凭据元数据、请求历史、签到历史
  新增: "查看成长中心 ->" / "查看积分明细 ->" 跳转链接

CreditsPage 增强:
  现有: 账号列表表格 + 积分变化曲线
  新增: 点击账号行 -> 展开该账号的积分包详情面板

后端自动化架构变化:
  移除: CheckinBatchExecutor 中签到后触发 _run_growth 的逻辑
  新增: GrowthScheduler - 独立调度器，定期检查并执行各自动化步骤
  新增: growth_automation_log 表 - 记录每次执行结果
  新增: 按步骤手动触发 API
  重构: GrowthAutomation.run() 返回结构化结果（非压缩字符串）
```

## Wave 1：后端 - 解耦 + API 扩展

### 1.1 解耦 growth 自动化与签到

**`src/qb2api/checkin/service_execution.py`**：
- 移除 `growth_runner` 参数和 `_run_growth` 方法
- 移除 `execute()` 中签到成功后调用 `_run_growth` 的逻辑（line 63-69）
- 移除相关 import

**`src/qb2api/checkin/service.py`**：
- 移除 `growth_automation` 参数和 `_run_growth` 方法
- 移除 `growth_runner` 传给 `CheckinBatchExecutor` 的逻辑
- 移除 `GrowthAutomation` import 和 `close()` 中的清理

### 1.2 新建 GrowthScheduler 独立调度器

**`src/qb2api/checkin/growth_scheduler.py`**（新建）：

对标 `CheckinScheduler` 的设计，但有关键差异：
- 调度频率更高（如每 30 分钟检查一次，可配置），而非一天一次
- 对每个 codebuddy 非环境变量账号独立执行
- 每个步骤受独立开关控制
- 执行结果写入 `growth_automation_log`

```python
class GrowthScheduler:
    """独立调度 growth 自动化，与签到解耦。"""

    def __init__(
        self,
        settings: Settings,
        automation: GrowthAutomation,
        registry: AccountRegistry,
        resolver: CredentialResolver,
        repo: AccountRepository,
    ) -> None: ...

    async def _loop(self) -> None:
        """每 interval 秒遍历所有 codebuddy 账号，执行已开启的自动化步骤。"""

    async def _run_for_account(self, provider: str, account_id: str) -> None:
        """单个账号的自动化执行：解析 token -> run -> 写日志。"""
```

新增配置项（`config.py`）：
- `growth_scheduler_enabled`（默认 true）- 是否启用独立调度
- `growth_scheduler_interval_seconds`（默认 1800，即 30 分钟）- 调度间隔

### 1.3 重构 `growth_automation.py` 返回结构

当前 `run()` 返回 `dict[str, str]`（压缩字符串），改为结构化：

```python
# 新返回结构
{
    "tasks": {
        "status": "completed",       # completed | skipped | failed | partial
        "accepted": 0,
        "claimed": 12,
        "detail": "领取了 12 个已完成任务的奖励",
    },
    "lottery": {
        "status": "no_chances",
        "drawn": 0,
        "available": 0,
        "detail": "暂无抽奖次数",
    },
    "travel": {
        "status": "daily_limit_reached",
        "detail": "今日旅行次数已用完",
    },
    "redeem": {
        "status": "insufficient",
        "remaining_days": 1,
        "required_days": 28,
        "detail": "连登 1/28 天，还差 27 天可兑换巅峰档",
    },
    "buddy_open": {
        "status": "skipped",
        "detail": "Buddy 自动开启未启用",
    },
}
```

同时支持按步骤单独执行：

```python
async def run_step(self, access_token: str, step: str) -> dict[str, Any]:
    """只执行单个步骤。step ∈ {tasks, lottery, travel, redeem, buddy_open}。"""
```

### 1.4 新增 API 端点

```
POST /accounts/{provider}/{account_id}/growth/run/{step}
  step ∈ {tasks, lottery, travel, redeem, buddy_open}
  -> 只执行单个步骤，返回该步骤的结构化结果

POST /accounts/{provider}/{account_id}/growth/run
  -> 执行所有已启用步骤，返回结构化结果

GET /accounts/{provider}/{account_id}/growth/history?limit=20
  -> 返回最近自动化执行记录

GET /growth/overview
  -> 返回所有 codebuddy 非环境变量账号的 growth 摘要
  -> 用于 GrowthPage 首页列表展示
```

### 1.5 Runtime 集成

**`src/qb2api/runtime.py`**：
- 新建 `GrowthScheduler` 实例（替代原来在 `CheckinService` 中注入 growth_automation 的方式）
- 启动/停止 `GrowthScheduler`
- `GrowthAutomation` 实例归 `GrowthScheduler` 持有

### 涉及文件

| 文件 | 变更 |
|------|------|
| `src/qb2api/checkin/service_execution.py` | 移除 growth 触发逻辑 |
| `src/qb2api/checkin/service.py` | 移除 growth_automation 注入 |
| `src/qb2api/checkin/growth_automation.py` | 重构返回结构 + 新增 `run_step()` |
| `src/qb2api/checkin/growth_scheduler.py` | **新建** 独立调度器 |
| `src/qb2api/config.py` | 新增 scheduler 配置项 |
| `src/qb2api/admin/account_routes.py` | 新增 `/growth/run/{step}` + `/growth/overview` + `/growth/history` 端点 |
| `src/qb2api/control/settings.py` | 新增 settings key mapping |
| `src/qb2api/admin/settings_routes.py` | 新增 settings schema |
| `src/qb2api/runtime.py` | 集成 GrowthScheduler |
| `tests/checkin/test_growth_automation.py` | 更新断言 |
| `tests/integration/test_growth_routes.py` | 新增端点测试 |
| `tests/integration/test_checkin_service.py` | 移除 growth 相关断言 |

## Wave 2：后端 - 历史记录

### 2.1 新增 `growth_automation_log` 表

**`src/qb2api/accounts/schema.py`**：

```sql
CREATE TABLE IF NOT EXISTS growth_automation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    triggered_by TEXT NOT NULL,        -- "scheduler" | "manual" | "manual:tasks" 等
    results TEXT NOT NULL,             -- JSON 序列化的结构化结果
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_growth_log_account
    ON growth_automation_log(provider, account_id, created_at DESC);
```

### 2.2 新增 repo mixin

**`src/qb2api/accounts/repo_growth_log.py`**（新建）：

```python
class GrowthLogMixin:
    async def insert_growth_log(
        self, *, provider: str, account_id: str,
        triggered_by: str, results: dict[str, Any],
    ) -> int: ...

    async def list_growth_logs(
        self, *, provider: str, account_id: str, limit: int = 20,
    ) -> list[dict[str, Any]]: ...
```

**`src/qb2api/accounts/repository.py`**：挂载 mixin

### 2.3 调度器/手动执行后写入日志

- `GrowthScheduler._run_for_account()` 执行后写日志
- `/growth/run` 和 `/growth/run/{step}` 端点执行后写日志

### 涉及文件

| 文件 | 变更 |
|------|------|
| `src/qb2api/accounts/schema.py` | 新增表 + 索引 |
| `src/qb2api/accounts/repo_growth_log.py` | **新建** repo mixin |
| `src/qb2api/accounts/repository.py` | 挂载 mixin |
| `src/qb2api/checkin/growth_scheduler.py` | 执行后写日志 |
| `src/qb2api/admin/account_routes.py` | 手动执行后写日志 + history 端点 |

## Wave 3：前端 GrowthPage 新建

### 设计原则

复刻 WorkBuddy 成长中心（`workbuddy.cn/profile/growth-center`）的 UI 结构，每个自动化功能独立展示、独立开关、独立手动触发。

### 页面布局

```
┌─────────────────────────────────────────────────────────────┐
│ 成长中心                              [账号选择▼] [刷新]     │
│ WorkBuddy 成长计划 · 任务 · 连登 · 抽奖 · 旅行 · 兑换       │
├─────────────────────────────────────────────────────────────┤
│ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐        │
│ │ 等级 5   │ │连登 28天 │ │可抽奖 3次│ │补登卡 2/4│        │  ← 摘要卡片
│ │ 已完成   │ │距巅峰 0天│ │          │ │          │        │
│ │ 18/20    │ │          │ │          │ │          │        │
│ └──────────┘ └──────────┘ └──────────┘ └──────────┘        │
├─────────────────────────────────────────────────────────────┤
│ 自动化控制                                                  │
│ ┌───────────────────────────────────────────────────────┐   │
│ │ 📋 任务自动化                           [开关 ON/OFF]  │   │
│ │ 上次结果: 领取了 12 个奖励 · 接受 0 个任务             │   │  ← 每个功能独立卡片
│ │ 上次执行: 2026-08-05 12:30 (调度器)                    │   │
│ │ [手动执行]                                              │   │
│ ├───────────────────────────────────────────────────────┤   │
│ │ 🎲 抽奖自动化                           [开关 ON/OFF]  │   │
│ │ 上次结果: 暂无抽奖次数                                  │   │
│ │ 上次执行: 2026-08-05 12:30 (调度器)                    │   │
│ │ [手动执行]                                              │   │
│ ├───────────────────────────────────────────────────────┤   │
│ │ ✈️ 旅行自动化                           [开关 ON/OFF]  │   │
│ │ 上次结果: 今日旅行次数已用完                            │   │
│ │ 上次执行: 2026-08-05 12:30 (调度器)                    │   │
│ │ [手动执行]                                              │   │
│ ├───────────────────────────────────────────────────────┤   │
│ │ 🎁 兑换自动化                           [开关 ON/OFF]  │   │
│ │ 上次结果: 连登 1/28 天，还差 27 天可兑换巅峰档         │   │  ← 档位选择 [28d▼]
│ │ 上次执行: 2026-08-05 12:30 (调度器)                    │   │
│ │ [手动执行]                                              │   │
│ ├───────────────────────────────────────────────────────┤   │
│ │ 🐾 Buddy 自动化                        [开关 ON/OFF]  │   │
│ │ 上次结果: 未启用                                        │   │
│ │ 上次执行: --                                            │   │
│ │ [手动执行]                                              │   │
│ └───────────────────────────────────────────────────────┘   │
├─────────────────────────────────────────────────────────────┤
│ 成长任务列表                                                 │
│ ┌───────────────────────────────────────────────────────┐   │
│ │ [图标] 完成每日签到          [已完成] 奖励 5 积分      │   │
│ │        进度 1/1 · 日常任务                              │   │
│ ├───────────────────────────────────────────────────────┤   │
│ │ [图标] 邀请好友              [可领奖] 奖励 50 积分     │   │
│ │        进度 3/3 · 有奖可领                              │   │
│ ├───────────────────────────────────────────────────────┤   │
│ │ [图标] 连续登录30天          [锁定] 需连登 30 天       │   │
│ │        进度 28/30 · 锁定中                              │   │
│ └───────────────────────────────────────────────────────┘   │
├─────────────────────────────────────────────────────────────┤
│ 连登热力图                                                   │
│ ┌───────────────────────────────────────────────────────┐   │
│ │ [██░░█░] [███░░░] [░░░███] [██████░] ...              │   │
│ │ 连登 28 天 · 距巅峰档还差 0 天 · 补登卡 2/4            │   │
│ │ 今日: 已活跃                                             │   │
│ └───────────────────────────────────────────────────────┘   │
├─────────────────────────────────────────────────────────────┤
│ 自动化执行历史                                               │
│ ┌───────────────────────────────────────────────────────┐   │
│ │ 2026-08-05 12:30  调度器                               │   │
│ │ ✅ 任务: 领取12  ⏭️ 抽奖: 无次数  ✅ 旅行: 已出发      │   │
│ │ ⏭️ 兑换: 1/28天  ⏭️ Buddy: 未启用                      │   │
│ ├───────────────────────────────────────────────────────┤   │
│ │ 2026-08-05 10:00  手动:任务                            │   │
│ │ ✅ 任务: 领取8                                          │   │
│ ├───────────────────────────────────────────────────────┤   │
│ │ 2026-08-04 12:30  调度器                               │   │
│ │ ✅ 任务: 领取12  ✅ 抽奖: 抽3次  ✅ 旅行: 已领奖        │   │
│ │ ✅ 兑换: 已兑换28d档                                    │   │
│ └───────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### 核心功能

1. **账号选择器** - 顶部下拉选择 codebuddy 账号
2. **摘要卡片** - 等级/连登天数/抽奖次数/补登卡，复刻 WorkBuddy profile
3. **自动化控制面板** - 5 个独立卡片，每个含：
   - 独立开关（ON/OFF，直接切换，调用 settings API）
   - 上次执行结果（结构化展示，非压缩字符串）
   - 上次执行时间和触发方式（调度器/手动）
   - 手动执行按钮（不受开关状态限制）
   - 兑换卡片额外有档位选择器（7d/14d/28d/off）
4. **成长任务列表** - 复刻 WorkBuddy 任务列表，带图标/状态/进度/奖励
5. **连登热力图** - 复用现有 heatmap 组件
6. **自动化执行历史** - 从 `growth_automation_log` 查询，按时间倒序展示

### 原型设计细节（已验证）

原型已用 mock 数据实现并通过 typecheck/lint/build/code-limits 验证，以下是确认的设计决策：

**组件结构**（GrowthPage.vue ~398 行，在 500 行上限内）：

```
<script setup>
  // 复用现有组件：PanelHeader, StatePill, NotificationRegion, useNotifications
  // 新增图标：Sprout, Sparkles, Dice5, MapPin, Gift, PawPrint, Trophy, CalendarDays

  // Mock 数据类型（后端就绪后替换为 useQuery + apiRequest）
  type StepKey = "tasks" | "lottery" | "travel" | "redeem" | "buddy_open"
  type StepConfig = {
    key: StepKey; label: string; icon: typeof Sprout; enabled: boolean;
    last_result: StepResult | null; last_run_at: string | null; last_triggered_by: string | null;
  }
</script>
```

**设计系统匹配**（完全复用现有 tokens.css）：

| 设计元素 | 使用的 CSS 变量 | 说明 |
|---------|----------------|------|
| 页面背景 | `var(--canvas)` #0b0b0d | 暗底终端风格 |
| 面板背景 | `var(--surface)` #101014 | data-panel 标准底色 |
| 分隔线 | `var(--line)` #26262e | 卡片间靠线分区 |
| 强调色 | `var(--accent)` #e8913a | 琥珀 CRT，开关/图标/等级 |
| 成功状态 | `var(--ok)` #4ec06e | 今日活跃热力图 |
| 文字层次 | `--text` / `--muted` / `--faint` | 三级灰度 |

**自动化卡片布局**（网格自适应）：

```css
.automation-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 1px;          /* 1px gap + 背景色 = 线分区效果 */
  background: var(--line);
}
```

每个卡片内部结构：
```
┌─────────────────────────────────────┐
│ [icon] 任务自动化        [switch]   │  ← header（flex justify-between）
├─────────────────────────────────────┤
│ [StatePill: completed] 领取了12个   │  ← body（结果 + 元数据）
│ 上次执行: 2026-08-05 12:30 · 调度器  │
├─────────────────────────────────────┤
│ [手动执行]              档位: [28d▼]│  ← footer（按钮 + 可选配置）
└─────────────────────────────────────┘
```

**任务列表状态色条**（左侧 border-left 区分状态）：

| 状态 | 色条 | 透明度 |
|------|------|--------|
| completed | 无 | 0.65（淡化） |
| claimable | `var(--accent)` 2px | 正常 |
| locked | 无 | 0.42（更淡） |
| not_accepted | `var(--warn)` 2px | 正常 |

**热力图**（直接复用 AccountDetailPage 的 heatmap 样式）：
- 13 周网格，每列 7 格（周一到周日）
- 4 级色阶：lvl0（空）→ lvl1（25% amber）→ lvl2（45%）→ lvl3（70%）→ lvl4（实色 accent）
- 今日状态高亮条（`.streak-today.active`）

**执行历史布局**（双栏网格）：

```css
.history-entry {
  display: grid;
  grid-template-columns: 200px minmax(0, 1fr);
  /* 左栏：时间 + 触发方式标签 */
  /* 右栏：各步骤结果摘要（✅/⏭️ + 详情） */
}
```

**交互行为**（原型已实现）：

1. **开关切换** - 点击 switch toggle，toast 通知"已开启/已关闭"
2. **手动执行** - 点击按钮后显示"执行中…"（1.5s 模拟延迟），完成后更新 last_result + toast 通知
3. **执行全部** - 顶部按钮，2s 模拟延迟后 toast 通知
4. **账号切换** - 下拉切换（原型中数据不变，实际应触发重新查询）

**路由和导航代码**（已验证可编译）：

```typescript
// router.ts - 在 checkin 和 settings 之间插入
const GrowthPage = () => import("@/pages/GrowthPage.vue");
// ...
{ path: "growth", name: "growth", component: GrowthPage },

// AdminShell.vue - "自动化"组新增项
// import 中新增 Sprout 图标
{
  label: "自动化",
  items: [
    { to: "/checkin", label: "签到", icon: CheckCircle2 },
    { to: "/growth", label: "成长中心", icon: Sprout },
  ],
},
```

### 路由和导航

```typescript
// router.ts
{ path: "growth", name: "growth", component: GrowthPage },

// AdminShell.vue navigationGroups
{
  label: "自动化",
  items: [
    { to: "/checkin", label: "签到", icon: CheckCircle2 },
    { to: "/growth", label: "成长中心", icon: Sprout },
  ],
},
```

### 涉及文件

| 文件 | 变更 |
|------|------|
| `frontend/src/pages/GrowthPage.vue` | **新建** |
| `frontend/src/router.ts` | 添加路由 |
| `frontend/src/layouts/AdminShell.vue` | 添加导航项 |

## Wave 4：AccountDetailPage 瘦身

### 移除内容

从 `AccountDetailPage.vue` 移除：
- **line 385** - 成长中心 section（整个 `growth-section`）
- **line 386** - 连登地图 section（整个 `streak-section`）
- **line 384** - 积分明细 section（整个 `points-detail-section`）
- **line 383 左侧** - 积分与配额 section（`detail-main-grid` 中的 `metricRows` 面板）

### 同时移除相关代码

- growth 相关 type 定义（`GrowthTask`, `GrowthProfile`, `GrowthHeatmap`, `GrowthStreak`, `GrowthLottery`, `GrowthOverview`）
- growth query/mutation（`growth`, `growthExecute`, `lastGrowthResult`）
- growth 辅助函数（`automationLabel`, `growthTaskStatus`, `growthTaskLabel`, `heatmapGrid`, `cellLevel`, `cellTitle`）
- 积分包相关 computed（`creditPackages`, `visibleCreditPackages`, `creditPackagePageCount`）
- 积分明细相关辅助函数（`formatPackageAmount`, `formatExpiry`, `setListPage` 中 packages 分支）
- growth + 积分明细相关 CSS（约 50 行）
- metrics query（如果不再被其他 section 使用则移除）

### 新增

在 AccountDetailPage 添加跳转链接：
- "查看成长中心 ->" 跳到 `/growth?account=codebuddy:{accountId}`
- "查看积分明细 ->" 跳到 `/credits?account={provider}:{accountId}`

### 涉及文件

| 文件 | 变更 |
|------|------|
| `frontend/src/pages/AccountDetailPage.vue` | 大幅瘦身（预计减少 ~200 行） |

## Wave 5：CreditsPage 增强 - 积分包详情下钻

### 交互设计

```
┌──────────────────────────────────────────────────────────┐
│ 积分监控                              [自动刷新▼] [刷新]  │
├──────────────────────────────────────────────────────────┤
│ [筛选条件: 服务提供方 / 账号搜索 / 趋势账号 / 时间窗口]   │
├──────────────────────────────────────────────────────────┤
│ 摘要卡片 (当前积分总量 / 窗口变化 / 最近采集 / ...)       │
├──────────────────────────────────────────────────────────┤
│ 积分变化曲线                                              │
├──────────────────────────────────────────────────────────┤
│ 账号积分表格                                              │
│ ┌──────────┬──────┬──────┬──────┬──────┬──────┐         │
│ │ 账号     │ 服务 │ 积分 │ 变化 │ 时间 │ 状态 │         │
│ ├──────────┼──────┼──────┼──────┼──────┼──────┤         │
│ │ my-acct ▼│ CB   │ 5200 │ +200 │ 12:30│ fresh │ ← 选中  │
│ ├──────────┴──────┴──────┴──────┴──────┴──────┤         │
│ │ ┌─────────────────────────────────────────┐ │         │
│ │ │ 积分包详情 (展开面板)                    │ │         │
│ │ │ ┌─────────┬──────┬──────┬──────┬──────┐ │ │         │
│ │ │ │ 名称    │ 总量 │ 已用 │ 剩余 │ 到期 │ │ │         │
│ │ │ │ 用户积分│ 5000 │ 1200 │ 3800 │ --   │ │ │         │
│ │ │ │ 附加积分│ 1000 │  600 │  400 │ 8/30 │ │ │         │
│ │ │ │ 签到奖励│  200 │    0 │  200 │ 8/15 │ │ │         │
│ │ │ └─────────┴──────┴──────┴──────┴──────┘ │ │         │
│ │ └─────────────────────────────────────────┘ │         │
│ ├──────────┬──────┬──────┬──────┬──────┬──────┤         │
│ │ other    │ QD   │ 8000 │    0 │ 11:00│ fresh │         │
│ └──────────┴──────┴──────┴──────┴──────┴──────┘         │
└──────────────────────────────────────────────────────────┘
```

### 实现要点

1. **复用现有数据** - CreditsPage 已有 `metrics` query，积分包数据可从 metrics snapshot 的 value 中提取
2. **选中账号展开** - 当前点击行已设置 `selectedAccount`（用于趋势图筛选），扩展为同时展开积分包详情面板
3. **提取共享逻辑** - 将 AccountDetailPage 中的 `creditPackages` / `formatPackageAmount` / `formatExpiry` 等逻辑提取为 composable
4. **Qoder/CodeBuddy 差异** - Qoder 用 `user_quota`/`add_on_quota`/`org_resource_package`，CodeBuddy 用 packages 数组

### 涉及文件

| 文件 | 变更 |
|------|------|
| `frontend/src/pages/CreditsPage.vue` | 新增展开面板 |
| `frontend/src/composables/useCreditPackages.ts` | **新建** 提取积分包计算逻辑 |

## Wave 6：前端构建 + 测试

### 验证步骤

```bash
# 前端
cd frontend && npm run typecheck && npm run lint && npm run build
cd frontend && npm run test

# 后端
.venv/bin/pytest tests/checkin/test_growth_automation.py tests/integration/test_growth_routes.py tests/integration/test_checkin_service.py -q
.venv/bin/ruff check src tests
.venv/bin/python tools/check_code_limits.py
.venv/bin/python -m compileall -q src/qb2api
```

### 涉及文件

| 文件 | 变更 |
|------|------|
| `frontend/src/pages/GrowthPage.vue` | 完成 |
| `frontend/src/pages/AccountDetailPage.vue` | 瘦身完成 |
| `frontend/src/pages/CreditsPage.vue` | 积分包详情面板完成 |
| `src/qb2api/web/dist/` | 构建产物更新 |

## 风险与注意事项

1. **解耦是 breaking change** - `CheckinBatchExecutor` 和 `CheckinService` 的构造签名变更，所有调用方需同步更新
2. **GrowthScheduler 调度间隔** - 太频繁会对 WorkBuddy API 造成压力，默认 30 分钟，最小不低于 10 分钟
3. **开关两处入口** - GrowthPage 前端可直接切换开关 + SettingsPage 也能改同一组开关，需确保两边读写一致（同一 settings API）
4. **AccountDetailPage 移除积分后** `detail-main-grid` 布局需调整（原为左右双栏，移除左侧后改为单栏或重新分配）
5. **GrowthPage.vue 行数控制** - 预计 350-450 行，如逼近 500 行上限，可将自动化控制面板和任务列表拆为子组件
6. **CreditsPage 行数** - 当前 191 行，新增积分包面板后预计 280-350 行，在上限内
7. **`growth_automation.py` 返回结构变更** - 前端和测试需同步更新
8. **前端 build 产物必须更新提交** - `dist/` 是构建产物入库的
9. **Scheduler 生命周期** - GrowthScheduler 需在 `RuntimeServices` 中正确启动/停止，settings 变更时需 reconfigure

## 执行顺序

```
Wave 1 (后端解耦+API) ──> Wave 2 (历史记录)
       │
       ├──> Wave 3 (GrowthPage)  ──┐
       ├──> Wave 5 (CreditsPage)  ──┼──> Wave 4 (DetailPage瘦身) ──> Wave 6 (构建+测试)
       └────────────────────────────┘
```

- Wave 1 先行（后端 API 是前端的基础）
- Wave 2 依赖 Wave 1（调度器需要先建好才能写日志）
- Wave 3 和 Wave 5 可并行（GrowthPage 新建 + CreditsPage 增强 互不依赖）
- Wave 4 在 Wave 3 和 5 完成后执行（先建好新页面再拆旧页面，避免功能空窗）
- Wave 6 最后执行（全量构建与验证）
