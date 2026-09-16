# 签到 catch-up 重复触发修复设计

## 目标

避免 Control Plane 在同一 catch-up 窗口内反复重启时，重复创建没有实际网络请求的签到批次；同时不改变手动签到、账号验证和首次自动补跑行为。

## 现状与根因

调度器每次启动都会执行 _maybe_catch_up。只要当前时间位于计划时间后的 catch-up window 内，就会创建新的 catch_up 批次。账号级 checkin_daily_state 会阻止已完成账号再次请求上游，但批次仍会被创建并持久化，因此历史页面出现大量 SKIPPED 记录。

## 方案

在调度器决定等待 jitter 前，调用签到服务的 has_pending_targets：

1. 根据当前启用且已验证的签到账号生成 catch-up 目标。
2. 查询当天、当前时区的 checkin_daily_state。
3. 只要存在没有 CLAIMED 或 ALREADY_CHECKED_IN 终态的账号，就允许 catch-up 批次执行。
4. 如果没有目标或全部账号已完成，设置 catch_up_decision=already_complete，不创建批次。

旧的测试 doubles 或兼容服务没有该方法时，调度器保持原有行为，避免破坏外部调用契约。该修复不删除历史数据，也不改变手动/verify 的 skip_already_done=False 语义。

## 错误处理

目标状态读取异常沿用调度器现有 catch-up 异常处理，不吞掉真正的服务错误；只有明确返回 False 才跳过补跑。

## 验证

- 调度器在窗口内且有待执行账号时仍创建一个 catch-up 批次。
- 所有账号当天已终态时不创建批次，并报告 already_complete。
- 服务层能正确识别当天终态与待执行账号。
- 运行签到调度和服务集成测试、ruff、compileall、git diff --check。
