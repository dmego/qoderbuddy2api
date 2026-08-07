# Issue: Qoder 模型信息上游同步方案（模型页同步按钮 + 元数据利用）

- 日期：2026-08-05
- 严重级别：Medium（模型清单过时会导致请求失败与能力误报）
- 状态：已实现（2026-08-06，落地于 docs/superpowers/plans/2026-08-06-qoder-model-sync-and-reasoning-passthrough.md）
- 实现摘要：新增 `src/qb2api/accounts/qoder_model_sync.py`（`fetch_qoder_models`/`convert_upstream_models`/`sync_qoder_models`，按 provider 拉取官方 `/api/v1/cloud/models`，PAT 只在 Control 进程内存使用）；管理台新增 `POST /models/sync/qoder` 端点（同步按钮在 ModelsPage「从上游同步」）；结果同时落库 `model_catalog`（source="upstream"）并合并进 `RuntimeSnapshotService.build()` 的 `models["qoder"]`（/v1/models 与请求路由可见）；`ModelDefinition` 新增可选 `metadata` 字段并经 snapshot 协议 v2 透传（cosy_key/default_effort）；`qoder_model_key` 支持运行时映射（`set_runtime_model_keys`，proxy_state start/refresh 时从 snapshot 合并）；旧 display 名记录自动停用（改名迁移）。
- 已知限制（本变更不修复）：`config/models.json` 中已改名的旧 id（如 `Qwen3.8-Max-Preview`）因「不改 config/models.json」约束仍会出现在 /v1/models 并可被请求，命中失效 COSY key；上游同步只停用 `model_catalog` 中的旧 upstream 记录，无法移除 config 基线中的旧定义。
- 关联：`src/qb2api/worker/metadata_routes.py`（/v1/models）、`src/qb2api/providers/qoder_payload.py`（映射表）、`src/qb2api/admin/catalog_routes.py`（refresh）、`frontend/src/pages/ModelsPage.vue`

## 背景与问题

实测（2026-08-05，项目真实 PAT 调用 `GET https://api.qoder.com.cn/api/v1/cloud/models` 返回 200）：

1. **模型改名未跟上**：接口当前返回 `qmodel_38max`（display "Qwen3.8-Max"，`is_new: true`），项目 `config/models.json` 与 `QODER_CLI_MODEL_KEYS` 仍写死 `Qwen3.8-Max-Preview → qmodel_preview`。请求旧名会拿失效 key 打上游，大概率失败；新模型在 `/v1/models` 中不可见。
2. **接口元数据完全未利用**：接口每个模型都返回 `is_enabled`、`efforts`/`default_effort`、`default_context_window`/`available_context_windows`、`price_factor`、`max_input_tokens`、`support_disable_reasoning`。项目配置只有 chat/streaming/reasoning_effort/context_window 四个布尔能力。
3. **无账号级过滤**：`is_enabled=false` 的模型（账号不可用）仍会出现在 `/v1/models`。

## 方案概述

在模型管理页新增 **"从上游同步"按钮**（按 provider 维度），点击后：

1. 后端用当前账号池里的 Qoder PAT 调用官方 models 接口；
2. 将返回的模型清单转换为 `model_catalog` 记录（`source="upstream"`），完整写入能力与元数据；
3. 处理改名迁移（旧 display 名自动停用）；
4. 更新 `QODER_CLI_MODEL_KEYS` 映射表（运行时合并，不破坏配置文件）；
5. 前端展示同步结果（新增/更新/停用计数 + 失败原因）。

## 接口信息（已实测验证）

```
GET https://api.qoder.com.cn/api/v1/cloud/models
Authorization: Bearer {PAT}
```

返回 `{"data": [{id, display_name, is_enabled, is_new, is_vl,
  support_disable_reasoning, price_factor, max_input_tokens,
  default_context_window, available_context_windows, efforts, default_effort}]}`

- `id` 即 COSY 内部 key（`qmodel_latest`、`dmodel`、`kmodel`…），与 `QODER_CLI_MODEL_KEYS` 的 value 同源，可直接回填
- PAT 来源：沿用现有 `QoderSession` 认证链路（`src/qb2api/providers/qoder_auth.py`），或直接从凭据池取 `pat`
- 注意：国际站 `api.qoder.com` 与签到域 `openapi.qoder.com.cn` 均不可用（实测 401），固定使用 `api.qoder.com.cn`

## 后端改动

### 1. 新增同步服务 `src/qb2api/accounts/qoder_model_sync.py`

```python
async def sync_qoder_models(repository, credential_resolver, settings) -> SyncReport
```

