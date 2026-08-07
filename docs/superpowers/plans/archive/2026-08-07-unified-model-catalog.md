# 统一模型目录实施计划（Unified Model Catalog）

> 状态：已实施完成（2026-08-07），全部任务勾选。

**Goal:** 对外只暴露一套规范小写 ID 的统一模型列表；双端共有模型内部按提供商轮询 + pre-commit 故障转移；qoder 模型完全来自上游接口同步（不再使用 config 静态旧表）。

**Architecture:** Worker 端从按提供商分组的 snapshot 数据构建统一目录（`models_catalog.py`），双路线模型由 `ModelRouter`（复用 `DynamicProviderPool` 的轮询/冷却/故障转移语义）路由；Control 端 qoder 段改为只取 `model_catalog` 上游数据，并新增周期同步调度器保证"从接口拿到最新的"；snapshot 协议 v2 不变。

**Tech Stack:** Python 3.12 / FastAPI / pytest（asyncio 模式）/ httpx

## Global Constraints

- snapshot 协议 `RUNTIME_PROTOCOL_VERSION = 2` 不变；`runtime_snapshot.py`（协议）不改。
- 不新增第三方依赖。
- 凭据/token 不进日志/审计/SQLite（既有硬约束）；PAT 只在 Control 进程内存使用。
- 代码硬上限：单文件 ≤500 行、单函数 ≤50 行、圈复杂度 ≤10、嵌套 ≤3、位置参数 ≤3（`tools/check_code_limits.py`）。
- 流式响应只允许在首个下游 chunk 前 refresh / failover；输出后禁止跨提供商/跨账号重试。
- 上游 ID 必须原样保留给对应提供商：codebuddy 直传，qoder 经 `qoder_model_key()` 映射 cosy key。
- 规范 ID = 小写标准名（`deepseek-v4-flash`、`qwen3.8-max-preview`），不做自定义别名。
- qoder 模型列表唯一事实源 = `model_catalog` 表 source=upstream 记录；`config/models.json` 不再含 qoder 静态表（推翻 issues/archive/2026-08-05-qoder-model-sync.md 中"不改 config"的历史约束，用户 2026-08-07 明确要求）。
- 旧 `provider/model` 前缀 ID 与旧裸上游 ID 仍可解析（deprecated 兼容），但不再出现在 `/v1/models`。

---

### Task 1: 统一目录构建（models_catalog.py）

**Files:**
- Create: `src/qb2api/models_catalog.py`
- Modify: `src/qb2api/models.py`
- Test: `tests/test_models_catalog.py`

**Interfaces:**
- Consumes: `ModelDefinition`、`ModelCapabilities`（qb2api.models）
- Produces:
  - `normalize_model_id(model_id: str) -> str`：`model_id.strip().lower()`
  - `ModelRoute` dataclass：`provider: str`、`upstream_id: str`
  - `UnifiedModel` dataclass：`id`、`name`、`capabilities: ModelCapabilities`、`max_context: int`、`max_output: int`、`routes: tuple[ModelRoute, ...]`、`to_info() -> dict`（`id` 为规范 ID，`owned_by="qoderbuddy2api"`）、`route_for(provider) -> ModelRoute | None`、`canonicalize(provider, upstream_id) -> str | None`
  - `build_unified_catalog(per_provider: dict[str, list[ModelDefinition]], overrides: dict | None = None) -> dict[str, UnifiedModel]`：按 `normalize_model_id` 分组自动合并（能力逐字段 OR、max_context/max_output 取 max、name 优先 codebuddy），`overrides` 结构 `{canonical_id: {"name"?, "routes": [{"provider","upstream_id"}], "capabilities"?}}` 覆盖（routes 显式声明时整体替换；capabilities 显式声明时整体替换）
- 修改 `models.py`：`KNOWN_PROVIDERS = ("codebuddy", "qoder")`；`load_models_from_config` 只处理 KNOWN_PROVIDERS；删除 `DEFAULT_QODER_MODELS`；删除 `ModelDefinition.to_info`；新增 `load_unified_overrides(config_path) -> dict`（读顶层 `unified` 段，缺省返回 `{}`）

**Acceptance:**
- `build_unified_catalog` 对当前 config 产出 19 个条目；`deepseek-v4-flash` 双路线 `[("codebuddy","deepseek-v4-flash"),("qoder","DeepSeek-V4-Flash")]`；`kimi-k2.7` 与 `kimi-k2.7-code` 是两个独立条目；能力并集：`deepseek-v4-flash` 含 `context_window`（qoder 侧提供）
- `load_models_from_config` 忽略 `unified` 顶层 key；config 缺失时只有 codebuddy 默认表

