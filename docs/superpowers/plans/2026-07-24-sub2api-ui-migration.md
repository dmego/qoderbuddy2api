# 2api Sub2API UI 直接复用迁移 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan in the current branch, task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变 2api 管理 API、鉴权与真实 Provider 行为的前提下，将 `/admin/` 迁移为直接派生自 Sub2API 的完整中文管理台 UI 系统。

**Architecture:** `frontend/` 保持 Vue 3、Vue Router、Pinia 和 TanStack Query；新建一个从 Sub2API Tailwind/theme/layout/common components 派生的 UI 层，现有页面只保留数据加载和业务动作。`AdminShell` 是唯一的布局适配入口；主题和导航状态由单独 Pinia store 管理；表格/对话框/状态组件由小型可复用组件取代当前分散 CSS。

**Tech Stack:** Vue 3.5、TypeScript、Vite 8、Pinia 4、Vue Router 5、TanStack Vue Query、ECharts、Lucide、Tailwind CSS 3、PostCSS、Vitest、Playwright。

## Global Constraints

- 上游基线固定为 `/Users/dmego/vibeCoding/sub2api` commit `cb24522`；每个直接复制或衍生的 UI 文件保留 `SPDX-License-Identifier: LGPL-3.0-or-later` 和上游路径注释。
- 在 `frontend/THIRD_PARTY_NOTICES.md` 与 `frontend/licenses/sub2api-LGPL-3.0.txt` 保存最小来源/许可材料；不得在运行中的 UI、浏览器 title 或日志中显示 Sub2API 名称、Logo 或来源说明。
- 所有面向用户文案均为简体中文。不得引入 `vue-i18n`、Sub2API store、Axios client、用户/支付/订阅/渠道/公告/图像/onboarding 功能或品牌资产。
- 不修改 `src/qb2api/`、数据库、环境变量、Control Plane/Worker 契约或 `/api/admin/*` 请求形状；不得触发任何真实 OAuth、Provider、签到或刷新请求。
- 保持现有路由、`apiRequest()`、Pinia session、CSRF、TanStack Query keys 和业务操作；迁移不得把 Admin Key、Bearer、Cookie 或原始上游错误写入浏览器存储或 UI。
- 单个新增/重写源码文件不超过 300 行；将上游 `AppSidebar.vue`（1085 行）与 `DataTable.vue`（1152 行）按职责拆分，不整文件落入仓库。
- 使用 `@lucide/vue` 作为唯一图标来源。所有图标按钮有中文 `aria-label`、44px 最小交互面积和可见 `:focus-visible` 焦点环；状态必须同时表达颜色和文字。
- 正式验证必须覆盖 375、768、1024、1440px；light/dark 两主题；键盘侧栏、抽屉和对话框；并尊重 `prefers-reduced-motion`。
- npm 安装使用用户指定镜像：`npm --registry=https://registry.npmmirror.com`。每个下列任务是一个可独立审阅、可独立提交的大阶段，不拆成微小提交。

---

## Source-to-target map

| Upstream source | 2api target | Explicit adaptation |
| --- | --- | --- |
| `frontend/tailwind.config.js` | `frontend/tailwind.config.js` | 保留 `primary`/`accent`/`dark` token 和 dark class；删除上游业务专用 animation、支付按钮和无关 token。 |
| `frontend/src/style.css` | `src/styles/tailwind.css`、`src/styles/sub2api-overrides.css` | 保留 Tailwind layers、base、input/card/button/table/dialog style language；仅写 2api selector 覆盖。 |
| `components/layout/AppLayout.vue` | `components/sub2api/layout/ShellLayout.vue` | 删除 onboarding、auth store；接收 `collapsed` prop 和 slots。 |
| `components/layout/AppSidebar.vue` | `layout/SidebarNav.vue`、`SidebarBrand.vue`、`SidebarFooter.vue` | 删除 i18n、feature flags、image/onboarding/admin settings；只保留 2api routes、折叠、drawer、theme action。 |
| `components/layout/AppHeader.vue` | `layout/AdminHeader.vue` | 删除 locale、subscription、announcement 与外部链接；使用 2api route title/session/service query。 |
| `components/layout/TablePageLayout.vue` | `table/TablePageLayout.vue` | 保留 fixed/scroll/mobile layout，使用 2api slot names。 |
| `components/common/DataTable.vue` | `table/DataTableFrame.vue`、`DataTableRows.vue`、`TableEmptyState.vue`、`useVirtualRows.ts` | 保留 table/card responsive mode、loading/empty semantics；不假设 offset page API。 |
| `components/common/StatusBadge.vue` | `feedback/StatusBadge.vue` | 输入为 2api status string，文案经过 `presentation.ts`。 |
| `components/common/BaseDialog.vue`、`ConfirmDialog.vue` | `feedback/BaseDialog.vue`、`feedback/ConfirmDialog.vue` | 保留 focus restore、Esc、backdrop 和按钮 hierarchy，替换 i18n/icon imports。 |

