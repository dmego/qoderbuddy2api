# CodeBuddy Credits Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用真实 CodeBuddy/WorkBuddy OAuth 凭据采集积分余额快照，落历史趋势，并在管理台账号详情展示当前积分与趋势图。

**Architecture:** 新增 `CodeBuddyCreditsClient` 调用已实测的 `POST /billing/meter/get-user-resource`；`MetricSnapshotCollector` 改为按账号/purpose 遍历，将归一化结果写入 `points` 快照与新增的 `account_metric_history` 表；管理 API 暴露历史序列；前端账号详情用现有 `MetricChart` 渲染。

**Tech Stack:** Python 3.12 / FastAPI / aiosqlite / httpx；Vue 3 + ECharts 6 + TanStack Query。

## Global Constraints

- 文件行数：手写源码函数 ≤ 50 行、文件 ≤ 300 行、嵌套 ≤ 3、位置参数 ≤ 3（项目 AGENTS.md 硬门禁）。
- 禁止落库/返回/记录：`Uin`、`DealName`、`payerUin`、`AppId`、`ResourceId`、token/cookie、原始上游响应。
- 积分未知或过期不得伪造为 0；状态语义：fresh / stale / unavailable / unknown。
- commit message：`<type>(scope): <summary>`，英文 summary ≤ 50 字符。
- 测试：`pytest`；前端：`pnpm -C frontend typecheck`、`pnpm -C frontend build`。
- 上游协议字段以实测为准（2026-08-03 Spike 已确认 `data.Response.Data.Accounts[]`）。

---

### Task 1: Schema V5 与积分历史存储

**Files:**
- Modify: `src/qb2api/accounts/schema_management.py`
- Modify: `src/qb2api/accounts/repository.py:69-88`（schema_version 5）
- Modify: `src/qb2api/accounts/repo_telemetry.py`（新增 3 个方法）
- Test: `tests/accounts/test_schema_migrations.py`、`tests/accounts/test_repository_transactions.py`（或新增 `tests/accounts/test_metric_history.py`）

**Interfaces:**
- Produces:
  - `repo.upsert_metric_history(*, provider, account_id, metric_kind, value, status="fresh", observed_at=None, expires_at=None)`
  - `repo.list_metric_history(*, provider, account_id, metric_kind, limit=500, since=None) -> list[dict]`（时间升序）
  - `repo.delete_metric_history_before(before_iso: str) -> int`
  - `account_metric_history` 表 + `idx_account_metric_history_lookup` 索引

- [ ] **Step 1: 写失败测试**

`tests/accounts/test_metric_history.py`：

```python
import pytest

from qb2api.accounts.repository import AccountRepository


@pytest.mark.asyncio
async def test_metric_history_upsert_list_cleanup(tmp_path):
    repo = AccountRepository(str(tmp_path / "h.sqlite3"))
    await repo.connect()
    await repo.migrate()
    await repo.upsert_metric_history(
        provider="codebuddy", account_id="cb-1", metric_kind="points",
        value={"total_remaining": 100}, observed_at="2026-08-03T00:00:00+00:00",
    )
    await repo.upsert_metric_history(
        provider="codebuddy", account_id="cb-1", metric_kind="points",
        value={"total_remaining": 90}, observed_at="2026-08-03T00:15:00+00:00",
    )
    rows = await repo.list_metric_history(
        provider="codebuddy", account_id="cb-1", metric_kind="points",
    )
    assert [r["value"]["total_remaining"] for r in rows] == [100, 90]
    assert await repo.delete_metric_history_before("2026-08-03T00:10:00+00:00") == 1
    assert len(await repo.list_metric_history(
        provider="codebuddy", account_id="cb-1", metric_kind="points",
    )) == 1
    await repo.close()


@pytest.mark.asyncio
async def test_metric_history_upsert_is_idempotent(tmp_path):
    repo = AccountRepository(str(tmp_path / "h2.sqlite3"))
    await repo.connect()
    await repo.migrate()
    observed = "2026-08-03T00:00:00+00:00"
    await repo.upsert_metric_history(
        provider="codebuddy", account_id="cb-1", metric_kind="points",
        value={"total_remaining": 1}, observed_at=observed,
    )
    await repo.upsert_metric_history(
        provider="codebuddy", account_id="cb-1", metric_kind="points",
        value={"total_remaining": 2}, observed_at=observed,
    )
    rows = await repo.list_metric_history(
        provider="codebuddy", account_id="cb-1", metric_kind="points",
    )
    assert len(rows) == 1 and rows[0]["value"]["total_remaining"] == 2
    await repo.close()
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/accounts/test_metric_history.py -q`
Expected: FAIL（`AccountRepository.upsert_metric_history` 不存在）

