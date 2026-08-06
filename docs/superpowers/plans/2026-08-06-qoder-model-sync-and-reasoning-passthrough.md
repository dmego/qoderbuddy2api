# Qoder 模型同步与 reasoning_content 透传优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Qoder 模型目录增加"从上游同步"能力（按 provider 维度拉取官方 `/api/v1/cloud/models`，落库 `model_catalog`、更新运行时模型映射、合并进 `/v1/models`），并让流式响应对 `reasoning_content` 的透传可控（默认剥离，提供选项放行）。

**Architecture:**
- Qoder 模型同步：`RuntimeSnapshotService.build()` 是 Worker 模型清单的唯一事实源（从 `config/models.json` 加载，见 `src/qb2api/control/runtime_snapshot.py:46`），Worker 进程从不打开 SQLite——因此上游同步结果必须**同时**写入 `model_catalog`（管理台展示）**并合并进 snapshot 的 `models` 字段**（`/v1/models` 与请求路由才能看到新模型）。两处共享同一转换函数。
- reasoning 透传：在 `openai_stream` 的逐 chunk 透传路径上按开关剥离 `delta.reasoning_content`；`sse.py` 的非流式聚合路径（`reasoning_parts` → `message.reasoning_content`）不受影响。开关从环境变量读取，默认剥离（issue 现象），明确可用 `QB2API_STREAM_REASONING=1` 恢复原行为。

**Tech Stack:** Python 3.12 / FastAPI / aiosqlite / httpx / Vue 3 / TanStack Query / vitest / pytest

## Global Constraints

- Worker 永不打开 SQLite、不接触凭据明文（CLAUDE.md 双进程架构）：模型同步的 DB 读写只允许出现在 Control 侧。
- snapshot 传输模型必须保持 `RUNTIME_PROTOCOL_VERSION = 2` 不变；`to_payload()` / `from_payload()` 必须同步改动（`runtime_snapshot.py` 与 `control/runtime_snapshot.py` 各一份）。
- 不改 `config/models.json`；同步结果只进 DB + 运行时合并。
- 不新增第三方依赖；Qoder 官方接口用 `httpx`（已是依赖）。
- 凭据、token 不得进日志/审计/SQLite（CLAUDE.md 硬约束）；`pat` 只在 Control 进程内存中使用。
- 代码硬上限（`tools/check_code_limits.py`）：单文件 ≤500 行、单函数 ≤50 行、圈复杂度 ≤10、嵌套 ≤3、位置参数 ≤3。
- 前端源码改动后必须 `npm run build`（`dist` 是提交产物）。
- `qoder/` 前缀语义保持：对外模型 ID 为 `qoder/<display>`，`qoder_model_key()` 做 display→COSY key 映射。

---

### Task 1: 上游模型转换与同步服务 `sync_qoder_models`

**Files:**
- Create: `src/qb2api/accounts/qoder_model_sync.py`
- Test: `tests/test_qoder_model_sync.py`
- Modify: `src/qb2api/accounts/__init__.py`（导出）

**Interfaces:**
- Consumes: `httpx.AsyncClient`；`QoderError`（`src/qb2api/providers/qoder_auth.py:38`，`status_code` 属性）
- Produces:
  - `async def fetch_qoder_models(pat: str, *, client: httpx.AsyncClient | None = None) -> list[UpstreamModel]` — 调用 `GET https://api.qoder.com.cn/api/v1/cloud/models`，`Authorization: Bearer {pat}`；非 2xx 抛 `QoderError`（透传 status_code）；返回 `UpstreamModel` 列表
  - `def convert_upstream_models(items: list[UpstreamModel]) -> list[dict]` — 纯函数，把上游条目转为落库/落 snapshot 用的 dict（见下）
  - `async def sync_qoder_models(repository, resolver, *, client=None) -> SyncReport` — 从账号池取一个可用 qoder 账号（`chat` purpose、`verification_status == verified` 优先）的 PAT → fetch → convert → 事务内 upsert 全部 `source="upstream"` 记录 → 对**已停用**的旧记录执行改名迁移（见 Task 2）→ 返回报告
  - `class UpstreamModel`（dataclass）、`class SyncReport`（dataclass，字段见下）

- [ ] **Step 1: 写转换测试（先红）**

`UpstreamModel` 字段（严格对齐 issue 实测接口）：`id`、`display_name`、`is_enabled`、`is_new`、`is_vl`、`support_disable_reasoning`、`price_factor`、`max_input_tokens`、`default_context_window`、`available_context_windows`、`efforts`、`default_effort`。

转换规则（单测逐条断言）：
- `model_id = display_name`（对外 ID 与现有 config 一致，用 display 名），`metadata["cosy_key"] = id`
- `enabled = is_enabled`
- `capabilities = ["chat", "streaming"] + (["reasoning"] if support_disable_reasoning else []) + (["reasoning_effort"] if default_effort else []) + (["context_window"] if default_context_window else [])`
- `metadata = {"cosy_key", "is_new", "is_vl", "price_factor", "max_input_tokens", "default_context_window", "available_context_windows", "efforts", "default_effort", "source": "upstream"}`
- 直接丢弃接口中 `is_enabled` 以外的布尔字段，避免冗余落库。

测试代码（`tests/test_qoder_model_sync.py`）：