## Task 1: 建立 Tailwind、来源材料与 UI 状态基础

**Files:**

- Create: `frontend/tailwind.config.js`
- Create: `frontend/postcss.config.js`
- Create: `frontend/src/styles/tailwind.css`
- Create: `frontend/src/styles/sub2api-overrides.css`
- Create: `frontend/src/stores/ui.ts`
- Create: `frontend/src/components/sub2api/layout/ThemeToggle.vue`
- Create: `frontend/THIRD_PARTY_NOTICES.md`
- Create: `frontend/licenses/sub2api-LGPL-3.0.txt`
- Create: `frontend/tests/ui-foundation.spec.ts`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `frontend/index.html`
- Modify: `frontend/src/main.ts`
- Modify: `frontend/src/styles/main.css`

**Consumes:** Sub2API `tailwind.config.js`, `src/style.css` and `LICENSE`; current 2api `main.ts`, `index.html` and Pinia initialization.

**Produces:** `useUiStore()` with stable `theme`, `navigationCollapsed`, `mobileNavigationOpen`, `setTheme()`, `toggleNavigation()` and `closeMobileNavigation()`; all later shell and pages import only this UI state and Tailwind/global CSS layers.

- [ ] **Step 1: Add failing UI state and root-class tests.**

```ts
import { createPinia, setActivePinia } from "pinia";
import { afterEach, describe, expect, it } from "vitest";
import { useUiStore } from "@/stores/ui";

describe("useUiStore", () => {
  afterEach(() => document.documentElement.classList.remove("dark"));

  it("applies explicit dark theme without persisting credentials", () => {
    setActivePinia(createPinia());
    const ui = useUiStore();
    ui.setTheme("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(localStorage.getItem("2api-ui-theme")).toBe("dark");
    expect(Object.keys(localStorage)).not.toContain("admin-key");
  });
});
```

- [ ] **Step 2: Run the focused test and record the expected red state.**

Run: `npm run test -- tests/ui-foundation.spec.ts`

Expected: failing module resolution for `@/stores/ui` before implementation.

- [ ] **Step 3: Add pinned frontend build dependencies with the configured mirror.**

Run:

```bash
npm install --save-dev tailwindcss@^3.4.0 postcss@^8.4.32 autoprefixer@^10.4.16 --registry=https://registry.npmmirror.com
```

Keep the generated lockfile; do not add `vue-i18n`, `axios` or any upstream business dependency.

- [ ] **Step 4: Implement the source-derived theme foundation.**

Create `tailwind.config.js` from the upstream color scale and set `content` to `./index.html` and `./src/**/*.{vue,ts}`. Create ESM `postcss.config.js` with `tailwindcss` and `autoprefixer`, because this frontend declares `type: module`. Put the following interface in `src/stores/ui.ts`:

```ts
export type Theme = "light" | "dark" | "system";

const THEME_STORAGE_KEY = "2api-ui-theme";

function readTheme(): Theme {
  const stored = localStorage.getItem(THEME_STORAGE_KEY);
  return stored === "light" || stored === "dark" || stored === "system" ? stored : "system";
}

function applyTheme(theme: Theme): void {
  const resolvedDark = theme === "dark" || (theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", resolvedDark);
}

export const useUiStore = defineStore("ui", () => {
  const theme = ref<Theme>(readTheme());
  const navigationCollapsed = ref(false);
  const mobileNavigationOpen = ref(false);

  function setTheme(next: Theme): void {
    theme.value = next;
    localStorage.setItem(THEME_STORAGE_KEY, next);
    applyTheme(next);
  }
  function toggleNavigation(): void { mobileNavigationOpen.value = !mobileNavigationOpen.value; }
  function closeMobileNavigation(): void { mobileNavigationOpen.value = false; }

  return { theme, navigationCollapsed, mobileNavigationOpen, setTheme, toggleNavigation, closeMobileNavigation };
});
```

