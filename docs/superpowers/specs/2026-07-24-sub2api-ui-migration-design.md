# 2api Sub2API 管理台 UI 直接复用迁移设计

> 状态：已确认，待实施计划
>
> 决策日期：2026-07-24
>
> 适用范围：`frontend/` 管理台表现层；不改变 Python Control Plane、Worker、账号/凭据存储或第三方 Provider 协议。

## 1. 背景与目标

当前 2api 的管理台已经具备代理服务、模型、账号、凭据、用量、签到、设置和审计等功能，但视觉系统来自本项目自定义 CSS。它只借鉴了管理台的信息架构，没有复用 Sub2API 的实际布局、组件和 Tailwind 样式，因此与用户指定的参照页面存在明显差异。

本次重构的目标是将 2api 管理台迁移为 **Sub2API 的实际 UI 系统**：固定侧栏、粘性顶栏、内容工作区、Tailwind 主题 token、表格页布局、状态徽标、分页/确认框和移动端抽屉均由 Sub2API 前端源码派生；2api 仍保持自己的中文文案、业务数据和单管理员本地权限模型。

验收时，用户在 `/admin/` 看到的是 2api 功能填充的 Sub2API 风格管理台，而不是“参考了 Sub2API 的另一套自定义控制台”。

## 2. 已确认约束

| 约束 | 决策 |
| --- | --- |
| 复用方式 | 直接复制并修改 Sub2API 前端源码；不只复刻截图或色彩。 |
| 来源 | 本地 `/Users/dmego/vibeCoding/sub2api`，基线 commit `cb24522`。 |
| 许可证 | 对所有复制或衍生 UI 文件保留最小 LGPL-3.0-or-later 许可与版权/来源说明；该说明仅保留在源码仓库，不在产品 UI、登录页、浏览器 title 或运行日志展示。 |
| 品牌和资产 | 不复制 Sub2API 名称、Logo、产品描述、支付/订阅/渠道代码、用户管理、公告、上游图标资源或后端业务。 |
| 语言 | 所有 2api 面向用户的文字保持简体中文；不引入 Sub2API 的 `vue-i18n`、语言切换器和翻译包。 |
| 数据与权限 | 继续使用 `apiRequest()`、Pinia session、CSRF Cookie、当前 Vue Router 和所有 `/api/admin/*` 契约；不新增浏览器端 Token/Cookie 读取能力。 |
| 后端边界 | 不修改签到、OAuth、账号导入、Provider、SQLite 或 Worker 协议；不得因视觉重构触发任何真实上游请求。 |
| 质量边界 | 源码文件继续遵守项目的 300 行上限；不能将上游 1085 行 `AppSidebar.vue` 或 1152 行 `DataTable.vue` 原样落入 2api。 |

## 3. 方案比较与选择

### 方案 A：继续调整现有自定义 CSS

保留 `AdminShell.vue` 和 `styles/*.css`，仅继续修改颜色、间距和组件外观。成本最低，但用户已经明确否决这一方向：它无法获得 Sub2API 的结构、交互和视觉节奏。

### 方案 B：独立重绘 Sub2API 视觉

按截图或人工观察重新编写 shell、table 和主题。许可证最简单，但会再次产生“看起来不一样”的解释空间；也失去用户要求的直接源码复用。

### 方案 C：受控的前端源码移植（采用）

将 Sub2API 的 UI 源码作为明确上游基线迁入 2api，再以小型适配层替换其路由、i18n、身份、功能开关和业务依赖。复杂大组件按职责拆分，保留每一派生文件的许可证头和仓库级最小通知。这样既能最大化保留实际样式与交互，又不把 Sub2API 的业务系统、包袱和超长文件带进项目。

## 4. 视觉与交互系统

### 4.1 设计基线

2api 使用 Sub2API 的 Tailwind v3 主题组织方式：`primary` 青绿色阶、`accent/dark` slate 阶、`darkMode: 'class'`、系统中文字体栈、低层级的 `card`/`glass`/输入/按钮工具类。Sub2API 的青绿色 `primary-500`（`#14b8a6`）和深色 slate 表面是唯一的主视觉来源；2api 不再保留当前蓝灰 token 系统。

这是数据密集的单管理员操作台，不设置营销 Hero、虚假图表、装饰性 KPI 或引导文案。现有真实数据、空状态和错误反馈保留，并映射到一致的 Sub2API 表面、间距和状态色。动画仅限 150–300 ms 的抽屉、模态框、按钮/行 hover 和加载反馈；`prefers-reduced-motion` 禁用非必要位移。