```python
import httpx

from qb2api.accounts.qoder_model_sync import (
    UpstreamModel,
    convert_upstream_models,
    fetch_qoder_models,
)

SAMPLE = {
    "id": "qmodel_38max",
    "display_name": "Qwen3.8-Max",
    "is_enabled": True,
    "is_new": True,
    "is_vl": False,
    "support_disable_reasoning": True,
    "price_factor": 1.0,
    "max_input_tokens": 131072,
    "default_context_window": 131072,
    "available_context_windows": [131072, 262144],
    "efforts": ["low", "high"],
    "default_effort": "high",
}


def test_convert_upstream_model():
    row = convert_upstream_models([UpstreamModel.from_dict(SAMPLE)])[0]
    assert row["model_id"] == "Qwen3.8-Max"
    assert row["enabled"] is True
    assert row["capabilities"] == ["chat", "streaming", "reasoning", "reasoning_effort", "context_window"]
    assert row["metadata"]["cosy_key"] == "qmodel_38max"
    assert row["metadata"]["is_new"] is True
    assert row["metadata"]["default_context_window"] == 131072
    assert row["metadata"]["available_context_windows"] == [131072, 262144]
    assert row["metadata"]["efforts"] == ["low", "high"]


def test_convert_disabled_model():
    item = UpstreamModel.from_dict({**SAMPLE, "is_enabled": False})
    row = convert_upstream_models([item])[0]
    assert row["enabled"] is False


def test_fetch_qoder_models_success(mocker):
    response = mocker.Mock(status_code=200, json=lambda: {"data": [SAMPLE]})
    client = mocker.Mock(get=mocker.AsyncMock(return_value=response))
    result = asyncio.run(fetch_qoder_models("pat-123", client=client))
    assert len(result) == 1
    assert result[0].id == "qmodel_38max"
    client.get.assert_awaited_once()
    args, kwargs = client.get.call_args
    assert args[0] == "https://api.qoder.com.cn/api/v1/cloud/models"
    assert kwargs["headers"]["Authorization"] == "Bearer pat-123"


def test_fetch_qoder_models_http_error():
    import pytest
    from qb2api.providers.qoder_auth import QoderError
    response = mocker.Mock(status_code=401)
    client = mocker.Mock(get=mocker.AsyncMock(return_value=response))
    with pytest.raises(QoderError) as exc:
        asyncio.run(fetch_qoder_models("pat", client=client))
    assert exc.value.status_code == 401
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/pytest tests/test_qoder_model_sync.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'qb2api.accounts.qoder_model_sync'`）

- [ ] **Step 3: 实现 `qoder_model_sync.py`**

```python
"""Qoder upstream model discovery and catalog sync (Control-side only)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from ..providers.qoder_auth import QoderError

MODELS_ENDPOINT = "https://api.qoder.com.cn/api/v1/cloud/models"
REQUIRED_FIELDS = (
    "id", "display_name", "is_enabled", "is_new", "is_vl",
    "support_disable_reasoning", "price_factor", "max_input_tokens",
    "default_context_window", "available_context_windows", "efforts", "default_effort",
)


@dataclass(frozen=True, slots=True)
class UpstreamModel:
    id: str
    display_name: str
    is_enabled: bool
    is_new: bool = False
    is_vl: bool = False
    support_disable_reasoning: bool = False
    price_factor: float = 1.0
    max_input_tokens: int = 0
    default_context_window: int = 0
    available_context_windows: list[int] = field(default_factory=list)
    efforts: list[str] = field(default_factory=list)
    default_effort: str = ""

    @classmethod
    def from_dict(cls, item: dict[str, Any]) -> "UpstreamModel":
        return cls(**{key: item[key] for key in REQUIRED_FIELDS if key in item})


@dataclass(frozen=True, slots=True)
class SyncReport:
    added: int = 0
    updated: int = 0
    disabled: int = 0
    models: list[dict[str, Any]] = field(default_factory=list)


async def fetch_qoder_models(
    pat: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[UpstreamModel]:
    """Fetch the official Qoder model list with a real PAT."""
    if client is None:
        async with httpx.AsyncClient(timeout=30) as owned:
            return await _fetch_once(owned, pat)
    return await _fetch_once(client, pat)


async def _fetch_once(client: httpx.AsyncClient, pat: str) -> list[UpstreamModel]:
    response = await client.get(
        MODELS_ENDPOINT,
        headers={"Authorization": f"Bearer {pat}"},
    )
    if not 200 <= response.status_code < 300:
        raise QoderError(
            f"Qoder models fetch failed (HTTP {response.status_code})",
            status_code=response.status_code,
        )
    payload = response.json()
    items = payload.get("data", []) if isinstance(payload, dict) else []
    return [UpstreamModel.from_dict(item) for item in items if isinstance(item, dict)]


def convert_upstream_models(items: list[UpstreamModel]) -> list[dict[str, Any]]:
    """Convert upstream models into `model_catalog` / snapshot-ready rows."""
    rows = []
    for item in items:
        capabilities = ["chat", "streaming"]
        if item.support_disable_reasoning:
            capabilities.append("reasoning")
        if item.default_effort:
            capabilities.append("reasoning_effort")
        if item.default_context_window:
            capabilities.append("context_window")
        rows.append(
            {
                "model_id": item.display_name,
                "display_name": item.display_name,
                "enabled": item.is_enabled,
                "capabilities": capabilities,
                "metadata": {
                    "cosy_key": item.id,
                    "is_new": item.is_new,
                    "is_vl": item.is_vl,
                    "price_factor": item.price_factor,
                    "max_input_tokens": item.max_input_tokens,
                    "default_context_window": item.default_context_window,
                    "available_context_windows": item.available_context_windows,
                    "efforts": item.efforts,
                    "default_effort": item.default_effort,
                    "source": "upstream",
                },
            }
        )
    return rows
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/pytest tests/test_qoder_model_sync.py -q`
Expected: PASS（注意：`UpstreamModel.from_dict` 缺字段时用 dataclass 默认值兜底，需在测试里覆盖 `is_new` 缺失场景——补一个 `test_from_dict_missing_optional` 断言默认值）