### Task 2: ModelRouter（worker/model_router.py）

**Files:**
- Create: `src/qb2api/worker/model_router.py`
- Test: `tests/test_model_router.py`

**Interfaces:**
- Consumes: `ProviderRegistry`、`Provider`、`DynamicProviderPool`、`UnifiedModel`、`ChatCompletionRequest`、`ProviderUnavailableError`
- Produces:
  - `ModelRouter(Provider)`：`name = "model-router"`；构造 `ModelRouter(registry: ProviderRegistry, catalog: dict[str, UnifiedModel])`；`available_models() -> list[UnifiedModel]`（至少一条路线可用：路线 provider 不是 DynamicProviderPool 或 `has_available_slots`）；`stream(request)` / `complete(request)` 按 `request.model` 查路线并执行；`close()` 为 no-op

**Routing 语义（与 DynamicProviderPool 同款）：**
- 每条路线 = `(provider_name, pool, upstream_id)`；每模型维护 RR cursor（成功后推进）
- 委托前 `request.model = route.upstream_id`，`finally` 恢复原值（请求对象单飞，无并发共享）
- 跳过：冷却中（30s，`time.monotonic()` 比较）、池无可用 slots
- 失败：`request.telemetry["stream_committed"]` 为 True → 立即上抛（禁止重试）；否则记冷却、记 last_err、尝试下一条
- 全部失败：抛 `last_err`，无 last_err 时抛 `ProviderUnavailableError(f"{model}: no available routes")`

**Acceptance:**
- 双路线 RR 交替；首路线 pre-commit 失败自动切第二路线；首路线 post-commit 失败不重试、原样上抛；池无 slots 时跳过该路线；全部路线不可用抛 ProviderUnavailableError；单路线模型行为与直连池一致（upstream_id 改写正确）

### Task 3: ProxyState 改造

**Files:**
- Modify: `src/qb2api/worker/proxy_state.py`
- Test: `tests/test_app.py`（适配）、`tests/test_anthropic.py`（适配 `_build_model_index` 调用）

**Interfaces:**
- Consumes: `build_unified_catalog`、`load_unified_overrides`、`ModelRouter`
- Produces:
  - `ResolvedModel` dataclass：`canonical_id: str`、`provider: Provider`、`upstream_model: str`
  - `ProxyState.resolve_model(model: str) -> ResolvedModel`：
    - 含 `/`：legacy 前缀解析——`provider/id` 在 catalog 中按路线反查（route.provider == provider 且 route.upstream_id == id）；未知 → 400（保持错误风格）
    - 无 `/`：先按规范 ID 命中；再按 aliases/上游 ID 精确反查（legacy 裸 ID 兼容）；未知 → 400
    - 命中后：单路线 → `provider = registry.get(route.provider)`、`upstream_model = route.upstream_id`；多路线 → `provider = self.router`、`upstream_model = canonical_id`
  - `ProxyState.available_models() -> list[UnifiedModel]`（委托 router；router 为空时返回统一目录全量）
  - `ProxyState.router: ModelRouter | None`、`ProxyState.unified_catalog: dict[str, UnifiedModel]`
  - 替换 `_build_model_index`/`model_index`/`_resolve_prefixed`/`_resolve_unprefixed` 为 `_rebuild_catalog()`（start/refresh 调用：构建 unified_catalog + router；`_sync_runtime_model_keys` 保持不变，仍基于 qoder 段 metadata）

**Acceptance:**
- `resolve_model("deepseek-v4-flash")` 返回双路线 router 目标；`resolve_model("codebuddy/glm-5.2")`、`resolve_model("DeepSeek-V4-Flash")` 兼容解析到同一统一模型；`auto` 不再 Ambiguous；未知模型 400

### Task 4: 协议路由与 metadata 适配

**Files:**
- Modify: `src/qb2api/worker/openai_routes.py`、`src/qb2api/worker/anthropic_routes.py`、`src/qb2api/worker/metadata_routes.py`

**Interfaces:**
- Consumes: `ResolvedModel`
- 两条协议路由：`resolved = state.resolve_model(original_model)`；`provider = resolved.provider`（不再 `registry.get`）；`chat_request.model = resolved.upstream_model`；`telemetry_context` 的 `model_id = resolved.canonical_id`
- metadata：`/v1/models` 输出 `ModelInfo(id=model.id, owned_by="qoderbuddy2api")`（来自 `state.available_models()`）；`/v1/props` 的 `models` 改为统一 ID 数组

