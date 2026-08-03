# CodeBuddy/WorkBuddy 积分余额监控设计

> 日期：2026-08-03
> 状态：已确认（用户选择：CodeBuddy 真实采集 + 历史趋势图；不做告警；Qoder 暂缓）

## 1. 背景与需求

2api 管理台需求包含“Token/积分/配额监控”（设计文档 `macmini-multi-account-proxy-checkin.md` 11.5、Phase 3）。当前实现只对 Qoder 真实拉取 quota；CodeBuddy 的 `points` 快照是占位值：

```python
# src/qb2api/checkin/metrics_collector.py
elif provider == "codebuddy" and purpose == "checkin":
    await self._write(
        key=(provider, account_id, "points"),
        value=None,
        status="unknown",
        error="protocol_not_verified",
    )
```

另外采集器只遍历 `credentials` 表（当前库里只有 chat 凭据行），导致 `points`/`quota` 分支实际从不执行。

本次目标：**用库里真实 CodeBuddy/WorkBuddy OAuth 凭据实现积分余额真实采集，并落历史趋势供前端展示。**

## 2. 已证实的上游协议（脱敏 Spike 证据）

2026-08-03 使用库中两个真实 CodeBuddy OAuth 账号（`cb-501debc6f6f7`、`cb-d0cb2f6ace4a`）实测：

- `POST https://www.workbuddy.cn/billing/meter/get-user-resource`
- 请求头：`Authorization: Bearer <access_token>`、`Accept: application/json`、`Content-Type: application/json`、`X-Client-Platform: web`，body `{}`
- 两个账号均返回 HTTP 200，`code=0`，`data.Response.Data.Accounts[]` 为资源包数组
- 关键字段（单位 `credits`）：
  - `CapacityRemain` / `CapacityUsed` / `CapacitySize`
  - `CycleCapacityRemain` / `CycleCapacitySize`
  - `Status`、`Threshold`、`ExpiredTime`、`RemainCycles`、`TotalCycles`
- 同一接口在 `https://www.codebuddy.cn` 同样可用且数据一致
- 现有 `checkin-status` GET（`/billing/meter/checkin-status`）返回 404，不作为积分来源

响应含 `Uin`、`DealName`、`payerUin` 等账号标识字段，**不得落库或返回前端**。

Qoder：库内两个账号只有 chat PAT，quota/签到接口均返回 401 `token is not active`；checkin purpose 为 `needs_import`。本轮 Qoder 不做。

## 3. 范围

### 范围内

1. CodeBuddy/WorkBuddy 积分余额真实采集（`points` 快照）
2. 积分历史趋势存储（每次刷新一个点）与保留清理
3. 管理 API 提供历史序列
4. 前端账号详情展示当前积分 + 趋势图，账号列表显示剩余积分

### 范围外

- 不做告警/通知（用户明确去掉）
- Qoder 积分/配额本轮不做（等待设备凭据导入）
- 不做积分消耗明细对账（`get-user-request-usage` 后续再说）

## 4. 架构与数据流

```text
MetricsScheduler (15min / 手动刷新)
  └─ MetricSnapshotCollector
       ├─ codebuddy + purpose=checkin
       │    └─ CodeBuddyCreditsClient
       │         POST /billing/meter/get-user-resource (Bearer)
       │         └─ normalize_credits() -> 脱敏摘要
       │              ├─ account_metric_snapshots (points, fresh/stale/unavailable)
       │              └─ account_metric_history (每个非空值一个点)
       ├─ token/checkin 快照（现有逻辑）
       └─ qoder quota（现有逻辑，本轮不动）
            └─ 清理历史：删除超过 metrics_history_retention_days 的行
```

## 5. 数据模型

### 5.1 `points` 快照值（`account_metric_snapshots.metric_value_json`）

```json
{
  "unit": "credits",
  "total_remaining": 3700,
  "total_used": 0,
  "total_capacity": 5000,
  "cycle_remaining": 3700,
  "cycle_capacity": 5000,
  "package_count": 17,
  "depleted_packages": 0,
  "lowest_remaining": 100,
  "expires_at": null
}
```

禁止字段：`Uin`、`DealName`、`payerUin`、`AppId`、`ResourceId`、任何 token/cookie。

### 5.2 历史表（schema V5）

```sql
CREATE TABLE IF NOT EXISTS account_metric_history (
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    metric_kind TEXT NOT NULL,
    metric_value_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'fresh',
    PRIMARY KEY (provider, account_id, metric_kind, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_account_metric_history_lookup
ON account_metric_history(provider, account_id, metric_kind, observed_at DESC);
```

写入规则：快照 `value` 非空时写历史（fresh 和保留旧值的 stale 都写；unavailable/unknown 不写）。

## 6. 后端变更

### 6.1 新客户端 `src/qb2api/checkin/codebuddy_credits.py`

```python
class CodeBuddyCreditsUnavailableError(RuntimeError): ...

class CodeBuddyCreditsClient:
    def __init__(self, base_url="https://www.workbuddy.cn",
                 path="/billing/meter/get-user-resource",
                 timeout=15.0, client=None): ...
    async def fetch(self, access_token: str) -> dict[str, Any]: ...
    async def aclose(self) -> None: ...

def normalize_credits(body: dict[str, Any] | None) -> dict[str, Any]: ...
```