- [ ] **Step 5: 补账号选择 + 落库的同步函数测试**

`sync_qoder_models` 签名与逻辑：

```python
async def sync_qoder_models(repository, resolver, *, client=None) -> SyncReport:
    """Sync the catalog from upstream using one available qoder account PAT."""
    # 1) 从 resolver 取一个可用的 qoder chat PAT（优先级：verified 账号 → 任意可用）
    pat = await _pick_qoder_pat(resolver)
    if pat is None:
        raise QoderError("No available qoder account credential", status_code=409)
    # 2) fetch + convert
    items = await fetch_qoder_models(pat, client=client)
    rows = convert_upstream_models(items)
    # 3) 事务：upsert 全部行；对已有 source="upstream" 但本次未返回的模型 enabled=0（改名迁移）
    report = SyncReport()
    async with repository.transaction():
        for row in rows:
            previous = await repository.list_models("qoder")  # 注意：事务内调用，依赖 repository 实现
            # upsert + 统计 added/updated
        await _disable_stale(repository, rows)
    return report
```

> 已确认：`transaction()` 独占 `_operation_lock`，`_operation()` 检测到 `_transaction_owner is current_task` 时直接复用连接（`src/qb2api/accounts/repository.py:149-151`）——事务内调用 `list_models` 安全，无死锁。基线在事务内取即可。

`sync_qoder_models` 的事务逻辑（实现时按此写）：
- 事务开始前：`existing = await repository.list_models("qoder")` 取基线（含全部 source）
- 事务内：对每个 upstream row 调 `upsert_model(provider="qoder", model_id=row["model_id"], display_name=row["display_name"], capabilities=row["capabilities"], source="upstream", enabled=row["enabled"], metadata=row["metadata"])`；统计 `added`（基线无此 model_id）/ `updated`（基线有且内容不同）
- 事务内：对基线中 `source=="upstream"` 但本次未返回的 model_id 调 `set_model_enabled(provider="qoder", model_id=model_id, enabled=False)`，计数进 `disabled`（改名迁移：旧 display 名自动停用）

测试（mock repository + resolver）：

```python
async def test_sync_qoder_models_upsert_and_disable(mocker):
    from qb2api.accounts.qoder_model_sync import sync_qoder_models, SyncReport
    resolver = mocker.AsyncMock()
    resolver.credential.return_value = mocker.Mock(payload={"pat": "pat-1"})
    repo = mocker.Mock()
    repo.transaction.return_value = AsyncContextMock()
    repo.upsert_model = mocker.AsyncMock(side_effect=record_upsert)  # 记录调用
    repo.list_models = mocker.AsyncMock(return_value=old_catalog)   # 含旧 display 名的记录
    repo.set_model_enabled = mocker.AsyncMock(return_value=True)
    report = await sync_qoder_models(repo, resolver, client=client_mock)
    assert report.added == 1
    assert report.updated == 1
    assert report.disabled == 1
```

- [ ] **Step 6: 实现 `_pick_qoder_pat` 与同步主体**

```python
async def _pick_qoder_pat(resolver) -> str | None:
    # 用 registry.snapshot("chat") 的 qoder slot 列表 + resolver.credential() 逐个取 pat
    # 优先 verification_status == "verified" 的账号；全部失败返回 None
    raise NotImplementedError  # 实现时替换
```

- [ ] **Step 7: 运行全量 qoder 相关测试**

Run: `.venv/bin/pytest tests/test_qoder_model_sync.py tests/test_qoder*.py -q`
Expected: PASS

- [ ] **Step 8: 提交**

```bash
git add src/qb2api/accounts/qoder_model_sync.py src/qb2api/accounts/__init__.py tests/test_qoder_model_sync.py
git commit -m "feat(qoder): add upstream model sync service"
```

---

### Task 2: 模型同步端点 `POST /models/sync/qoder`

**Files:**
- Modify: `src/qb2api/admin/catalog_routes.py`（新增路由 + 复用 `_audit`、`_repository`）
- Test: `tests/test_catalog_routes.py`（或新建 `tests/test_model_sync_route.py`）

**Interfaces:**
- Consumes: `sync_qoder_models(repository, resolver)`（Task 1）；`admin_state(request).credential_resolver`（`src/qb2api/admin/dependencies.py`）
- Produces: `POST /models/sync/qoder` 返回 `{"status": "succeeded", "added": int, "updated": int, "disabled": int, "models": [...]}`；错误码：400（provider≠qoder）、401/403（凭据失效，透传 `QoderError.status_code`）、409（无可用账号）、502（上游异常）

- [ ] **Step 1: 写路由测试（先红）**

```python
async def test_sync_qoder_route_success(app_client, mocker):
    report = SyncReport(added=2, updated=1, disabled=1, models=[])
    mocker.patch("qb2api.admin.catalog_routes.sync_qoder_models", return_value=report)
    resp = await app_client.post("/models/sync/qoder")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["added"] == 2


async def test_sync_qoder_route_rejects_other_provider(app_client):
    resp = await app_client.post("/models/sync/codebuddy")
    assert resp.status_code == 400
```

- [ ] **Step 2: 实现路由**