- [ ] **Step 3: 实现 schema 与方法**

`src/qb2api/accounts/schema_management.py` 末尾：

```python
MANAGEMENT_SCHEMA_V5 = """
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
"""

MANAGEMENT_SCHEMA = MANAGEMENT_SCHEMA_V4 + MANAGEMENT_SCHEMA_V5
```

`src/qb2api/accounts/repository.py` migrate()：`'4'` → `'5'`（两处：写库值与测试断言对应）。

`src/qb2api/accounts/repo_telemetry.py` 新增：

```python
    async def upsert_metric_history(
        self,
        *,
        provider: str,
        account_id: str,
        metric_kind: str,
        value: Any,
        status: str = "fresh",
        observed_at: str | None = None,
        expires_at: str | None = None,
    ) -> None:
        async with self._operation(write=True) as db:
            await db.execute(
                """
                INSERT INTO account_metric_history
                    (provider, account_id, metric_kind, metric_value_json,
                     observed_at, expires_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, account_id, metric_kind, observed_at) DO UPDATE SET
                    metric_value_json=excluded.metric_value_json,
                    expires_at=excluded.expires_at,
                    status=excluded.status
                """,
                (
                    provider,
                    account_id,
                    metric_kind,
                    json.dumps(value, ensure_ascii=False),
                    observed_at or now_iso(),
                    expires_at,
                    status,
                ),
            )

    async def list_metric_history(
        self,
        *,
        provider: str,
        account_id: str,
        metric_kind: str,
        limit: int = 500,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT * FROM account_metric_history
            WHERE provider=? AND account_id=? AND metric_kind=?
        """
        params: list[Any] = [provider, account_id, metric_kind]
        if since:
            query += " AND observed_at >= ?"
            params.append(since)
        query += " ORDER BY observed_at DESC LIMIT ?"
        params.append(limit)
        async with self._operation() as db:
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
        return [self._metric_row(row) for row in reversed(rows)]

    async def delete_metric_history_before(self, before_iso: str) -> int:
        async with self._operation(write=True) as db:
            cursor = await db.execute(
                "DELETE FROM account_metric_history WHERE observed_at < ?",
                (before_iso,),
            )
        return cursor.rowcount
```

（`_metric_row` 已存在，可复用。）

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/accounts/test_metric_history.py -q`
Expected: PASS

- [ ] **Step 5: 更新既有 schema 断言并运行**

`tests/accounts/test_schema_migrations.py`：`schema_version() == "4"` → `"5"`（两处），`test_public_schema_contains_current_management_tables` 断言集合增加 `"account_metric_history"`。

Run: `python -m pytest tests/accounts/test_schema_migrations.py tests/accounts/test_metric_history.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/qb2api/accounts/schema_management.py src/qb2api/accounts/repository.py src/qb2api/accounts/repo_telemetry.py tests/accounts/test_metric_history.py tests/accounts/test_schema_migrations.py
git commit -m "feat(metrics): persist credit history snapshots"
```

---

### Task 2: CodeBuddyCreditsClient 与归一化

**Files:**
- Create: `src/qb2api/checkin/codebuddy_credits.py`
- Test: `tests/checkin/test_codebuddy_credits.py`

**Interfaces:**
- Produces:
  - `CodeBuddyCreditsUnavailableError(RuntimeError)`
  - `CodeBuddyCreditsClient(base_url="https://www.workbuddy.cn", path="/billing/meter/get-user-resource", timeout=15.0, client=None)`
  - `await client.fetch(access_token: str) -> dict`（归一化结果）
  - `await client.aclose()`
  - `normalize_credits(body: dict | None) -> dict`

- [ ] **Step 1: 写失败测试**

`tests/checkin/test_codebuddy_credits.py`：

```python
import pytest

from qb2api.checkin.codebuddy_credits import (
    CodeBuddyCreditsClient,
    CodeBuddyCreditsUnavailableError,
    normalize_credits,
)