`fetch` 错误语义：空 token → `CodeBuddyCreditsUnavailableError("access credential unavailable")`；传输错误 → `transport:<type>`；非 2xx → `http:<code>`；`code != 0` 或缺少 `data.Response.Data.Accounts` → 业务错误。

### 6.2 `metrics_collector.py`

- 遍历来源从“credentials 元数据”改为“accounts × purposes”：`repo.list_accounts(provider)` + `repo.list_purposes(provider, account_id)`，跳过未启用 purpose。
- `MetricDependencies` 增加 `codebuddy_credits`。
- `_write_provider_snapshot`：`codebuddy + checkin` 走 `_write_credits_snapshot`（与 quota 相同的退避/失败语义）；`qoder + checkin` 保持 quota。
- `_write()` 在 value 非空时追加历史行。
- `collect()` 末尾执行历史保留清理（按 `settings.metrics_history_retention_days`）。

### 6.3 存储 `repo_telemetry.py` / `schema.py`

- 新增 `MANAGEMENT_SCHEMA_V5`（历史表 + 索引），`MANAGEMENT_SCHEMA = V4 + V5`，`schema_version` 升到 `5`。
- `TelemetryRepositoryMixin` 新增：
  - `upsert_metric_history(provider, account_id, metric_kind, value, status, observed_at=None, expires_at=None)`
  - `list_metric_history(provider, account_id, metric_kind, limit=500, since=None) -> list[dict]`（时间升序，取最近 limit 条）
  - `delete_metric_history_before(before_iso: str) -> int`

### 6.4 配置

- `Settings`：`codebuddy_credits_path: str = "/billing/meter/get-user-resource"`（env `CODEBUDDY_CREDITS_PATH`）；`metrics_history_retention_days: int = 90`（env `QB2API_METRICS_HISTORY_RETENTION_DAYS`）
- `control/settings.py`：`_ATTRS` 增加 `monitoring.metrics_history_retention_days`；`_RANGE_RULES` 增加 (1, 3650)
- 前端 SettingsPage 增加“积分历史保留天数”条目

### 6.5 管理 API

`GET /api/admin/metrics/accounts/{provider}/{account_id}/history/{metric_kind}`，query：`limit`（默认 500，最大 2000）、`since`（ISO）。返回：

```json
{
  "provider": "codebuddy",
  "account_id": "cb-...",
  "metric_kind": "points",
  "rows": [{"observed_at": "...", "value": {"total_remaining": 3700}, "status": "fresh"}],
  "limit": 500
}
```

需 `require_admin`，账号存在性校验与现有 detail 端点一致。

## 7. 前端

### 7.1 账号详情 `AccountDetailPage.vue`

- “积分与配额”面板：`points` 显示 `剩余 X credits（已用 Y / 总 Z）` + 采样时间 + 状态；
- 面板下方新增趋势区：`GET /metrics/accounts/{provider}/{account_id}/history/points?limit=500`，用现有 `MetricChart` 组件渲染（labels=时间、values=total_remaining）；
- 无数据状态：`尚未采集积分历史`；加载/失败状态与现有面板一致。

### 7.2 账号列表 `AccountsPage.vue`

`metricSummary`：`points` 且 value 有 `total_remaining` 时显示 `剩余 X credits`；否则保留 stale/unavailable/unknown 文案。

## 8. 状态语义

| 场景 | snapshot status | value | 历史点 |
| --- | --- | --- | --- |
| 首次成功 | fresh | 归一化值 | 写 |
| 成功（已有旧值） | fresh | 新值 | 写 |
| 传输/5xx 失败（有旧值） | stale | 保留旧值 | 写（保留旧值） |
| 401/403 | unavailable | None | 不写；同时更新对应 purpose `needs_reauth` |
| 无凭据/协议错误 | unavailable | None | 不写 |
| 退避期间 | stale/skipped | 保留旧值或跳过 | 不重复写 |

## 9. 测试与验证

1. 单测：`normalize_credits` 字段白名单与脱敏；client 错误分类；历史表 upsert/list/cleanup
2. 调度测试：codebuddy points fresh/stale/unavailable；历史点写入；`test_scheduler_keeps_workbuddy_points_unknown` 改为 fresh 断言
3. schema 测试：`account_metric_history` 存在；`schema_version == "5"`
4. API 测试：history 端点鉴权、分页、脱敏
5. 真实验证：跑一次 `refresh_once`，确认两个真实账号产生 `points` 快照 + 历史点（脱敏输出）
6. 前端：`vue-tsc` typecheck、`vite build`、相关 vitest
7. 收尾：`git diff --check`、pytest 定向套件

## 10. 风险与权衡

- 上游接口为非公开契约，字段名以实测为准；解析采用字段白名单，未知字段自动丢弃，后续字段变化只会导致部分值缺失而非泄露。
- 每次刷新全量拉取 17~27 个资源包并只保留摘要；15 分钟粒度 × 90 天 ≈ 每账号 8640 行，SQLite 可承受。
- `X-Client-Platform: web` 为实测必需头；不依赖 Cookie，仅用 OAuth Bearer。
