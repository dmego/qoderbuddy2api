# Credits Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在账号池下新增可筛选、可自动刷新的积分监控页面。

**Architecture:** 页面复用现有管理 API 和 `MetricChart`，先读取账号列表与当前积分，再按账号读取积分历史，在浏览器内完成时间过滤、汇总和趋势计算。立即刷新使用现有 `/metrics/refresh` 异步任务并轮询结果；不新增后端 schema。

**Tech Stack:** Vue 3 Composition API、Vue Router、TanStack Vue Query、ECharts、Lucide。

## Global Constraints

- 未知、过期或失败的积分不得转换为 0。
- 不记录或展示 token、cookie、API key 或原始上游响应。
- 保持现有暗色终端样式、键盘焦点和移动端响应式行为。
- 不修改后端接口、依赖版本或用户已有后端改动。

---

### Task 1: 路由与导航

**Files:**
- Modify: `frontend/src/router.ts`
- Modify: `frontend/src/layouts/AdminShell.vue`

- [ ] 新增懒加载 `CreditsPage` 和 `/credits` 路由。
- [ ] 在“账号池”导航组加入“积分监控”，使用 `Coins` 图标并保持现有 active link 行为。

### Task 2: 积分监控页面

**Files:**
- Create: `frontend/src/pages/CreditsPage.vue`

- [ ] 定义账号、快照、历史行和刷新操作的 TypeScript 类型。
- [x] 使用分页读取 `/accounts?limit=100`、`/metrics/accounts?limit=500`，根据服务商读取 `/history/points` 或 `/history/quota`。
- [ ] 实现服务商、账号、快捷时间、自定义时间和图表范围筛选；筛选同时作用于摘要、图表和表格。
- [ ] 只采纳数值型 `total_remaining`，计算总积分、账号数、最近采集时间和窗口变化。
- [x] 使用 `MetricChart` 绘制总量或单账号平滑曲线，支持悬浮查看采样时间与积分值，并保留空数据状态。
- [ ] 实现自动刷新关闭/30 秒/1 分钟/5 分钟选择，并将其传入查询的 `refetchInterval`。
- [ ] 实现 `/metrics/refresh` mutation，显示运行状态、成功/失败通知和查询刷新。
- [ ] 使用现有 `PanelHeader`、`StatePill`、`NotificationRegion`，不暴露敏感字段。

### Task 3: 样式与验证

**Files:**
- Modify: `frontend/src/styles/data-controls.css`（仅在新页面需要时补充响应式类）

- [x] 为页面专用筛选和图表容器补充最小 CSS，复用现有 tokens。
- [ ] 运行 `npm run typecheck`、`npm run lint`、`npm run build`、`git diff --check`。
- [ ] 检查生成的 `src/qb2api/web/dist` 是否需要按仓库约定重建；若构建产生，仅保留本次构建产物。