def _body():
    return {
        "code": 0,
        "msg": "OK",
        "data": {"Response": {"Data": {"TotalCount": 2, "Accounts": [
            {
                "AccountId": 1, "Uin": "secret-uin", "DealName": "secret-deal",
                "CapacityUnit": "credits", "CapacityRemain": 100, "CapacityUsed": 0,
                "CapacitySize": 500, "CycleCapacityRemain": 90, "CycleCapacitySize": 500,
                "Status": 0, "Threshold": 10, "ExpiredTime": "",
                "AccountAttributes": [{"Key": "payerUin", "Value": "secret"}],
            },
            {
                "AccountId": 2, "Uin": "secret-uin", "DealName": "secret-deal",
                "CapacityUnit": "credits", "CapacityRemain": 0, "CapacityUsed": 500,
                "CapacitySize": 500, "CycleCapacityRemain": 0, "CycleCapacitySize": 500,
                "Status": 0, "Threshold": 0, "ExpiredTime": "1784517058000",
                "AccountAttributes": [],
            },
        ]}}},
    }


def test_normalize_credits_keeps_only_business_fields():
    value = normalize_credits(_body())
    assert value == {
        "unit": "credits",
        "total_remaining": 100,
        "total_used": 500,
        "total_capacity": 1000,
        "cycle_remaining": 90,
        "cycle_capacity": 1000,
        "package_count": 2,
        "depleted_packages": 1,
        "lowest_remaining": 0,
        "expires_at": "2026-07-19T15:10:58+00:00",
    }
    dumped = str(value)
    assert "secret" not in dumped


def test_normalize_credits_rejects_missing_data():
    assert normalize_credits(None) == {}
    assert normalize_credits({"code": 1, "msg": "boom"}) == {}


@pytest.mark.asyncio
async def test_client_rejects_empty_token_and_http_errors():
    client = CodeBuddyCreditsClient()
    with pytest.raises(CodeBuddyCreditsUnavailableError, match="access credential"):
        await client.fetch("")
    await client.aclose()
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/checkin/test_codebuddy_credits.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现客户端**

`src/qb2api/checkin/codebuddy_credits.py`：

```python
"""CodeBuddy/WorkBuddy credit balance client (CB-CREDITS-01)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from .base import join_url, parse_json_body


class CodeBuddyCreditsUnavailableError(RuntimeError):
    """The upstream credits endpoint did not provide a usable snapshot."""


class CodeBuddyCreditsClient:
    """Fetch only the aggregate credit fields required by the console."""

    def __init__(
        self,
        *,
        base_url: str = "https://www.workbuddy.cn",
        path: str = "/billing/meter/get-user-resource",
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.path = path
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0))
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self, access_token: str) -> dict[str, Any]:
        if not access_token:
            raise CodeBuddyCreditsUnavailableError("access credential unavailable")
        try:
            response = await self._client.post(
                join_url(self.base_url, self.path),
                json={},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-Client-Platform": "web",
                },
            )
        except httpx.HTTPError as error:
            raise CodeBuddyCreditsUnavailableError(
                f"transport:{type(error).__name__}"
            ) from error
        body = parse_json_body(response.text)
        if not 200 <= response.status_code < 300:
            raise CodeBuddyCreditsUnavailableError(f"http:{response.status_code}")
        normalized = normalize_credits(body)
        if not normalized:
            raise CodeBuddyCreditsUnavailableError("empty credits response")
        return normalized


def normalize_credits(body: dict[str, Any] | None) -> dict[str, Any]:
    """Return a bounded, secret-free aggregate of the credits response."""
    if not isinstance(body, dict) or body.get("code") not in (0, "0"):
        return {}
    data = body.get("data")
    response = data.get("Response") if isinstance(data, dict) else None
    payload = response.get("Data") if isinstance(response, dict) else None
    accounts = payload.get("Accounts") if isinstance(payload, dict) else None
    if not isinstance(accounts, list) or not accounts:
        return {}
    total_remaining = 0
    total_used = 0
    total_capacity = 0
    cycle_remaining = 0
    cycle_capacity = 0
    depleted = 0
    lowest: int | None = None
    unit = ""
    expires: list[int] = []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        unit = unit or str(account.get("CapacityUnit") or "")
        remain = _number(account.get("CapacityRemain"))
        used = _number(account.get("CapacityUsed"))
        size = _number(account.get("CapacitySize"))
        cycle_remain = _number(account.get("CycleCapacityRemain"))
        cycle_size = _number(account.get("CycleCapacitySize"))
        total_remaining += remain
        total_used += used
        total_capacity += size
        cycle_remaining += cycle_remain
        cycle_capacity += cycle_size
        if remain <= 0:
            depleted += 1
        if lowest is None or remain < lowest:
            lowest = remain
        if account.get("ExpiredTime"):
            expires.append(int(account["ExpiredTime"]))
    return {
        "unit": unit or "credits",
        "total_remaining": total_remaining,
        "total_used": total_used,
        "total_capacity": total_capacity,
        "cycle_remaining": cycle_remaining,
        "cycle_capacity": cycle_capacity,
        "package_count": len(accounts),
        "depleted_packages": depleted,
        "lowest_remaining": lowest if lowest is not None else 0,
        "expires_at": _epoch_ms_to_iso(min(expires)) if expires else None,
    }


def _number(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _epoch_ms_to_iso(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()
```