- 从账号池取一个可用的 qoder 账号（`chat` purpose）的 PAT
- GET 官方 models 接口，失败时抛出带 status_code 的错误（透传 401/403 → 前端提示凭据失效）
- 转换：`id` 不变，`display_name` → model_id（保持与现有配置一致：模型对外用 display 名），内部 key 与 display 的映射写入返回结果

### 2. 新端点 `POST /models/sync/qoder`（`catalog_routes.py`）

```python
@router.post("/sync/{provider}")
async def sync_upstream_models(provider: str, request: Request) -> dict[str, Any]:
    # 仅支持 provider == "qoder"，其余返回 400
    # 事务内：upsert 每个模型（source="upstream"，完整元数据）
    # 改名迁移：旧 display 名（如 Qwen3.8-Max-Preview）置 enabled=0
    # 审计事件 action="model.sync" + 元数据（新增/更新/停用计数）
    return {"status": "succeeded", "added": n, "updated": n, "disabled": n, "models": [...]}
```

### 3. 元数据映射（写入 `model_catalog.metadata_json` / `capabilities_json`）

| 接口字段 | 落库位置 | 用途 |
| --- | --- | --- |
| `is_enabled` | `enabled` 列 | 账号不可用的模型不出现在 /v1/models |
| `display_name` | `display_name` 列 | 管理台展示 |
| `efforts` / `default_effort` | `metadata.efforts` / `metadata.default_effort`；capabilities 加 `reasoning_effort` | 后续可按模型钳制 reasoning_effort |
| `default_context_window` / `available_context_windows` | `metadata.context_windows`；capabilities 加 `context_window` | /v1/models 可暴露；供客户端选择上下文 |
| `max_input_tokens` | `metadata.max_input_tokens` | max_tokens 钳制依据 |
| `price_factor` | `metadata.price_factor` | 管理台成本展示（可选） |
| `support_disable_reasoning` | `metadata.disable_reasoning` | 能力展示 |
| `is_new` / `is_vl` | `metadata.is_new` / `metadata.is_vl` | 管理台标记新模型 |

### 4. 映射表运行时合并（`qoder_payload.py`）

`QODER_CLI_MODEL_KEYS` 保持为基线；同步后在内存中合并上游结果（`display_name → id`），`qoder_model_key()` 优先查运行时映射，miss 时回退静态表。这样：

- `Qwen3.8-Max → qmodel_38max` 立即生效（旧名 `Qwen3.8-Max-Preview` 仍走静态表兼容，但标记 deprecated）
- 未来上游新增模型无需改代码

### 5. `/v1/models` 与 model_definitions 合并（可选增强）

`ProxyState.available_models()` 目前读静态 config；同步后若 model_catalog 存在 `source="upstream"` 的 qoder 记录，优先合并（保持 `qoder/<display>` 前缀语义不变）。此步可放在后续迭代，第一版仅保证管理台与映射表正确。

## 前端改动（`ModelsPage.vue`）

在 header-actions 现有"刷新模型目录"按钮旁新增 **"从上游同步"** 按钮（仅 provider 过滤为 qoder 时启用）：

- 调 `POST /models/sync/qoder`
- 成功后用返回的 `{added, updated, disabled}` 显示结果提示，并 invalidate 模型列表查询
- 失败（401/403）提示"Qoder 凭据失效，请在账号页检查"

## 冲突与兼容

- **不改 `config/models.json`**：同步结果只进 `model_catalog`（DB），配置基线保留，避免破坏现有部署
- **改名迁移策略**：display 名变更时，旧名模型 `enabled=0`（保留记录，不删除，便于审计回滚）；用户可手动重新启用
- **`source` 语义**：`definition` = config 静态来源；`upstream` = 上游同步来源；列表页已有 source 筛选可直接用

## 测试与验证

1. 单元测试：`sync_qoder_models` 转换逻辑（mock 接口响应 → 断言 metadata/capabilities/enabled）
2. 集成测试：mock httpx 返回固定 JSON，调用 `POST /models/sync/qoder` 断言审计事件与 upsert 行
3. 手动验证：管理台点击同步 → 确认 `Qwen3.8-Max` 出现、`Qwen3.8-Max-Preview` 变停用；请求 `qoder/Qwen3.8-Max` 走通
4. 回归：`/v1/models` 仍返回 `qoder/<id>` 前缀；`/models/refresh`（config 同步）行为不变

## 后续可选

- 定时同步（每日一次，复用 checkin scheduler 模式）
- 同步结果页展示差异 diff（新增/更新/停用的字段级对比）
- CodeBuddy 无官方接口，维持 config 基线 + WorkBuddy.app product.json 提取脚本（另开 issue）