`setTheme()` must write only `2api-ui-theme`, resolve `system` using `matchMedia("(prefers-color-scheme: dark)")`, and call `document.documentElement.classList.toggle("dark", resolvedDark)`. `index.html` must contain a small synchronous, exception-safe equivalent before module loading to eliminate first-paint theme flashing.

Add the upstream Tailwind base/components/utilities and the 2api-specific overrides as separate imports from `main.css`; remove imports of `tokens.css`, `admin-shell.css`, `console.css`, `data-controls.css`, `login.css` and `proxy-keys.css` only after their consumers are migrated in Task 6.

Use the direct-source header form on every new derived file:

```ts
/* SPDX-License-Identifier: LGPL-3.0-or-later
 * Derived from Wei-Shaw/sub2api frontend/<upstream path> at cb24522.
 */
```

The notice file must list repository URL, commit, import date, every source-to-target mapping in this plan, and state that no upstream marks/assets are shipped. The license file is an exact copy of `/Users/dmego/vibeCoding/sub2api/LICENSE`.

- [ ] **Step 5: Make the focused test green and run foundation checks.**

Run:

```bash
npm run test -- tests/ui-foundation.spec.ts
npm run typecheck
npm run lint
npm run build
```

Expected: the new UI store test and existing frontend type/lint/build checks pass.

- [ ] **Step 6: Commit the foundation phase.**

```bash
git add frontend/package.json frontend/package-lock.json frontend/tailwind.config.js frontend/postcss.config.js frontend/index.html frontend/src/main.ts frontend/src/styles/main.css frontend/src/styles/tailwind.css frontend/src/styles/sub2api-overrides.css frontend/src/stores/ui.ts frontend/src/components/sub2api/layout/ThemeToggle.vue frontend/THIRD_PARTY_NOTICES.md frontend/licenses/sub2api-LGPL-3.0.txt frontend/tests/ui-foundation.spec.ts
git commit -m "feat(frontend): add sub2api ui foundation"
```

## Task 2: 迁移 Sub2API 壳层、导航和主题操作

**Files:**

- Create: `frontend/src/components/sub2api/layout/ShellLayout.vue`
- Create: `frontend/src/components/sub2api/layout/SidebarBrand.vue`
- Create: `frontend/src/components/sub2api/layout/SidebarNav.vue`
- Create: `frontend/src/components/sub2api/layout/SidebarFooter.vue`
- Create: `frontend/src/components/sub2api/layout/AdminHeader.vue`
- Create: `frontend/src/components/sub2api/layout/routeTitles.ts`
- Create: `frontend/tests/shell-layout.spec.ts`
- Modify: `frontend/src/layouts/AdminShell.vue`
- Modify: `frontend/tests/app.spec.ts`
- Modify: `frontend/e2e/admin-console.spec.ts`

**Consumes:** Task 1 `useUiStore`, `ThemeToggle`, Tailwind styles, router names, `useSessionStore`, existing `apiRequest` queries.

**Produces:** a source-derived responsive shell used by every protected route, with no Sub2API auth/i18n/feature-flag imports and no changes to `/admin/` routing.

- [ ] **Step 1: Add failing shell behavior tests.**

```ts
it("collapses desktop navigation and opens a keyboard-labelled mobile drawer", async () => {
  const wrapper = mount(AdminShell, { global: { plugins: [createPinia(), VueQueryPlugin, router] } });
  await wrapper.get('button[aria-label="收起导航"]').trigger("click");
  expect(wrapper.get("aside").classes()).toContain("lg:w-[72px]");
  await wrapper.get('button[aria-label="展开导航"]').trigger("click");
  expect(wrapper.get('button[aria-label="关闭导航"]').attributes("aria-expanded")).toBe("true");
});
```