（`join_url` 来自 `qb2api.checkin.base`，已存在。）

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/checkin/test_codebuddy_credits.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/qb2api/checkin/codebuddy_credits.py tests/checkin/test_codebuddy_credits.py
git commit -m "feat(checkin): add codebuddy credits client"
```

---

### Task 3: 配置项（路径与保留天数）

**Files:**
- Modify: `src/qb2api/config.py`
- Modify: `src/qb2api/control/settings.py`
- Modify: `frontend/src/pages/SettingsPage.vue`
- Test: `tests/test_config_settings.py`、`tests/control/test_settings.py`

**Interfaces:**
- Produces: `settings.codebuddy_credits_path`、`settings.metrics_history_retention_days`；设置键 `monitoring.metrics_history_retention_days`

- [ ] **Step 1: 写失败测试**

在 `tests/test_config_settings.py` 追加：

```python
def test_observability_history_retention_default(monkeypatch, tmp_path):
    from qb2api.config import Settings
    monkeypatch.delenv("QB2API_METRICS_HISTORY_RETENTION_DAYS", raising=False)
    settings = Settings.from_env(str(tmp_path / "missing.env"))
    assert settings.metrics_history_retention_days == 90
    assert settings.codebuddy_credits_path == "/billing/meter/get-user-resource"
```

在 `tests/control/test_settings.py` 追加：

```python
def test_history_retention_setting_validation():
    from qb2api.control.settings import SettingsApplier
    assert SettingsApplier.attribute("monitoring.metrics_history_retention_days") == "metrics_history_retention_days"
    with pytest.raises(ValueError):
        SettingsApplier.validate("monitoring.metrics_history_retention_days", 0)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_config_settings.py tests/control/test_settings.py -q -k "history or retention" -q`
Expected: FAIL

- [ ] **Step 3: 实现**

`src/qb2api/config.py`：
- `Settings` 字段 `metrics_interval_seconds` 后加：
  ```python
  codebuddy_credits_path: str = "/billing/meter/get-user-resource"
  metrics_history_retention_days: int = 90
  ```
- `_codebuddy_checkin_values()` 增加：
  ```python
  "codebuddy_credits_path": os.getenv("CODEBUDDY_CREDITS_PATH", "/billing/meter/get-user-resource"),
  ```
- `_observability_values()` 增加：
  ```python
  "metrics_history_retention_days": _env_int("QB2API_METRICS_HISTORY_RETENTION_DAYS", 90),
  ```

`src/qb2api/control/settings.py`：
- `_RANGE_RULES` 增加：`"monitoring.metrics_history_retention_days": (1, 3650, "history retention must be between 1 and 3650 days")`
- `_ATTRS` 增加：`"monitoring.metrics_history_retention_days": "metrics_history_retention_days"`

`frontend/src/pages/SettingsPage.vue` 的设置 map 中 `monitoring.metrics_interval_seconds` 后增加：

```ts
"monitoring.metrics_history_retention_days": { label: "积分历史保留天数", description: "账号积分历史快照的保留窗口。", min: 1, max: 3650, unit: "天" },
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_config_settings.py tests/control/test_settings.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/qb2api/config.py src/qb2api/control/settings.py frontend/src/pages/SettingsPage.vue tests/test_config_settings.py tests/control/test_settings.py
git commit -m "feat(config): add credits path and history retention"
```

---

### Task 4: 采集器接入（真实积分 + 历史写入 + 遍历修复）

**Files:**
- Modify: `src/qb2api/checkin/metrics.py`
- Modify: `src/qb2api/checkin/metrics_collector.py`
- Test: `tests/metrics/test_metrics_scheduler.py`

**Interfaces:**
- Consumes: `CodeBuddyCreditsClient`、`repo.upsert_metric_history`、`repo.delete_metric_history_before`
- Produces: `MetricsScheduler(..., codebuddy_credits=...)`；`points` 快照 + 历史点

- [ ] **Step 1: 写失败测试**

替换 `tests/metrics/test_metrics_scheduler.py` 中 `test_scheduler_keeps_workbuddy_points_unknown` 为：

```python
class FakeCredits:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    async def fetch(self, token):
        self.calls += 1
        await asyncio.sleep(0)
        if self.error:
            raise self.error
        return self.result

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_scheduler_collects_workbuddy_points_fresh(metric_context):
    repo, vault, registry, resolver = metric_context
    await _seed(repo, vault, "codebuddy", "cb-1", "checkin", {"access_token": "cb-token"})
    credits = FakeCredits({
        "unit": "credits", "total_remaining": 300, "total_used": 0,
        "total_capacity": 500, "cycle_remaining": 300, "cycle_capacity": 500,
        "package_count": 2, "depleted_packages": 0, "lowest_remaining": 100,
        "expires_at": None,
    })
    scheduler = MetricsScheduler(
        settings=Settings(metrics_enabled=False),
        repo=repo,
        registry=registry,
        resolver=resolver,
        qoder_quota=FakeQuota(),
        codebuddy_credits=credits,
    )
    result = await scheduler.refresh_once()
    points = [row for row in await repo.list_metric_snapshots() if row["metric_kind"] == "points"]
    assert result["fresh"] >= 1
    assert points[0]["status"] == "fresh"
    assert points[0]["value"]["total_remaining"] == 300
    history = await repo.list_metric_history(
        provider="codebuddy", account_id="cb-1", metric_kind="points",
    )
    assert history and history[-1]["value"]["total_remaining"] == 300
    await scheduler.stop()