```python
@router.post("/sync/{provider}")
async def sync_upstream_models(provider: str, request: Request) -> dict[str, Any]:
    await require_admin(request)
    if provider != "qoder":
        raise HTTPException(status_code=400, detail="unsupported_provider")
    state = admin_state(request)
    try:
        report = await sync_qoder_models(
            state.account_repo,
            state.credential_resolver,
        )
    except QoderError as error:
        await _audit(
            request,
            action="model.sync",
            resource_type=provider,
            resource_id="catalog",
            result="failed",
            metadata={"error_code": error.status_code},
        )
        raise HTTPException(status_code=error.status_code, detail="sync_failed") from error
    await _audit(
        request,
        action="model.sync",
        resource_type=provider,
        resource_id="catalog",
        metadata={"added": report.added, "updated": report.updated, "disabled": report.disabled},
    )
    return {
        "status": "succeeded",
        "added": report.added,
        "updated": report.updated,
        "disabled": report.disabled,
        "models": report.models,
    }
```

> 需要 `from qb2api.providers.qoder_auth import QoderError`（已有同类导入 `qoder` 的地方，检查文件头是否已 import；无则加）。路由注册：确认 `catalog_routes.py` 的 router 已被 `admin/router.py` 挂载到 `/models` 前缀下（当前 `@router.get("")` 与 `@router.post("/refresh")` 均为 `/models` 下，`/sync/{provider}` 会正确成为 `/models/sync/{provider}`，无需改挂载）。

- [ ] **Step 3: 运行路由测试**

Run: `.venv/bin/pytest tests/test_catalog_routes.py tests/test_model_sync_route.py -q`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add src/qb2api/admin/catalog_routes.py tests/test_model_sync_route.py
git commit -m "feat(admin): add model sync endpoint"
```

---

### Task 3: snapshot 携带上游模型（/v1/models 合并）

**Files:**
- Modify: `src/qb2api/models.py`（`ModelDefinition` 加可选 `metadata: dict | None = None` 字段）
- Modify: `src/qb2api/control/runtime_snapshot.py:46-52`（`models` 来源：config + `model_catalog` 中 `source="upstream"` 的记录合并，携带 `metadata`）
- Modify: `src/qb2api/runtime_snapshot.py`（`_model_payload` 输出可选 `metadata`；`_parse_model` 读可选 `metadata`，缺失/非 dict 为 `None`；协议版本保持不变）
- Test: `tests/test_runtime_snapshot.py`

**Interfaces:**
- Consumes: `repository.list_models("qoder")` 返回的 `_model_row` dict（含 `capabilities`、`metadata` 已解析）；`ModelDefinition` / `ModelCapabilities`（`src/qb2api/models.py`）
- Produces: snapshot 的 `models["qoder"]` 额外包含 `ModelDefinition(id=model_id, name=display_name, provider="qoder", capabilities=...)`，`max_context` 取 `metadata.default_context_window`（无则 config 默认）

- [ ] **Step 1: 写测试（先红）**

```python
async def test_snapshot_merges_upstream_catalog_models(mocker):
    service = RuntimeSnapshotService(mocker.Mock())
    service._runtime.settings.model_config_path = "config/models.json"
    service._runtime.account_registry = None
    service._runtime.credential_resolver = None
    service._runtime.account_repo = mocker.Mock()
    service._runtime.account_repo.list_models = mocker.AsyncMock(
        return_value=[
            {
                "provider": "qoder",
                "model_id": "Qwen3.8-Max",
                "display_name": "Qwen3.8-Max",
                "capabilities": ["chat", "streaming", "reasoning", "reasoning_effort", "context_window"],
                "source": "upstream",
                "enabled": True,
                "metadata": {
                    "cosy_key": "qmodel_38max",
                    "default_context_window": 131072,
                    "default_effort": "high",
                },
            }
        ]
    )
    snapshot = await service.build()
    qoder_models = {m.id for m in snapshot.models["qoder"]}
    assert "Qwen3.8-Max" in qoder_models
```

- [ ] **Step 2: 实现合并逻辑**

在 `RuntimeSnapshotService.build()` 中，`models = load_models_from_config(...)` 之后追加：

```python
upstream = await self._upstream_catalog_models()
if upstream:
    models = {key: list(value) for key, value in models.items()}
    models.setdefault("qoder", []).extend(upstream)
    models = {key: tuple(value) for key, value in models.items()}
```

`_upstream_catalog_models` 实现要点：
- `self._runtime.account_repo` 为 None 时返回 `[]`（env-only 部署无 DB）
- `list_models("qoder")` → 过滤 `source == "upstream"` 且 `enabled`
- 转换为 `ModelDefinition`：`id=model_id`、`name=display_name or model_id`、`provider="qoder"`、`capabilities=ModelCapabilities(**{k: (k in row["capabilities"]) for k in ("chat","streaming","tool_calling","reasoning","reasoning_effort","context_window","max_output_tokens")})`、`max_context=int(metadata.get("default_context_window", 0))`、`max_output=0`、`metadata={"cosy_key": row["metadata"].get("cosy_key"), ...}`（透传 `cosy_key` + `default_effort`，供 Task 4 使用）
- 去重：与 config 中已存在的同名 id 合并（后者覆盖前者——同名时用 `metadata` 非空的版本；实现时对 `models["qoder"]` 按 id 去重，upstream 优先）

- [ ] **Step 2b: 修改 `ModelDefinition` 与 snapshot 序列化**

`src/qb2api/models.py` 的 `ModelDefinition` 加字段（dataclass 末尾，带默认值，不影响既有构造）：

```python
@dataclass
class ModelDefinition:
    """Model definition with metadata."""
    id: str
    name: str
    provider: str
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)
    max_context: int = 128000
    max_output: int = 4096
    metadata: dict | None = None