Update Playwright to assert the theme button, sticky header, desktop collapsed rail and mobile drawer contain the existing 2api labels.

- [ ] **Step 2: Run focused shell tests for the red state.**

Run: `npm run test -- tests/shell-layout.spec.ts tests/app.spec.ts`

Expected: missing source-derived shell controls before migration.

- [ ] **Step 3: Migrate layout code into small source-derived components.**

Implement `ShellLayout.vue` from upstream `AppLayout.vue` with the upstream `bg-gray-50 dark:bg-dark-950`, mesh background, and `lg:ml-64` / `lg:ml-[72px]` geometry. Pass layout state explicitly rather than importing upstream app/onboarding/auth stores.

Implement `SidebarNav.vue` with exactly these groups and route names:

```ts
const groups = [
  { label: "运行", items: ["overview", "service"] },
  { label: "账号池", items: ["accounts", "credentials"] },
  { label: "代理与模型", items: ["proxy-keys", "models", "usage"] },
  { label: "自动化", items: ["checkin"] },
  { label: "治理", items: ["settings", "audit"] },
] as const;
```

Resolve icon and label metadata locally with `@lucide/vue`; nav clicks call `closeMobileNavigation()`. Desktop collapse is controlled by `navigationCollapsed`; mobile uses a fixed drawer, backdrop close, `aria-expanded`, and focusable close button.

Implement `AdminHeader.vue` from upstream header surface only: derive the current title via `routeTitles.ts`, retain 2api service/in-flight queries, add `ThemeToggle`, and call existing `session/logout` then `router.replace("/login")`. Do not display upstream subscription, locale, announcement, avatar, URL or admin-settings widgets.

Replace `AdminShell.vue` with composition of those components and `<RouterView />`; remove local `navigationCollapsed`, viewport listeners and legacy class names from it.

- [ ] **Step 4: Run shell/unit and browser validation.**

Run:

```bash
npm run test -- tests/shell-layout.spec.ts tests/app.spec.ts
npm run test:e2e -- --grep "登录不持久化密钥|窄屏导航"
npm run typecheck
npm run lint
npm run build
```

Expected: the header/sidebar preserve every approved 2api route, navigation works at 390px, and no legacy shell selector is needed by the migrated shell.

- [ ] **Step 5: Commit the shell phase.**

```bash
git add frontend/src/components/sub2api/layout frontend/src/layouts/AdminShell.vue frontend/tests/shell-layout.spec.ts frontend/tests/app.spec.ts frontend/e2e/admin-console.spec.ts
git commit -m "refactor(frontend): migrate admin shell"
```

## Task 3: 迁移表格、状态、对话框和通知基础组件

**Files:**

- Create: `frontend/src/components/sub2api/table/TablePageLayout.vue`
- Create: `frontend/src/components/sub2api/table/DataTableFrame.vue`
- Create: `frontend/src/components/sub2api/table/DataTableRows.vue`
- Create: `frontend/src/components/sub2api/table/TableEmptyState.vue`
- Create: `frontend/src/components/sub2api/table/CursorPagination.vue`
- Create: `frontend/src/components/sub2api/feedback/StatusBadge.vue`
- Create: `frontend/src/components/sub2api/feedback/BaseDialog.vue`
- Create: `frontend/src/components/sub2api/feedback/ConfirmDialog.vue`
- Create: `frontend/tests/sub2api-primitives.spec.ts`
- Modify: `frontend/src/components/PaginatedTable.vue`
- Modify: `frontend/src/components/StatePill.vue`
- Modify: `frontend/src/components/AccessibleDrawer.vue`
- Modify: `frontend/src/components/ConfirmDialog.vue`
- Modify: `frontend/src/components/NotificationRegion.vue`

**Consumes:** Task 1 theme; current `statusLabel()`, `useCursorPager()`, notifications and operation contracts.

**Produces:** one visual primitive family that all data-management pages can use without changes to data fetches or mutations.

- [ ] **Step 1: Add failing primitive contract tests.**