@pytest.mark.asyncio
async def test_scheduler_marks_workbuddy_points_stale_on_failure(metric_context):
    repo, vault, registry, resolver = metric_context
    await _seed(repo, vault, "codebuddy", "cb-1", "checkin", {"access_token": "cb-token"})
    credits = FakeCredits({
        "unit": "credits", "total_remaining": 300, "total_used": 0,
        "total_capacity": 500, "cycle_remaining": 300, "cycle_capacity": 500,
        "package_count": 2, "depleted_packages": 0, "lowest_remaining": 100,
        "expires_at": None,
    })
    scheduler = MetricsScheduler(
        settings=Settings(metrics_enabled=False),
        repo=repo, registry=registry, resolver=resolver,
        qoder_quota=FakeQuota(), codebuddy_credits=credits,
    )
    await scheduler.refresh_once()
    credits.error = CodeBuddyCreditsUnavailableError("http:503")
    await scheduler.refresh_once()
    points = [row for row in await repo.list_metric_snapshots() if row["metric_kind"] == "points"]
    assert points[0]["status"] == "stale"
    assert points[0]["value"]["total_remaining"] == 300
    await scheduler.stop()
```

顶部 import 增加 `from qb2api.checkin.codebuddy_credits import CodeBuddyCreditsUnavailableError`。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/metrics/test_metrics_scheduler.py -q -k "points" -q`
Expected: FAIL

- [ ] **Step 3: 实现**

`src/qb2api/checkin/metrics.py`：

```python
from .codebuddy_credits import CodeBuddyCreditsClient

class MetricsScheduler:
    def __init__(self, *, settings, repo, registry, resolver,
                 qoder_quota=None, codebuddy_credits=None):
        ...
        self.codebuddy_credits = codebuddy_credits or CodeBuddyCreditsClient(
            base_url=settings.codebuddy_checkin_base,
            path=settings.codebuddy_credits_path,
            timeout=float(settings.checkin_request_timeout_seconds),
        )
        self._collector = MetricSnapshotCollector(
            MetricDependencies(
                settings=settings, repo=repo, registry=registry,
                resolver=resolver, qoder_quota=self.qoder_quota,
                codebuddy_credits=self.codebuddy_credits,
            ),
            self._backoff,
        )
```

`stop()` 中 `await self.codebuddy_credits.aclose()`。

`src/qb2api/checkin/metrics_collector.py`：

```python
@dataclass(frozen=True)
class MetricDependencies:
    settings: Settings
    repo: AccountRepository
    registry: AccountRegistry
    resolver: CredentialResolver
    qoder_quota: Any
    codebuddy_credits: Any
```

`collect()` 与 `_collect_item` 改为按账号/purpose 遍历：