```

`src/qb2api/runtime_snapshot.py` 的 `_model_payload` 增加：

```python
def _model_payload(model: ModelDefinition) -> dict[str, Any]:
    payload = { ... 现有字段 ... }
    if model.metadata:
        payload["metadata"] = model.metadata
    return payload
```

`_parse_model` 增加（`metadata` 缺失/非 dict 时为 `None`）：

```python
raw_metadata = value.get("metadata")
metadata = raw_metadata if isinstance(raw_metadata, dict) else None
return ModelDefinition(
    ...,
    metadata=metadata,
)
```

> 不需要 bump `RUNTIME_PROTOCOL_VERSION`：新字段可选，`from_payload` 对旧 payload 的 `value.get("metadata")` 为 `None` 走默认值，worker 能正常解析旧 snapshot；新 snapshot 对旧 worker 也不存在（Control/Worker 同进程版本部署）。若 `from_payload` 有逐字段严格校验导致必须加字段，则按 CLAUDE.md 规则 bump 版本——实现时先跑 `tests/test_runtime_snapshot.py` 确认既有 roundtrip 测试是否失败，失败则加版本号。

- [ ] **Step 3: 运行测试 + 全量 snapshot 测试**

Run: `.venv/bin/pytest tests/test_runtime_snapshot.py tests/test_qoder*.py -q`
Expected: PASS（若 `from_payload` 对新增 `metadata` 键有严格校验导致失败，按 Step 2b 提示处理：要么兼容默认值，要么 bump 协议版本——以测试结果为准）

- [ ] **Step 5: 提交**

```bash
git add src/qb2api/control/runtime_snapshot.py src/qb2api/runtime_snapshot.py tests/test_runtime_snapshot.py
git commit -m "feat(snapshot): merge upstream qoder models into runtime snapshot"
```

---

### Task 4: 运行时模型键映射合并（`qoder_payload.py`）

**Files:**
- Modify: `src/qb2api/providers/qoder_payload.py`（`qoder_model_key` 支持运行时映射）
- Modify: `src/qb2api/worker/proxy_state.py`（`refresh()` / `start()` 时把 `model_definitions["qoder"]` 的 `metadata.cosy_key` 合并进运行时映射）
- Test: `tests/test_qoder_model_key.py`（或并入现有 qoder payload 测试）

**Interfaces:**
- Consumes: `ModelDefinition.metadata`（`dict | None`，Task 3 新增的可选字段，默认 `None`）；`snapshot._model_payload` / `_parse_model` 对该字段做可选传输（缺失时默认 `None`，不参与严格校验）

- [ ] **Step 1: 写映射测试（先红）**

```python
from qb2api.providers.qoder_payload import qoder_model_key, set_runtime_model_keys, clear_runtime_model_keys

def test_runtime_key_overrides_static():
    set_runtime_model_keys({"Qwen3.8-Max": "qmodel_38max"})
    try:
        assert qoder_model_key("Qwen3.8-Max") == "qmodel_38max"
        assert qoder_model_key("Qwen3.7-Max") == "qmodel_latest"  # 静态表兜底
        assert qoder_model_key("Unknown-New") == "Unknown-New"     # 无映射时原样返回
    finally:
        clear_runtime_model_keys()
```

- [ ] **Step 2: 实现运行时映射**

```python
_runtime_model_keys: dict[str, str] = {}


def set_runtime_model_keys(mapping: dict[str, str]) -> None:
    _runtime_model_keys.clear()
    _runtime_model_keys.update(mapping)


def clear_runtime_model_keys() -> None:
    _runtime_model_keys.clear()


def qoder_model_key(model: str) -> str:
    """Map the public CLI display model to the COSY internal key."""
    if model in _runtime_model_keys:
        return _runtime_model_keys[model]
    return QODER_CLI_MODEL_KEYS.get(model, model)
```

- [ ] **Step 3: 在 `proxy_state.py` 合并映射**

在 `ProxyState.start()` / `refresh()` 中 `self.model_definitions` 更新后调用：

```python
from qb2api.providers.qoder_payload import set_runtime_model_keys


def _sync_runtime_model_keys(self) -> None:
    mapping = {
        model.id: model.metadata["cosy_key"]
        for model in self.model_definitions.get("qoder", [])
        if model.metadata
    }
    set_runtime_model_keys(mapping)
```

> **前置依赖（Task 3 已实现）**：`ModelDefinition` 新增可选 `metadata: dict | None = None` 字段；`RuntimeSnapshotService.build()` 合并上游模型时携带 `metadata={"cosy_key": item["metadata"]["cosy_key"], ...}`；`runtime_snapshot.py` 的 `_model_payload` 加 `"metadata"` 键（`None` 时省略），`_parse_model` 加 `metadata=_text_or_none(value.get("metadata"))`（或 `_parse_optional_dict`，缺失/非 dict 一律 `None`）——纯增量字段，`from_payload` 对缺失键走默认值，**无需 bump `RUNTIME_PROTOCOL_VERSION`**（worker 侧 `_parse_model` 只读新增可选键，旧 payload 无此键时默认 `None`；Control 侧 `to_payload` 对旧 `ModelDefinition` 同样输出 `None` 省略）。若实现中发现该字段必须参与严格校验，则改走 `available_models()` 合并降级方案并在此注明。

- [ ] **Step 4: 运行测试**

Run: `.venv/bin/pytest tests/test_qoder_model_key.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/qb2api/providers/qoder_payload.py src/qb2api/worker/proxy_state.py tests/test_qoder_model_key.py
git commit -m "feat(qoder): runtime model key mapping merge"
```

---

### Task 5: 前端"从上游同步"按钮（ModelsPage.vue）

**Files:**
- Modify: `frontend/src/pages/ModelsPage.vue`（header-actions 加按钮 + mutation）
- Test: `frontend/tests/ModelsPage.spec.ts`（若存在；否则新建）

**Interfaces:**
- Consumes: `POST /models/sync/qoder`（Task 2 端点）
- Produces: 同步按钮（仅 provider 过滤为 qoder 或空时启用）；成功后 invalidate `["models"]` 查询并 toast `新增 X · 更新 Y · 停用 Z`；401/403 错误 toast "Qoder 凭据失效，请在账号页检查"

- [ ] **Step 1: 写组件测试（先红）**

```ts
import { mount } from "@vue/test-utils";
import ModelsPage from "@/pages/ModelsPage.vue";
import { vi } from "vitest";