```ts
it("renders a status as text and tone, not color only", () => {
  const wrapper = mount(StatusBadge, { props: { value: "action_required" } });
  expect(wrapper.text()).toContain("需要处理");
  expect(wrapper.attributes("data-tone")).toBe("warning");
});

it("returns focus to the trigger after a confirmed dialog closes", async () => {
  const trigger = document.createElement("button");
  document.body.append(trigger);
  trigger.focus();
  const wrapper = mount(BaseDialog, { props: { open: true, title: "删除账号" } });
  await wrapper.get('button[aria-label="关闭对话框"]').trigger("click");
  expect(document.activeElement).toBe(trigger);
});
```

- [ ] **Step 2: Run primitive tests and capture the red state.**

Run: `npm run test -- tests/sub2api-primitives.spec.ts`

Expected: imports are unavailable before the source-derived components exist.

- [ ] **Step 3: Implement the source-derived primitives.**

`TablePageLayout.vue` preserves upstream `actions`, `filters`, `table`, `pagination` slots and desktop fixed/scroll/mobile behavior. `DataTableFrame.vue` provides the Sub2API table shell and emits no data requests. `DataTableRows.vue` is generic over keyed rows and exposes `#row` / `#card` slots; it supports `loading`, `emptyTitle`, `emptyDescription` and `mobileCards` rather than re-implementing each page table.

`CursorPagination.vue` accepts:

```ts
defineProps<{
  page: number;
  hasPrevious: boolean;
  hasNext: boolean;
  total?: number;
}>();
defineEmits<{ previous: []; next: [] }>();
```

It must never claim a nonexistent last page or page-size selector when a 2api cursor response does not provide that information.

`StatusBadge.vue` uses `statusLabel()` plus an explicit tone map. `BaseDialog.vue` ports the upstream focus lifecycle with `open`, `title`, `closeLabel` and `@close`; it closes on backdrop and Escape, traps focus while visible and restores the recorded trigger. `ConfirmDialog.vue` retains existing `danger`, confirmation text and emitted `confirm`/`cancel` events, with Chinese defaults.

Turn the existing components into thin compatibility wrappers only where needed, then update imports page-by-page in Tasks 4–6. Do not delete a legacy component until `rg` proves no remaining import.

- [ ] **Step 4: Verify primitives and existing behavior contracts.**

Run:

```bash
npm run test -- tests/sub2api-primitives.spec.ts tests/operations-contracts.spec.ts tests/account-detail.spec.ts
npm run typecheck
npm run lint
npm run build
```

Expected: drawer/dialog focus behavior and existing cursor/filter contracts remain intact.

- [ ] **Step 5: Commit the primitives phase.**

```bash
git add frontend/src/components/sub2api frontend/src/components/PaginatedTable.vue frontend/src/components/StatePill.vue frontend/src/components/AccessibleDrawer.vue frontend/src/components/ConfirmDialog.vue frontend/src/components/NotificationRegion.vue frontend/tests/sub2api-primitives.spec.ts
git commit -m "feat(frontend): add sub2api data primitives"
```

## Task 4: 迁移运行、服务、用量与签到工作区

**Files:**

- Modify: `frontend/src/pages/OverviewPage.vue`
- Modify: `frontend/src/pages/ServicePage.vue`
- Modify: `frontend/src/pages/UsagePage.vue`
- Modify: `frontend/src/pages/CheckinPage.vue`
- Modify: `frontend/src/components/MetricChart.vue`
- Modify: `frontend/tests/operations-console.spec.ts`
- Modify: `frontend/tests/usage.spec.ts`
- Modify: `frontend/tests/checkin.spec.ts`
- Modify: `frontend/e2e/admin-console.spec.ts`

**Consumes:** Tasks 1–3. Existing Overview/Service/Usage/Checkin queries, mutations and safe operation polling remain the only data layer.

**Produces:** the four operational pages rendered through the Sub2API shell, toolbar, table/card and status language.

- [ ] **Step 1: Extend existing page tests before visual migration.**

Add assertions that `UsagePage` and `CheckinPage` contain `TablePageLayout` landmarks (`data-testid="table-page-layout"`), that Service preserves typed stop confirmation, and that Overview displays real zero-state copy rather than synthetic metrics.

- [ ] **Step 2: Run focused red tests.**

Run:

```bash
npm run test -- tests/operations-console.spec.ts tests/usage.spec.ts tests/checkin.spec.ts
```

Expected: added layout landmark assertions fail before page conversion.