**Acceptance:**
- `/v1/models` 只含 19 个无前缀小写 ID；`/v1/props` models 为数组

### Task 5: Control 侧 qoder 只用上游数据

**Files:**
- Modify: `src/qb2api/control/runtime_snapshot.py`、`config/models.json`、`src/qb2api/admin/catalog_routes.py`
- Test: `tests/control/test_runtime_snapshot.py`（适配）

**Interfaces:**
- `RuntimeSnapshotService.build()`：`models = load_models_from_config(...)`（只剩 codebuddy）；qoder 段 = `_upstream_catalog_models()` 结果（可为空列表）；删除 `_merge_upstream_models`
- `config/models.json`：删除 `qoder` 段；保留 `codebuddy` 段；新增空 `unified: {}` 顶层段（说明用途）
- `admin/catalog_routes.py`：`_probe_model_id` 的 config fallback 仅适用于 codebuddy（qoder 无 enabled 模型时直接 `provider_model_unavailable`）
- `tests/control/test_runtime_snapshot.py`：`test_snapshot_merges_upstream_catalog_models` 改为断言 config 里的旧 qoder 模型不再出现在 snapshot（只含 upstream）

**Acceptance:**
- 有上游记录：snapshot qoder 段 = 上游模型（含 cosy_key metadata）；无上游记录：qoder 段为空；config 中旧 qoder 定义不再生效

### Task 6: qoder 模型自动同步调度器

**Files:**
- Create: `src/qb2api/control/model_sync_scheduler.py`
- Modify: `src/qb2api/config.py`、`src/qb2api/runtime.py`、`src/qb2api/control/lifecycle.py`
- Test: `tests/control/test_model_sync_scheduler.py`

**Interfaces:**
- `Settings`：`model_sync_enabled: bool = True`、`model_sync_interval_seconds: int = 21600`（环境变量 `QB2API_MODEL_SYNC_ENABLED` / `QB2API_MODEL_SYNC_INTERVAL_SECONDS`，`_observability_values` 中读取）
- `ModelSyncScheduler`：构造 `(settings, repo, registry, resolver, refresh_callback)`；`start()` 启动 asyncio task（循环：立即跑一次 sync，失败记 warning 不退出；然后 sleep interval）；`stop()`；`sync_once() -> bool`（调 `sync_qoder_models`，added/updated/disabled 总和 > 0 时调 `refresh_callback()` 并返回 True）
- `RuntimeServices`：`_start_model_sync_services()`（repo/registry/resolver 齐备且 `model_sync_enabled` 时创建）；`close()` 中 stop
- `lifecycle._start_control`：创建 scheduler 后注入 `refresh_callback=partial(_refresh_runtime, runtime=runtime, supervisor=supervisor, snapshot_service=snapshot_service)`

**Acceptance:**
- 单测：scheduler 启动即同步；变化 > 0 触发 refresh_callback；sync 抛异常不崩循环（下次继续）；interval 生效

### Task 7: 测试适配与新增

**Files:**
- Modify: `tests/test_app.py`（resolve_model 断言改为 ResolvedModel：`auto` 双路线、`codebuddy/deepseek-v3` 单路线、legacy 前缀兼容、未知 400）、`tests/test_anthropic.py`（`_build_model_index` → `_rebuild_catalog`）、`tests/integration/test_worker_protocol.py`（`/v1/models` 断言改为统一 ID 格式）、`tests/control/test_runtime_snapshot.py`
- Create: `tests/test_models_catalog.py`、`tests/test_model_router.py`、`tests/control/test_model_sync_scheduler.py`

**Acceptance:**
- `pytest tests/ -x -q` 全绿；`ruff check src tests`；`python -m compileall src`

### Task 8: 全量验证与文档同步

**Files:**
- Modify: `docs/superpowers/plans/2026-08-07-unified-model-catalog.md`（勾选完成项）、`README.md` / `README.zh.md`（如有模型列表说明则更新）、`docs/superpowers/specs/2026-08-07-unified-model-catalog-design.md`（状态改为已实施）

**Acceptance:**
- 全量 `pytest`、`ruff check`、`compileall` 通过；`tools/check_code_limits.py` 通过；`git diff --check` 通过
- 真实双进程 smoke：`/v1/models` 只含统一 ID；`deepseek-v4-flash` 连续请求遥测 provider 字段交替