vi.mock("@/api/client", () => ({
  apiRequest: vi.fn().mockResolvedValue({ status: "succeeded", added: 2, updated: 1, disabled: 0, models: [] }),
}));

describe("ModelsPage sync button", () => {
  it("shows sync button and calls endpoint", async () => {
    const wrapper = mount(ModelsPage);
    const button = wrapper.find("button[aria-label='从上游同步']");
    expect(button.exists()).toBe(true);
    await button.trigger("click");
    expect(apiRequest).toHaveBeenCalledWith("/models/sync/qoder", { method: "POST" });
  });
});
```

- [ ] **Step 2: 实现按钮**

在 `ModelsPage.vue` 的 `<script setup>` 中加：

```ts
const syncUpstream = useMutation({
  mutationFn: () => apiRequest<{ added: number; updated: number; disabled: number }>("/models/sync/qoder", { method: "POST" }),
  onSuccess: async (result) => {
    notify("上游同步完成", { message: `新增 ${result.added} · 更新 ${result.updated} · 停用 ${result.disabled}`, tone: "success" });
    await queryClient.invalidateQueries({ queryKey: ["models"] });
  },
  onError: (error) => notify("上游同步失败", { message: String(error).includes("401") || String(error).includes("403") ? "Qoder 凭据失效，请在账号页检查" : String(error), tone: "error" }),
});
```

模板 header-actions 中"刷新目录"按钮旁：

```html
<button type="button" :disabled="syncUpstream.isPending.value || (provider !== '' && provider !== 'qoder')" @click="syncUpstream.mutate()"><RefreshCcw :class="{ spin: syncUpstream.isPending.value }" :size="16" />从上游同步</button>
```

> 图标可复用 `RefreshCcw` 或 `Boxes`；若想区分，从 `@lucide/vue` 引入 `CloudDownload`（确认组件库有该图标，没有就用现有图标）。

- [ ] **Step 3: 运行前端测试 + 类型检查**

Run: `cd frontend && npm run test -- ModelsPage.spec.ts && npm run typecheck`
Expected: PASS

- [ ] **Step 4: 构建 dist（必须）**

Run: `cd frontend && npm run build`
Expected: `dist/` 更新，无错误

- [ ] **Step 5: 提交**

```bash
git add frontend/src/pages/ModelsPage.vue frontend/tests/ModelsPage.spec.ts src/qb2api/web/dist
git commit -m "feat(ui): add upstream model sync button"
```

---

### Task 6: 流式 `reasoning_content` 透传开关

**Files:**
- Modify: `src/qb2api/worker/streaming.py`（`openai_stream` 中按开关剥离 `delta.reasoning_content`）
- Modify: `src/qb2api/config.py`（新增 `QB2API_STREAM_REASONING` 环境变量 → `Settings.stream_reasoning: bool = False`）
- Test: `tests/test_streaming.py`（或新建 `tests/test_reasoning_passthrough.py`）

**Interfaces:**
- Consumes: `settings.stream_reasoning`（默认 `False` = 剥离）；`openai_stream` 增加可选参数 `keep_reasoning: bool | None = None`（None 时读 settings，便于测试注入）
- Produces: `openai_stream(..., keep_reasoning=...)` 在流中过滤 `reasoning_content`

- [ ] **Step 1: 写测试（先红）**

```python
import json
import pytest
from qb2api.worker.streaming import openai_stream


def _chunks(chunks: list[bytes]) -> list[dict]:
    return [json.loads(c[6:].decode().strip()) for c in chunks if c.startswith(b"data: ") and c.strip() != b"data: [DONE]"]


async def _run_openai_stream(provider, *, keep_reasoning: bool | None = None, settings=None):
    from qb2api.worker.streaming import StreamLogContext
    context = StreamLogContext(provider_name="qoder", model="m", reasoning_effort=None, tool_calls_count=0)
    out = []
    async for chunk in openai_stream(
        provider=provider,
        request=mocker.Mock(model="m"),
        context=context,
        request_logger=None,
        keep_reasoning=keep_reasoning,
        settings=settings,
    ):
        out.append(chunk)
    return _chunks(out)


def test_stream_reasoning_stripped_by_default(mocker):
    provider = mocker.Mock()
    provider.stream.return_value = _async_iter([
        b'data: {"choices":[{"delta":{"reasoning_content":"think...","content":"hi"}}]}',
        b'data: [DONE]',
    ])
    chunks = asyncio.run(_run_openai_stream(provider))
    assert "reasoning_content" not in chunks[0]["choices"][0]["delta"]
    assert chunks[0]["choices"][0]["delta"]["content"] == "hi"