- [ ] **Step 3: Convert the operational pages without altering API calls.**

- `OverviewPage.vue`: use source-derived dashboard surfaces for the existing service, account, model, usage, metric and check-in query values. Retain the ECharts component only for actual usage rollups and show an explicit no-data state when no rollups exist.
- `ServicePage.vue`: make actions a `TablePageLayout` actions slot; retain start/stop/restart and the existing confirmation/operation polling sequence exactly.
- `UsagePage.vue`: put filter controls in `filters`, rollup/event detail in `table`, and cursor movement in `pagination`; retain CSV export request and current filters.
- `CheckinPage.vue`: retain manual batch confirmation, durable operation ID and history. It must not make a check-in request automatically on mount or due to layout refresh.
- `MetricChart.vue`: source its surface colors from CSS variables/classes derived from Tailwind theme, call `resize()` via a cleaned-up `ResizeObserver`, and render no fake series.

- [ ] **Step 4: Run focused regression and browser smoke tests.**

Run:

```bash
npm run test -- tests/operations-console.spec.ts tests/usage.spec.ts tests/checkin.spec.ts
npm run test:e2e -- --grep "可从控制台启动并停止|主要管理页面可访问"
npm run typecheck
npm run lint
npm run build
```

Expected: all original side-effect confirmation paths and safe empty states work under the new visual primitives.

- [ ] **Step 5: Commit the operations-pages phase.**

```bash
git add frontend/src/pages/OverviewPage.vue frontend/src/pages/ServicePage.vue frontend/src/pages/UsagePage.vue frontend/src/pages/CheckinPage.vue frontend/src/components/MetricChart.vue frontend/tests/operations-console.spec.ts frontend/tests/usage.spec.ts frontend/tests/checkin.spec.ts frontend/e2e/admin-console.spec.ts
git commit -m "refactor(frontend): migrate operations pages"
```

## Task 5: 迁移账号池、凭据、密钥、模型和账号详情

**Files:**

- Modify: `frontend/src/pages/AccountsPage.vue`
- Modify: `frontend/src/pages/AddAccountPage.vue`
- Modify: `frontend/src/pages/AccountDetailPage.vue`
- Modify: `frontend/src/pages/CredentialsPage.vue`
- Modify: `frontend/src/pages/ProxyKeysPage.vue`
- Modify: `frontend/src/pages/ModelsPage.vue`
- Modify: `frontend/src/components/AccountImportPanel.vue`
- Modify: `frontend/tests/accounts.spec.ts`
- Modify: `frontend/tests/account-detail.spec.ts`
- Modify: `frontend/tests/proxy-keys.spec.ts`
- Modify: `frontend/tests/operations-contracts.spec.ts`

**Consumes:** Tasks 1–3 and existing account/mutation security rules.

**Produces:** all account-management workflows in source-derived table/detail/form visual language, without rendering secret fields.

- [ ] **Step 1: Add visual-structure assertions while preserving security assertions.**

For Accounts, assert the actions/filters/table/pagination slots exist and a selected account opens a labelled drawer. For Credentials/Proxy Keys/Add Account, assert each sensitive field has a visible `<label>` and no test fixture value appears after save/import. Retain the existing checks for one-time key reveal, read-only env accounts and cookie non-rendering.

- [ ] **Step 2: Run focused red tests.**

Run:

```bash
npm run test -- tests/accounts.spec.ts tests/account-detail.spec.ts tests/proxy-keys.spec.ts tests/operations-contracts.spec.ts
```

Expected: new source-derived structural assertions fail before conversion, while current security tests establish a baseline.

- [ ] **Step 3: Convert all account-management views.**

- Place Accounts, Credentials, Proxy Keys and Models into `TablePageLayout`; pass their current query filters and `useCursorPager()` handlers unchanged to `CursorPagination`.
- Render provider, purpose, verification, metrics and enabled values through `StatusBadge` or explicit text. Do not expose fingerprints, encrypted credential values, raw Cookie, Bearer, refresh token or one-time key after its existing permitted reveal flow.
- Convert selected account/model/key details to `BaseDialog` or the existing focus-safe drawer shell styled by its source-derived equivalent.
- Convert Add Account and Account Detail to Sub2API-derived grouped form/detail surfaces; retain the current OAuth/import requests and never initiate login/refresh merely to populate the page.
- Keep destructive disable/delete/revoke actions behind the existing confirmation component. Preserve disabled forms during a pending mutation.

