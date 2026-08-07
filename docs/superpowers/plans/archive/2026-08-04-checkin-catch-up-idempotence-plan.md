# 签到 catch-up 重复触发修复实施计划

> For agentic workers: Execute this plan task-by-task with focused verification.

Goal: 在同一补跑窗口内重启服务时，不再为已完成账号创建重复的 catch-up 批次。

Architecture: CheckinScheduler 在启动补跑前询问 CheckinService 的当天待执行状态；CheckinService 复用现有 registry 与 daily-state 持久化查询，不新增数据库表或改变已有批次契约。

Tech Stack: Python 3.11+, asyncio, aiosqlite, pytest。

## Global Constraints

- 不修改账号凭据、签到历史数据或上游请求协议。
- 不改变手动签到和 verify 的强制执行语义。
- 保留已有未提交改动，只修改本修复涉及的签到调度、服务和测试。

## Task 1: 调度器与服务层待执行判断

Files:
- Modify: src/qb2api/checkin/scheduler.py
- Modify: src/qb2api/checkin/service.py
- Test: tests/integration/test_scheduler.py
- Test: tests/integration/test_checkin_service.py

- [x] 为窗口内全部账号已完成的场景编写失败测试。
- [x] 在 CheckinService.has_pending_targets 中按当前日期和时区读取 daily state。
- [x] 在 _maybe_catch_up 创建批次前跳过无待执行账号的情况，并设置 already_complete。
- [x] 保留没有该能力的旧 service double 的兼容回退。
- [ ] 运行定向测试和静态检查。

## Task 2: 整合验证

Files:
- Inspect: git diff、签到相关测试和运行时配置。

- [ ] 运行 pytest tests/integration/test_scheduler.py tests/integration/test_checkin_service.py。
- [ ] 运行 .venv/bin/ruff check src/qb2api/checkin/scheduler.py src/qb2api/checkin/service.py tests/integration/test_scheduler.py tests/integration/test_checkin_service.py。
- [ ] 运行 .venv/bin/python -m compileall -q src/qb2api。
- [ ] 运行 git diff --check 并确认没有越界修改。