def test_stream_reasoning_kept_when_enabled(mocker):
    provider = mocker.Mock()
    provider.stream.return_value = _async_iter([
        b'data: {"choices":[{"delta":{"reasoning_content":"think...","content":"hi"}}]}',
        b'data: [DONE]',
    ])
    chunks = asyncio.run(_run_openai_stream(provider, keep_reasoning=True))
    assert chunks[0]["choices"][0]["delta"]["reasoning_content"] == "think..."
```

- [ ] **Step 2: 实现过滤**

```python
def _filter_reasoning(chunk: bytes, *, keep: bool) -> bytes:
    if keep:
        return chunk
    line = chunk.decode("utf-8", errors="replace").strip()
    if not line.startswith("data:") or line.startswith("data: [DONE]"):
        return chunk
    try:
        payload = json.loads(line[5:].strip())
    except json.JSONDecodeError:
        return chunk
    for choice in payload.get("choices", []):
        delta = choice.get("delta")
        if isinstance(delta, dict):
            delta.pop("reasoning_content", None)
    return f"data: {json.dumps(payload)}\n\n".encode()
```

在 `openai_stream` 的 chunk 循环内：

```python
keep = keep_reasoning if keep_reasoning is not None else getattr(settings, "stream_reasoning", False)
async for chunk in provider.stream(request):
    raw = chunk if isinstance(chunk, bytes) else str(chunk).encode()
    yield _filter_reasoning(raw, keep=keep)