```python
    async def collect(self) -> dict[str, int]:
        await self._dependencies.registry.rebuild()
        state = MetricCollectionState(
            previous=await self._previous_rows(),
            counts={"fresh": 0, "stale": 0, "unknown": 0, "unavailable": 0, "skipped": 0},
            seen=set(),
        )
        for account in await self._dependencies.repo.list_accounts():
            if not account.get("enabled"):
                continue
            provider = str(account["provider"])
            account_id = str(account["account_id"])
            for purpose in await self._dependencies.repo.list_purposes(provider, account_id):
                if not purpose.get("enabled"):
                    continue
                await self._collect_item(
                    provider=provider,
                    account_id=account_id,
                    purpose=str(purpose["purpose"]),
                    expires_at=purpose.get("expires_at"),
                    state=state,
                )
        self._count_unseen_previous(state)
        await self._prune_history()
        return state.counts

    async def _collect_item(
        self,
        *,
        provider: str,
        account_id: str,
        purpose: str,
        expires_at: str | None,
        state: MetricCollectionState,
    ) -> None:
        await self._write_token_snapshot(
            provider=provider,
            account_id=account_id,
            purpose=purpose,
            expires_at=expires_at,
            state=state,
        )
        await self._write_checkin_snapshot(provider, account_id, state)
        await self._write_provider_snapshot(
            provider=provider,
            account_id=account_id,
            purpose=purpose,
            state=state,
        )
```

`_write_token_snapshot` 参数 `item` 改为 `expires_at`。

`_write_provider_snapshot` 的 codebuddy 分支：

```python
        if provider == "codebuddy" and purpose == "checkin":
            await self._write_credits_snapshot(account_id, state)
        elif provider == "qoder" and purpose == "checkin":
            await self._write_quota_snapshot(account_id, state)
```

新增 `_write_credits_snapshot`（与 quota 同款退避/失败语义）：

```python
    async def _write_credits_snapshot(
        self,
        account_id: str,
        state: MetricCollectionState,
    ) -> None:
        key = ("codebuddy", account_id, "points")
        state.seen.add(key)
        if await self._write_backoff_snapshot(key, state):
            return
        try:
            credential = await self._dependencies.resolver.credential(
                "codebuddy", account_id, "checkin"
            )
            token = _access_token(credential)
            value = await self._dependencies.codebuddy_credits.fetch(token)
        except (LookupError, QuotaUnavailableError, CodeBuddyCreditsUnavailableError) as error:
            await self._write_failure(key, state, str(error))
        except Exception as error:
            await self._write_failure(key, state, type(error).__name__)
        else:
            await self._write(key=key, value=value, status="fresh", state=state)
            self._backoff.pop(self._backoff_key(key), None)
```

`_write` 末尾（counts 更新前）追加历史：

```python
        if value is not None:
            await self._dependencies.repo.upsert_metric_history(
                provider=key[0],
                account_id=key[1],
                metric_kind=key[2],
                value=value,
                status=status,
                observed_at=observed_at,
            )
```

新增清理：

```python
    async def _prune_history(self) -> None:
        retention = self._dependencies.settings.metrics_history_retention_days
        if retention <= 0:
            return
        before = (datetime.now(UTC) - timedelta(days=retention)).isoformat()
        await self._dependencies.repo.delete_metric_history_before(before)
```

文件顶部 import `CodeBuddyCreditsUnavailableError`。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/metrics/test_metrics_scheduler.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/qb2api/checkin/metrics.py src/qb2api/checkin/metrics_collector.py tests/metrics/test_metrics_scheduler.py
git commit -m "feat(metrics): collect real codebuddy credit balance"
```

---

### Task 5: 管理 API 历史端点

**Files:**
- Modify: `src/qb2api/admin/observability_routes.py`
- Test: `tests/integration/test_metric_history_api.py`

**Interfaces:**
- Produces: `GET /api/admin/metrics/accounts/{provider}/{account_id}/history/{metric_kind}?limit=&since=`

- [ ] **Step 1: 写失败测试**

`tests/integration/test_metric_history_api.py`（复用 `management_context` fixture，参照 `tests/integration/test_management_metrics.py`）：

```python
import pytest

import httpx


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer admin-secret"}


