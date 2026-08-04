# Admin Console Visual System Implementation Plan

> **For agentic workers:** Execute this plan inline in the current session. Keep existing account dialog changes and finish with the verification gate.

**Goal:** 建立统一的管理台视觉基线，并重构账号详情页与账号摘要弹窗，使尺寸、字体、信息层级和响应式行为稳定一致。

**Architecture:** 先调整 tokens/main/console 的全局基线，再将 `AccountDetailPage.vue` 改成固定区域的详情布局；`AccessibleDrawer` 保留通用 drawer 能力并提供固定尺寸 dialog，账号池只保留摘要。业务 API 和操作逻辑不变。

**Tech Stack:** Vue 3、TypeScript、Vite、Vitest、ECharts、CSS custom properties、Lucide icons。

## Global Constraints

- 正文默认系统无衬线字体，ID/数值/版本号使用等宽字体。
- 页面标题 22px、面板标题 15px、正文 14px、辅助信息 12px，最小可读信息不低于 12px。
- 间距仅使用 8/12/16/24/32px；普通控件 36px，紧凑控件 32px，图标按钮点击区域不低于 40px。
- 保留现有暗色画布和琥珀强调色，不引入渐变、玻璃效果或装饰性动效。
- 前端源码变更后重建 `src/qb2api/web/dist`。

### Task 1: 统一全局视觉基线

**Files:**
- Modify: `frontend/src/styles/tokens.css`
- Modify: `frontend/src/styles/console.css`
- Modify: `frontend/src/styles/admin-shell.css`

- [ ] 调整字体变量，使正文使用 `--sans`，仅 `.mono`、代码和数据专用字段使用 `--mono`。
- [ ] 建立字号变量和统一控件高度，更新页面标题、面板标题、正文、辅助文字、按钮和表格文字。
- [ ] 将通用面板、状态区、空态和列表行的 padding/gap 归一到 8/12/16/24/32px。
- [ ] 修正 topbar、sidebar、page-stage 的基线字号和移动端间距。
- [ ] 保持现有色彩变量和 reduced-motion 行为。

### Task 2: 稳定详情 Dialog 容器

**Files:**
- Modify: `frontend/src/components/AccessibleDrawer.vue`
- Modify: `frontend/src/styles/data-controls.css`
- Test: `frontend/tests/operations-contracts.spec.ts`

- [ ] 为 dialog 模式设置固定视口范围、内部滚动、标题栏固定最小高度和移动端边距。
- [ ] 为 drawer/dialog 内容增加统一 `mono`、section spacing 和 overflow 规则。
- [ ] 保留遮罩关闭、Escape、Tab 焦点陷阱、焦点恢复，并测试账号池居中模式。

### Task 3: 重构 AccountDetailPage 信息架构

**Files:**
- Modify: `frontend/src/pages/AccountDetailPage.vue`
- Modify: `frontend/src/styles/data-controls.css`
- Modify: `frontend/src/styles/console.css`

- [ ] 保留现有查询、mutation、确认和通知逻辑。
- [ ] 将模板分为固定的标题栏、状态摘要、操作栏、用途设置、指标、趋势、凭据/请求/签到区。
- [ ] 使用固定列宽和 `min-height`，空态和长文本不改变区域关系。
- [ ] 将积分趋势容器固定为 280px，并保持 tooltip 数据展示。
- [ ] 删除重复说明文字，只保留用户执行操作所需的标签和时间。

### Task 4: 收敛账号池摘要弹窗

**Files:**
- Modify: `frontend/src/pages/AccountsPage.vue`

- [ ] 保留来源、身份、总体状态、用途状态、指标摘要、刷新和探测操作。
- [ ] 通过固定摘要区域和统一列表样式避免内容数量导致弹窗跳动。
- [ ] 不在摘要弹窗重复完整详情页的凭据、趋势、请求和签到历史。

### Task 5: 验证和嵌入式构建

**Files:**
- Modify: `src/qb2api/web/dist/**` (generated)

- [ ] 运行 `pnpm typecheck`、`pnpm lint`、`pnpm test`、`pnpm build`。
- [ ] 运行 `git diff --check`，确认生成产物与源码一致。
- [ ] 检查工作区差异仅包含本次视觉重构、测试、设计和生成产物。