```

> `openai_stream` 签名增加 `settings: Any | None = None` 与 `keep_reasoning: bool | None = None`。调用方 `openai_routes.py:48` 传 `settings=state.settings`。`anthropic_stream` 的 `openai_stream_to_anthropic` 转换路径（`src/qb2api/anthropic_stream.py`）若也逐 delta 透传，需同样处理——先检查该文件，若它在转换时丢弃 reasoning 则无需改（见 Step 3）。

- [ ] **Step 3: 检查 anthropic 转换路径**

`openai_stream_to_anthropic`（`src/qb2api/anthropic_stream.py`）如何消费 delta：若它只取 `content`/`tool_calls` 而忽略 `reasoning_content`，则 Anthropic 端点天然不受影响，本任务只覆盖 OpenAI 端点。若它也透传，则给 `anthropic_stream` 加同样的开关。

- [ ] **Step 4: 运行测试**

Run: `.venv/bin/pytest tests/test_streaming.py tests/test_reasoning_passthrough.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/qb2api/worker/streaming.py src/qb2api/worker/openai_routes.py src/qb2api/config.py tests/test_reasoning_passthrough.py
git commit -m "fix(stream): strip reasoning_content by default with opt-in flag"
```

---

### Task 7: 统一 9999 端口转发全部 proxy 路径（Ollama 兼容端点）

**背景（已定位根因）：** Hermes 等 Ollama 风格客户端走统一端口 9999（Control Plane）时，`GET /api/v1/models`、`GET /api/tags`、`POST /api/show` 返回 404。根因：`control/app.py` 的 `forward_proxy_requests` **只转发 `/v1/` 前缀**，Ollama 兼容端点落回 Control 自身路由（不存在）→ 404。而 Worker 的 `metadata_routes.py` 早已注册这些路由，`classify_path` 也将它们归为 `proxy_private`（`tests/test_admin_auth.py:46,48` 有断言）——语义上就该走 Worker，只是 Control 没转发。**设计决策：`classify_path` 是鉴权/归属分类的唯一事实源（CLAUDE.md），Control 转发判定直接复用 `classify_path(method, path) == "proxy_private"`，不另维护白名单，避免两处漂移。**

**Files:**
- Modify: `src/qb2api/control/app.py:112-117`（`forward_proxy_requests` 转发判定改为复用 `classify_path`）
- Test: `tests/test_control_app.py`（或 `tests/test_forward_proxy.py`，若已有转发测试文件则复用它）

**Interfaces:**
- Consumes: `classify_path`（`src/qb2api/admin/auth.py:47`，Control 侧 `request_auth.py` 已导入同款）；`_relay_to_worker`（`control/app.py:119`，原样保留）
- Produces: 所有 `classify_path == "proxy_private"` 的路径（`/v1/*`、`/api/v1/*`、`/api/tags`、`/api/show`、`/v1/props`、`/version` 归属见 Step 3 备注）从 9999 转发到 Worker；鉴权沿用现有链路（转发保留 Authorization 头 → Worker 边界 `verify_proxy_auth`，日志证明 Hermes 的请求已带有效 key）

- [ ] **Step 1: 写转发测试（先红）**

```python
async def test_forward_ollama_compat_paths(app_client, mocker):
    """Ollama 兼容端点应被 Control 转发到 Worker，而非 Control 自身 404。"""
    relay = mocker.patch("qb2api.control.app._relay_to_worker", new_callable=mocker.AsyncMock)
    relay.return_value = mocker.Mock(status_code=200)
    for path in ("/api/v1/models", "/api/tags", "/api/show"):
        await app_client.get(path)
    assert relay.await_count >= 3
    # 非 proxy 路径（admin）不应转发
    relay.reset_mock()
    await app_client.get("/api/admin/foo")
    assert relay.await_count == 0
```

（`app_client` fixture 用现有 control app 测试的 fixture；若无 `_relay_to_worker` 可直接 mock，需按测试文件现有模式调整。）

- [ ] **Step 2: 实现转发判定复用 `classify_path`**

`src/qb2api/control/app.py` 顶部加导入，`forward_proxy_requests` 改判定：

```python
from qb2api.admin.auth import classify_path


async def forward_proxy_requests(request: Request, call_next: Callable):
    """Unified-port entry: forward every proxy-classified path to the Proxy Worker."""
    if classify_path(request.method, request.url.path) != "proxy_private":
        return await call_next(request)
    return await _relay_to_worker(request)
```

- [ ] **Step 3: 核对归类边界（测试里逐条断言）**

`classify_path` 对以下路径的归类，确认测试覆盖：
- `GET /v1/models` → `proxy_private`（原本就转发，行为不变）
- `GET /api/v1/models`、`GET /api/tags`、`POST /api/show` → `proxy_private`（本次修复，新增断言）
- `GET /v1/props`、`GET /props`、`GET /version` → `proxy_private`（`/version` 在 Worker 是 `metadata_routes.py:32` 的 200；`/props` 在 `metadata_routes.py:70`。注意：Control 自己也注册了 `GET /version`（`control/app.py:72`）——改后 `GET /version` 会转发到 Worker，Control 自身的 `/version` 路由被"遮蔽"。验证：两个 `/version` 返回体字段不同（control 是 `"component": "control-plane"`，worker 是 `"component": "proxy-worker"`），确认无测试断言 Control 版本；若存在，修正该测试）

- [ ] **Step 4: 运行转发测试 + 鉴权测试**

Run: `.venv/bin/pytest tests/test_control_app.py tests/test_admin_auth.py -q`
Expected: PASS（`test_admin_auth` 的 `proxy_private` 分类断言不受影响——分类本来就对）

- [ ] **Step 5: 提交**

```bash
git add src/qb2api/control/app.py tests/test_control_app.py
git commit -m "fix(control): forward all proxy-classified paths on unified port"
```

---

### Task 8: 端到端验证与收尾

**Files:**
- Modify: 无（仅验证）
- Test: 运行全量测试

- [ ] **Step 1: 全量后端测试**

Run: `.venv/bin/pytest -q`
Expected: 全绿（含既有 test_design_alignment 等）

- [ ] **Step 2: 前端全量**

Run: `cd frontend && npm run test && npm run typecheck && npm run lint`
Expected: 全绿

- [ ] **Step 3: 代码门禁**

Run: `.venv/bin/ruff check src tests && .venv/bin/python -m compileall -q src/qb2api && .venv/bin/python tools/check_code_limits.py`
Expected: 无报错

- [ ] **Step 4: 手动冒烟（可选，需真实 PAT；统一 9999 端口验证）**

- 启动 Control：`.venv/bin/qb2api --mode control`
- 管理台 → 模型管理 → provider=qoder → 点"从上游同步"
- 预期：`Qwen3.8-Max` 出现且 enabled；旧 `Qwen3.8-Max-Preview` 变停用（若上游已改名）
- **统一端口验证（全部带 proxy key 打 9999）**：
  - `curl http://localhost:9999/v1/models` 确认 `qoder/Qwen3.8-Max` 存在
  - `curl http://localhost:9999/api/v1/models`、`/api/tags`、`/api/show`、`/v1/props` 确认不再 404（Task 7）
  - `curl http://localhost:9999/version` 确认返回 worker 版本（Task 7 后 `/version` 转发到 Worker；如无客户端依赖 Control 的 `/version`，此项仅记录）
- `curl http://localhost:9999/v1/chat/completions -d '{"model":"qoder/Qwen3.8-Max","stream":true,...}'` 确认流式响应无 `reasoning_content`；设 `QB2API_STREAM_REASONING=1` 重启后确认恢复

- [ ] **Step 5: 更新文档**

在 `docs/issues/2026-08-05-qoder-model-sync.md` 与 `docs/issues/2026-08-05-streaming-reasoning-content-passthrough.md` 的状态行标记"已实现"，并附实现摘要（本次计划的落地路径）。

- [ ] **Step 6: 提交文档**

```bash
git add docs/issues/
git commit -m "docs: mark qoder sync and reasoning passthrough issues implemented"
```

---

## Self-Review 记录

- **Spec 覆盖**：issue1 的 5 个改动点（同步服务 Task1、端点 Task2、元数据映射 Task1、映射表合并 Task4、/v1/models 合并 Task3）全覆盖；issue2 的剥离开关在 Task6；Hermes/Ollama 兼容端点的 404 在 Task7——**统一 9999 端口**：转发判定复用 `classify_path == "proxy_private"`（唯一事实源），不另维护白名单。issue1 的"定时同步""diff 展示""CodeBuddy 提取脚本"属于"后续可选"，本计划明确不做（YAGNI）。
- **风险已显式标注并已核实**：`ModelDefinition.metadata` 缺失 → Task 3 新增可选字段（纯增量、无需 bump 协议，Step 2b 已写实）；`transaction()` 锁语义 → 已确认事务内 `list_models` 安全（`repository.py:149-151`），Task 1 已按事务内基线实现。
- **类型一致性**：`SyncReport`（added/updated/disabled/models）贯穿 Task1→Task2→Task5；`qoder_model_key` 签名不变，新增 `set_runtime_model_keys`；`openai_stream` 新增参数带默认值，既有调用不受影响。

## 执行交接

计划已保存至 `docs/superpowers/plans/2026-08-06-qoder-model-sync-and-reasoning-passthrough.md`。

**两种执行方式：**

1. **Subagent-Driven（推荐）** — 每个任务派发独立子代理实现，任务间我做 review，迭代快、上下文干净
2. **Inline Execution** — 本会话内用 executing-plans 按任务批量执行，带检查点

**选哪种？**（回复 1 或 2）