### 4.2 框架布局

```text
桌面 ≥1024px
┌─────────────── 256 / 72px ───────────────┬──────────── sticky header ────────────┐
│ 品牌 · 折叠                               │ 当前页标题 · 服务状态 · 主题 · 退出      │
│                                           ├─────────────────────────────────────────┤
│ 运行 / 账号池 / 代理与模型 / 自动化 / 治理 │ 页面 actions / filters                    │
│                                           │ ┌────── table / cards / charts ───────┐ │
│ 主题切换                                  │ └──────── pagination / detail ─────────┘ │
└───────────────────────────────────────────┴─────────────────────────────────────────┘

移动 <1024px
┌────────── sticky header ──────────┐
│ 菜单 · 当前页 · 服务状态 · 操作    │
├───────────────────────────────────┤
│ 正常纵向滚动内容                   │
└───────────────────────────────────┘
  ↳ 菜单打开时为 Sub2API 样式的侧栏抽屉和可关闭遮罩
```

- 桌面侧栏初始宽 256px，可折叠为 72px；折叠状态由 2api 本地 UI store 记忆，不写入服务端设置。
- 顶栏粘性显示路由标题、Worker 观察状态、进行中请求数、主题切换和退出；不展示伪造额度或上游品牌信息。
- 页面主体采用 `p-4 / md:p-6 / lg:p-8`，表格页采用固定 actions、filters、可滚动表格区和分页区。
- 主题默认遵从系统偏好，用户切换后在浏览器本地持久化；首次加载在脚本前应用 class，以避免主题闪烁。

### 4.3 组件规则

| 组件 | 目标行为 |
| --- | --- |
| `ShellLayout` | 派生自 `AppLayout.vue`，只负责背景、侧栏宽度和页面 slot。 |
| `Sidebar` | 派生自 `AppSidebar.vue` 的布局与交互，但仅保留 2api 的 11 个路由组、折叠/移动抽屉和主题操作。 |
| `Header` | 派生自 `AppHeader.vue` 的粘性表面和操作区；数据来自当前 service/usage/metrics query。 |
| `TablePageLayout` | 直接迁入其固定/滚动区模型；在移动端恢复正常文档流。 |
| `DataTable` | 从上游 `DataTable.vue` 拆成表框、头部、行/空状态和可选虚拟行 composable；保留其响应式卡片、加载骨架和可访问表语义。现有 cursor 分页 API 不伪装为页码总数。 |
| `StatusBadge` | 派生自 `StatusBadge.vue`，以 2api 的 `presentation.ts` 映射服务、账号、签到、指标和操作状态。颜色不是唯一状态表达。 |
| `BaseDialog` / `ConfirmDialog` | 派生自上游对话框表面、焦点与按钮层级；维持当前删除、停用和批量操作确认契约。 |
| 表单与通知 | 保留已有 `AccountImportPanel`、`NotificationRegion`、`OperationStatus` 的业务逻辑，统一替换为新 token 和组件壳。 |

所有图标继续使用项目已依赖的 `@lucide/vue`，统一 16/18/20px 描边；图标按钮具有文字 `aria-label`、最小 44px 点击区域和明显焦点环。

## 5. 文件与依赖结构

```text
frontend/
  tailwind.config.js                         # 从 Sub2API 派生的主题 token
  postcss.config.js                          # Tailwind/PostCSS 编译入口
  THIRD_PARTY_NOTICES.md                     # 最小的来源与派生组件清单
  licenses/sub2api-LGPL-3.0.txt              # 上游 LICENSE 的完整副本
  src/styles/tailwind.css                    # Tailwind layers + 从 style.css 派生的全局工具类
  src/styles/sub2api-overrides.css            # 仅 2api 路由/页面适配，不复制旧蓝灰系统
  src/layouts/AdminShell.vue                  # 变为 ShellLayout 适配入口
  src/components/sub2api/layout/              # ShellLayout、Sidebar、Header、ThemeToggle
  src/components/sub2api/table/               # TablePageLayout、DataTableFrame、TableRows、useVirtualRows
  src/components/sub2api/feedback/            # StatusBadge、BaseDialog、ConfirmDialog
  src/stores/ui.ts                            # sidebarCollapsed、mobileNavOpen、theme
  src/pages/*.vue                             # 保持路由和 API 调用，仅按新组件/utility 组织视图
```