- [ ] **Step 4: Run security and interaction regression checks.**

Run:

```bash
npm run test -- tests/accounts.spec.ts tests/account-detail.spec.ts tests/proxy-keys.spec.ts tests/operations-contracts.spec.ts
npm run typecheck
npm run lint
npm run build
```

Expected: account, credential and proxy-key security assertions pass with the new layout.

- [ ] **Step 5: Commit the account-management phase.**

```bash
git add frontend/src/pages/AccountsPage.vue frontend/src/pages/AddAccountPage.vue frontend/src/pages/AccountDetailPage.vue frontend/src/pages/CredentialsPage.vue frontend/src/pages/ProxyKeysPage.vue frontend/src/pages/ModelsPage.vue frontend/src/components/AccountImportPanel.vue frontend/tests/accounts.spec.ts frontend/tests/account-detail.spec.ts frontend/tests/proxy-keys.spec.ts frontend/tests/operations-contracts.spec.ts
git commit -m "refactor(frontend): migrate account pages"
```

## Task 6: 迁移设置、审计、登录并清除旧视觉系统

**Files:**

- Modify: `frontend/src/pages/SettingsPage.vue`
- Modify: `frontend/src/pages/AuditPage.vue`
- Modify: `frontend/src/pages/LoginPage.vue`
- Modify: `frontend/src/components/OperationStatus.vue`
- Modify: `frontend/src/components/PanelHeader.vue`
- Modify: `frontend/src/styles/main.css`
- Delete: `frontend/src/styles/tokens.css`
- Delete: `frontend/src/styles/admin-shell.css`
- Delete: `frontend/src/styles/console.css`
- Delete: `frontend/src/styles/data-controls.css`
- Delete: `frontend/src/styles/login.css`
- Delete: `frontend/src/styles/proxy-keys.css`
- Modify: `frontend/tests/auth.spec.ts`
- Modify: `frontend/tests/operations-console.spec.ts`

**Consumes:** Tasks 1–5. All route imports must already use the new shell/primitives before legacy CSS deletion.

**Produces:** a completely source-derived UI style, no active old blue-gray stylesheet, and unchanged authentication/settings/audit behavior.

- [ ] **Step 1: Add final page and cleanup failure tests.**

Add a test that Login has labelled Admin Key input, remote HTTP warning and no storage persistence under the new theme classes. Add a source scan test or test script assertion that `main.css` does not import any legacy CSS filename listed above.

- [ ] **Step 2: Run focused cleanup tests for the red state.**

Run: `npm run test -- tests/auth.spec.ts tests/operations-console.spec.ts`

Expected: the stylesheet-import assertion fails while legacy CSS remains active.

- [ ] **Step 3: Convert settings, audit and login.**

- `SettingsPage.vue`: retain SQLite runtime setting grouping, validation, version conflict/error messages, apply status and save/rollback controls. Convert controls to source-derived cards/forms; setting labels stay visible and Chinese.
- `AuditPage.vue`: retain filtering, backup and dry-run restore safety flow. Convert logs to `DataTableFrame` and operation states to `StatusBadge`.
- `LoginPage.vue`: create a restrained independent source-derived login surface. Preserve exact labels required by existing test, explicit unsafe remote HTTP warning and the guarantee that Admin Key is cleared/not persisted after session creation.
- Restyle `OperationStatus` and `PanelHeader` to avoid old CSS selectors.

Before deletion, run `rg -n "tokens.css|admin-shell.css|console.css|data-controls.css|login.css|proxy-keys.css" frontend/src frontend/index.html`; the result must contain no active import/reference. Delete only the listed obsolete stylesheet files, not any unrelated frontend asset.

- [ ] **Step 4: Run final frontend functional verification.**

Run:

```bash
npm run test
npm run typecheck
npm run lint
npm run build
npm run test:e2e
```

Expected: all current frontend tests, type check, lint, production build and Playwright flows pass without legacy CSS assets.

- [ ] **Step 5: Commit the final migration/cleanup phase.**

