# 签到奖励与配额变化 Implementation Plan

> Execute this plan task-by-task in the current session with verification checkpoints.

**Goal:** 持久化 WorkBuddy/Qoder 签到奖励与配额前后变化，并在管理台显示奖励、差值和各配额包到期时间。

**Architecture:** 在现有 CheckInResult → CheckinExecutor → CheckinService → SQLite/API → Vue 链路中增加结构化 reward/quota_delta 字段。配额采集复用现有 MetricsProviders 与 Qoder/WorkBuddy 客户端；签到前读取最近快照，签到成功后立即刷新，数据库迁移采用幂等 ALTER TABLE。

**Tech Stack:** Python 3.11+, FastAPI, SQLite/aiosqlite, httpx, Vue 3 + TypeScript, Vitest/Playwright。

## Global Constraints

- 不记录或输出 Token、Cookie、Authorization、原始响应正文和凭据明文。
- 未知奖励或余额差值保持 null，禁止显示为 0。
- Qoder 用户配额、附加配额分别显示 expires_at；WorkBuddy 无有效字段时显示“未提供到期时间”。
- 保留现有用户未提交修改；前端修改后重建 src/qb2api/web/dist。

### Task 1: 后端数据契约与响应分类

**Files:**
- Modify: src/qb2api/checkin/models.py
- Modify: src/qb2api/checkin/qoder_status.py
- Modify: src/qb2api/checkin/codebuddy.py
- Test: tests/test_qoder_checkin_client.py, tests/test_checkin_clients.py

- [ ] 先为 Qoder status/claim 与 WorkBuddy claim 增加奖励字段、到期字段抽取断言，运行定向测试确认新增断言失败。
- [ ] 保持 CheckInResult.reward_credits，新增 quota_before, quota_after, quota_delta 为结构化可序列化字段；抽取接口仅允许额度、remaining、total、unit、expires_at 等安全字段。
- [ ] 让 Qoder/WorkBuddy 分类器返回 reward_credits；未知或缺失字段保持 None。
- [ ] 运行 pytest tests/test_qoder_checkin_client.py tests/test_checkin_clients.py -q。

### Task 2: SQLite 持久化与签到后配额刷新

**Files:**
- Modify: src/qb2api/accounts/schema.py, src/qb2api/accounts/repository.py
- Modify: src/qb2api/accounts/repo_checkin.py
- Modify: src/qb2api/checkin/executors.py, src/qb2api/checkin/service_execution.py, src/qb2api/checkin/batch.py
- Modify: src/qb2api/checkin/metrics_providers.py
- Test: tests/accounts/test_repository_transactions.py, tests/test_checkin_clients.py, tests/metrics/test_metrics_scheduler.py

- [ ] 为 checkin_attempts 增加 reward_credits、quota_before_json、quota_after_json、quota_delta_json、quota_observed_at 列，并在 migrate 中幂等补列。
- [ ] 在执行器中读取最近 fresh quota 快照，签到结果为 CLAIMED 或 ALREADY_CHECKED_IN 后调用对应配额采集器；采集失败不改变签到终态。
- [ ] 在同一事务中持久化奖励和配额结构，计算按包 remaining 差值；仅在前后均为数字时计算差值。
- [ ] 为旧行和未知字段返回 null，不把缺失值转换为零。
- [ ] 增加迁移、事务回滚、刷新成功/失败和差值计算测试，并运行定向测试。

### Task 3: 管理 API 与前端签到历史

**Files:**
- Modify: src/qb2api/admin/checkin_routes.py
- Modify: frontend/src/pages/CheckinPage.vue, frontend/src/pages/AccountDetailPage.vue
- Modify: frontend/tests/checkin.spec.ts, frontend/tests/account-detail.spec.ts

- [ ] API 的 attempt view 返回 reward_credits、quota_before、quota_after、quota_delta、quota_observed_at，并保持旧字段兼容。
- [ ] CheckinPage 和 AccountDetailPage 类型增加这些字段；签到历史窄行显示结果、奖励积分、余额变化，详情展开/辅助文本按配额包显示到期时间。
- [ ] Qoder 的 user_quota/add_on_quota 分别显示到期时间；WorkBuddy 仅显示上游提供的有效时间。
- [ ] 增加前端断言：奖励 100、差值未知不显示 0、两个配额包的到期时间分别出现。

### Task 4: 整合验证与构建

**Files:**
- Modify: src/qb2api/web/dist (generated)

- [ ] 运行 Python focused tests、ruff、compileall、代码限制检查。
- [ ] 运行前端 typecheck、lint、test、build、test:e2e。
- [ ] 检查 git diff --check、secret scan、工作区变更边界，并记录无法验证的真实上游行为。