`tailwindcss@3`、`postcss` 和 `autoprefixer` 加入 `frontend` 的开发依赖；虚拟表格只在实际导入上游 virtualizer 时增加 `@tanstack/vue-virtual`。不引入 `vue-i18n`、Sub2API store、Sub2API Axios client 或其任一后端包。

直接复制/衍生文件在文件首部声明 `SPDX-License-Identifier: LGPL-3.0-or-later` 和其对应上游路径；`THIRD_PARTY_NOTICES.md` 记录上游仓库 URL、基线 commit、导入日期、文件映射和适配说明。该材料满足用户要求的“最小保留”，但不出现在应用界面。

## 6. 页面映射

| 2api 路由 | 迁移后承载方式 | 不变的业务能力 |
| --- | --- | --- |
| `/overview` | Sub2API dashboard 表面、真实指标区和图表区 | Worker、账号、模型、用量、积分/签到状态。 |
| `/service` | actions + 状态详情页 | 启动、停止、重启、健康和操作轮询。 |
| `/accounts`、`/credentials`、`/proxy-keys`、`/models` | `TablePageLayout` + table/filter/action toolbar + detail drawer/dialog | 查询、筛选、批量操作、导入、启停、删除和密钥管理。 |
| `/accounts/add`、`/accounts/:provider/:accountId` | 分组表单/详情工作区 | OAuth/导入发起、purpose 状态、凭据摘要和指标。 |
| `/usage`、`/checkin` | dense table + 真实统计/任务区 | Token 事件、用量汇总、签到运行、任务历史。 |
| `/settings`、`/audit` | 设置分组与审计表 | runtime 配置、调度开关、备份和审计筛选。 |
| `/login` | 独立、克制的 Sub2API 风格登录表面 | Admin Key 建立 session；没有上游账号或凭据输入。 |

页面不能因视觉迁移删除现有管理功能，也不能把任何 2api 查询结果改为模拟值。操作进行时显示 pending/disabled 语义；失败显示脱敏 API 错误、下一步和重试入口。

## 7. 可访问性、响应式与性能

- 在 375、768、1024、1440px 宽度验证。移动端不出现横向页面滚动；数据表单独允许可见的横向表格滚动或结构化卡片降级。
- 侧栏、抽屉、确认框、筛选和分页能用键盘完成；焦点不落到被遮罩内容；Esc 和关闭按钮均可关闭 overlay。
- 两个主题的正文对比度至少 4.5:1，次要文本/边框达到可辨识级别；状态同时提供文字与颜色。
- 图表延续 ECharts；大表仅在数据量确实超出普通 DOM 可接受范围时启用 `@tanstack/vue-virtual`，避免无意义依赖和布局抖动。
- 继续保留路由懒加载、TanStack Query 的既有缓存和请求频率；主题/侧栏状态不触发数据重新请求。

## 8. 验证与非目标

### 8.1 每阶段验证

1. `npm run typecheck`、`npm run lint`、`npm run test` 与 `npm run build`。
2. 现有 Playwright 控制台流程保持可用，并补充：主题切换、桌面折叠侧栏、移动抽屉、确认框键盘关闭和 Accounts 表格工作区。
3. 本地 Control Plane 启动后，通过非敏感 mock/本地数据验证所有 11 个路由、登录保护、读取列表和 Worker 管理操作；不登录真实第三方账号。
4. 对 light/dark、1440px、1024px、768px、375px 截图比对；视觉验收以 Sub2API 的布局层次、间距、交互和主题系统为准，而非仅颜色相近。

### 8.2 明确非目标

- 不迁移 Sub2API 的支付、订阅、用户角色、渠道、公告、图像批处理、onboarding、i18n、后端、Redis/PostgreSQL 或品牌资产。
- 不修改现有后端管理 API、数据库迁移、环境变量或真实 Provider 调用。
- 不以当前 UI 改造宣称 `AUTH-01`、`CB-CHECKIN-01`、`QD-CHECKIN-01` 已完成；它们仍受独立的真实账号授权门禁约束。

## 9. 设计自审

- **范围**：仅 frontend 表现层、必要的 frontend 构建依赖和来源文件；没有混入后端或真实协议实现。
- **一致性**：直接源码派生、最小 LGPL 保留、中文 2api 业务适配和无 UI 来源标识没有冲突。
- **可实施性**：上游超长组件在导入时按明确职责拆分，避免违反仓库 300 行限制；其内部第三方依赖不会被隐式带入。
- **验收性**：每个页面、主题、断点、数据边界和质量命令均有明确验证路径。