@pytest.mark.asyncio
async def test_metric_history_endpoint_requires_admin_and_returns_rows(management_context):
    app, repo, _refreshes = management_context
    await repo.upsert_account(
        provider="codebuddy", account_id="cb-1", label="cb-1",
        source="manual", enabled=True,
    )
    await repo.upsert_purpose(
        provider="codebuddy", account_id="cb-1", purpose="checkin",
        enabled=True, status="active", verification_status="verified",
    )
    await app.state.account_registry.rebuild()
    await repo.upsert_metric_history(
        provider="codebuddy", account_id="cb-1", metric_kind="points",
        value={"total_remaining": 100}, observed_at="2026-08-03T00:00:00+00:00",
    )
    await repo.upsert_metric_history(
        provider="codebuddy", account_id="cb-1", metric_kind="points",
        value={"total_remaining": 90}, observed_at="2026-08-03T00:15:00+00:00",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        anonymous = await client.get(
            "/api/admin/metrics/accounts/codebuddy/cb-1/history/points?limit=1"
        )
        assert anonymous.status_code == 401
        response = await client.get(
            "/api/admin/metrics/accounts/codebuddy/cb-1/history/points?limit=1",
            headers=_headers(),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["metric_kind"] == "points"
        assert [r["value"]["total_remaining"] for r in body["rows"]] == [90]
        assert body["limit"] == 1
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/integration/test_metric_history_api.py -q`
Expected: FAIL（404/无路由）

- [ ] **Step 3: 实现路由**

`src/qb2api/admin/observability_routes.py` 在 `account_metric_detail` 后新增：

```python
@router.get("/metrics/accounts/{provider}/{account_id}/history/{metric_kind}")
async def account_metric_history(
    provider: str,
    account_id: str,
    metric_kind: str,
    request: Request,
    *,
    limit: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    await require_admin(request)
    selected_provider = provider_filter(provider)
    selected_account = optional_account_id(account_id)
    state = admin_state(request)
    if find_account_view(state, selected_provider, selected_account) is None:
        raise HTTPException(status_code=404, detail="account_not_found")
    selected_limit = bounded_int(limit, default=500, maximum=2000)
    rows = await _repository(request).list_metric_history(
        provider=selected_provider,
        account_id=selected_account,
        metric_kind=metric_kind,
        limit=selected_limit,
        since=since or None,
    )
    return {
        "provider": selected_provider,
        "account_id": selected_account,
        "metric_kind": metric_kind,
        "rows": rows,
        "limit": selected_limit,
    }
```

确认该模块已 import `bounded_int`、`provider_filter`、`optional_account_id`、`find_account_view`（detail 端点已在用）。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/integration/test_metric_history_api.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/qb2api/admin/observability_routes.py tests/integration/test_metric_history_api.py
git commit -m "feat(admin): expose credit history endpoint"
```

---

### Task 6: 前端展示（当前积分 + 趋势图）

**Files:**
- Modify: `frontend/src/pages/AccountDetailPage.vue`
- Modify: `frontend/src/pages/AccountsPage.vue`
- Modify: `frontend/src/styles/tokens.css`（如无 caption 样式，新增 `.chart-caption`）

- [ ] **Step 1: 实现账号详情**

`AccountDetailPage.vue`：
- import 增加 `MetricChart from "@/components/MetricChart.vue"`；
- 类型增加：
  ```ts
  type MetricHistoryRow = { observed_at: string; status: string; value: Record<string, unknown> | null };
  ```
- `metrics` query 后增加：
  ```ts
  const pointsHistory = useQuery({
    queryKey: ["account-metric-history", provider, accountId],
    queryFn: () => apiRequest<{ rows: MetricHistoryRow[] }>(`/metrics/accounts/${encodeURIComponent(provider.value)}/${encodeURIComponent(accountId.value)}/history/points?limit=500`),
    staleTime: 30_000,
  });
  const creditsChart = computed(() => {
    const rows = pointsHistory.data.value?.rows ?? [];
    const labels: string[] = [];
    const values: number[] = [];
    for (const row of rows) {
      const total = (row.value as { total_remaining?: number } | null)?.total_remaining;
      if (typeof total !== "number") continue;
      labels.push(row.observed_at.slice(5, 16));
      values.push(total);
    }
    return { labels, values };
  });
  ```
- `metricValue` 改为：
  ```ts
  function metricValue(metric: Metric): string {
    const value = metric.value as { unit?: string; total_remaining?: number; total_used?: number; total_capacity?: number } | null;
    if (metric.metric_kind === "points" && value && typeof value.total_remaining === "number") {
      return `剩余 ${value.total_remaining} ${value.unit ?? "credits"}（已用 ${value.total_used ?? 0} / 总 ${value.total_capacity ?? 0}）`;
    }
    return metric.value ? JSON.stringify(metric.value) : "尚无可用数据";
  }
  ```
- 模板：在“积分与配额”面板（`<section class="overview-grid">` 内第二个 data-panel 之后）新增趋势面板：
  ```html
  <section class="data-panel"><PanelHeader title="积分趋势" description="每次采集一个点，默认保留 90 天。" /><div v-if="pointsHistory.isPending.value" class="loading-row">正在读取积分历史…</div><div v-else-if="pointsHistory.isError.value" class="data-state data-state--error">积分历史读取失败。<button class="secondary-button compact-button" type="button" @click="pointsHistory.refetch()">重试</button></div><div v-else-if="!creditsChart.labels.length" class="compact-empty">尚未采集积分历史。</div><template v-else><MetricChart :labels="creditsChart.labels" :values="creditsChart.values" /><small class="chart-caption">最近 {{ creditsChart.labels.length }} 个采样点 · 总剩余 Credits</small></template></section>
  ```

`AccountsPage.vue` `metricSummary` 开头增加：

```ts
const value = metric.value as { total_remaining?: number; unit?: string } | null;
if (metric.metric_kind === "points" && value && typeof value.total_remaining === "number") return `剩余 ${value.total_remaining} ${value.unit ?? "credits"}`;
```

- [ ] **Step 2: 验证前端**

Run: `pnpm -C frontend typecheck`
Expected: PASS

Run: `pnpm -C frontend build`
Expected: PASS（构建产物按仓库惯例同步到 `src/qb2api/web/dist`，若 vite outDir 配置如此）

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/AccountDetailPage.vue frontend/src/pages/AccountsPage.vue src/qb2api/web/dist
git commit -m "feat(admin): show credit balance and history chart"
```

---

### Task 7: 真实数据验证与收尾

**Files:**
- No new files（验证用临时命令）

- [ ] **Step 1: 跑一次真实采集**

用库中真实账号执行一次 `MetricsScheduler.refresh_once()`（复用 Spike 脚本结构，输出脱敏）：

```bash
PYTHONPATH=src python - <<'PY'
import asyncio
from qb2api.config import Settings
from qb2api.accounts import AccountRepository, AccountRegistry, CredentialResolver, CredentialVault
from qb2api.checkin.metrics import MetricsScheduler

async def main():
    settings = Settings.from_env(".env")
    vault = CredentialVault(settings.credential_key or "")
    repo = AccountRepository(str(settings.data_dir) + "/qb2api.sqlite3")
    await repo.connect()
    registry = AccountRegistry(repo, vault, codebuddy_tokens=settings.codebuddy_tokens or [], qoder_tokens=settings.qoder_tokens or [])
    resolver = CredentialResolver(repo, vault, registry, skew_seconds=settings.codebuddy_oauth_refresh_skew)
    scheduler = MetricsScheduler(settings=settings, repo=repo, registry=registry, resolver=resolver)
    result = await scheduler.refresh_once()
    print("counts:", result)
    rows = await repo.list_metric_snapshots(provider="codebuddy")
    for row in rows:
        print(row["account_id"], row["metric_kind"], row["status"], row["value"])
    history = await repo.list_metric_history(provider="codebuddy", account_id="cb-<redacted>", metric_kind="points", limit=3)
    print("history:", [(r["observed_at"], r["value"].get("total_remaining")) for r in history])
    await scheduler.stop()
    await repo.close()

asyncio.run(main())
PY
```

Expected: 两个 codebuddy 账号出现 `points/fresh` 快照（含 `total_remaining`），历史表有对应行；输出不含 token。

- [ ] **Step 2: 定向测试全量**

Run:
```bash
python -m pytest tests/checkin/test_codebuddy_credits.py tests/metrics/test_metrics_scheduler.py tests/accounts/test_metric_history.py tests/accounts/test_schema_migrations.py tests/integration/test_metric_history_api.py tests/test_config_settings.py tests/control/test_settings.py -q
```
Expected: PASS

Run: `git diff --check`
Expected: 无输出

- [ ] **Step 3: 收尾检查与说明**

核对：`points` 快照不再出现 `protocol_not_verified`；README/docs 如需可补一行“积分监控”说明（非必须）。

- [ ] **Step 4: Commit（如有收尾改动）**

```bash
git add -A
git commit -m "test(metrics): verify real credit collection"
```