```bash
git add frontend/src/pages/SettingsPage.vue frontend/src/pages/AuditPage.vue frontend/src/pages/LoginPage.vue frontend/src/components/OperationStatus.vue frontend/src/components/PanelHeader.vue frontend/src/styles/main.css frontend/tests/auth.spec.ts frontend/tests/operations-console.spec.ts
git rm frontend/src/styles/tokens.css frontend/src/styles/admin-shell.css frontend/src/styles/console.css frontend/src/styles/data-controls.css frontend/src/styles/login.css frontend/src/styles/proxy-keys.css
git commit -m "refactor(frontend): complete sub2api ui migration"
```

## Task 7: 视觉验收、真实本地 Control Plane 回归与文档收尾

**Files:**

- Create: `frontend/e2e/sub2api-visual.spec.ts`
- Modify: `frontend/e2e/admin-console.spec.ts`
- Modify: `docs/design/macmini-multi-account-proxy-checkin.md`
- Modify: `.superpowers/sdd/progress.md`
- Modify: `frontend/THIRD_PARTY_NOTICES.md` only if the final copied-file mapping differs from the plan.

**Consumes:** all earlier tasks and the existing local Control Plane test harness.

**Produces:** reproducible visual/interaction evidence and an accurate project record that distinguishes UI completion from unverified upstream protocol work.

- [ ] **Step 1: Add browser visual/interaction checks.**

Create `sub2api-visual.spec.ts` covering this matrix:

```ts
const viewports = [
  { name: "desktop", width: 1440, height: 960 },
  { name: "laptop", width: 1024, height: 900 },
  { name: "tablet", width: 768, height: 1024 },
  { name: "mobile", width: 375, height: 812 },
] as const;
```

For each route class, assert no page-level horizontal overflow. Explicitly test desktop sidebar collapse, mobile drawer open/close by keyboard and backdrop, dialog Escape close/focus return, and light/dark root class switching. Save reviewable Playwright screenshots for Overview, Accounts, Settings and Login in both themes; screenshot paths are test artifacts, not tracked UI assets.

- [ ] **Step 2: Run visual test red/green cycle.**

Run: `npm run test:e2e -- sub2api-visual.spec.ts`

Expected before final test implementation: missing spec; expected after implementation: all viewport/theme/interaction checks pass.

- [ ] **Step 3: Run the full integration verification set.**

Run:

```bash
cd frontend && npm run test && npm run typecheck && npm run lint && npm run build && npm run test:e2e
cd .. && env PYTHONPATH=src pytest -q && ruff check src tests && python tools/check_code_limits.py && git diff --check
```

Also start the local Control Plane with only non-secret local test configuration, authenticate through the browser test harness, and verify every existing route loads. Do not perform third-party login, OAuth completion, Provider generation, credential refresh or check-in.

- [ ] **Step 4: Update project records exactly.**

In the main design and progress ledger, record the exact frontend commit range, test commands and that Sub2API UI migration is complete only if the previous command succeeds. Keep `AUTH-01`, `CB-CHECKIN-01` and `QD-CHECKIN-01` explicitly unverified unless a separate user-authorized real-account acceptance run supplies evidence.

- [ ] **Step 5: Commit validation/docs as the final UI phase.**

```bash
git add frontend/e2e/sub2api-visual.spec.ts frontend/e2e/admin-console.spec.ts docs/design/macmini-multi-account-proxy-checkin.md .superpowers/sdd/progress.md frontend/THIRD_PARTY_NOTICES.md
git commit -m "test(frontend): verify sub2api console"
```

## Plan self-review

- **Spec coverage:** Tasks 1–3 implement direct-source foundation, licensing, responsive shell and primitives; Tasks 4–6 cover every existing route and remove old styling; Task 7 covers visual, frontend, backend and documentation proof.
- **No backend scope creep:** all task interfaces retain `apiRequest`, existing session and current mutations; no task changes a Python API, persistence schema or real upstream call.
- **Source/identity consistency:** every copied source has one target, adaptation boundary and license requirement; no task adds upstream branding or i18n.
- **Safety coverage:** sensitive credential/Key rendering, typed confirmations, cookie persistence, remote HTTP warning and check-in side effects remain specific regression targets.
