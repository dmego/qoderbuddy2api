# 2api Completion Execution Plan

> This plan is the single execution source after baseline commit `703cdfc`. Superseded execution plans were removed after Task 9; Git history retains their implementation record.

## 1. Goal

Complete the approved `2api` refactor as a production-ready local operations platform:

- persistent Control Plane and independently supervised loopback Proxy Worker;
- multiple CodeBuddy/WorkBuddy/Qoder accounts;
- separate Proxy, Admin, and Worker credentials;
- account login/import, credential management, model routing, usage, token/points/quota monitoring, automatic check-in, runtime settings, audit, backup, deployment and migration;
- a feature-complete Vue management console;
- no unused legacy implementation, stale documentation, or unverified completion claims.

Approved design: `docs/design/macmini-multi-account-proxy-checkin.md`.

## 2. Baseline

Baseline commit:

```text
703cdfc feat: add control plane refactor baseline
```

Verified on 2026-07-24:

```text
rtk pytest -q                              221 passed
rtk ruff check src tests                   no issues
rtk python -m compileall -q src/qb2api     exit 0
rtk npm run test                           4 passed
rtk npm run typecheck                      exit 0
rtk npm run lint                           exit 0
rtk npm run build                          exit 0
rtk git diff --check                       no diagnostics
```

Known baseline debt:

- remote HTTP admin login is rejected even when `QB2API_ADMIN_COOKIE_SECURE=false`;
- Proxy API Key backend exists, but the UI and real create/revoke Worker reload path are incomplete;
- missing design APIs: account refresh/probe, model probe, metrics account detail, and service events;
- the operations console has the main pages, but several are partial workflows rather than full management surfaces;
- WorkBuddy/Qoder real protocol evidence is incomplete;
- deployment, migration, launchd, browser acceptance and restart recovery are incomplete;
- 8 files exceed 300 lines and the graph reports multiple functions over the agreed complexity/size limits;
- the legacy `src/qb2api/web/admin.*` implementation coexists with the Vue console;
- documentation still assumes remote management always uses HTTPS.

## 3. Execution model

### 3.1 Commit and verification policy

- The plan contains 9 large delivery tasks.
- Each task produces exactly one implementation commit.
- Internal substeps do not receive separate commits.
- Run one task-level verification bundle after the task is implemented; do not rerun the same suite after every small edit.
- Run the full Python/frontend suite once after each parallel wave is integrated.
- Progress is reported as completed task commits out of 9. Each task represents approximately 11.1% of the remaining implementation plan.
- Update `.superpowers/sdd/progress.md` after each integrated task with commit SHA, changed domains and actual verification results.

### 3.2 Parallel development

- Root works on the integration branch `codex/multi-account-proxy-checkin`.
- Each worker uses an isolated worktree and branch `codex/task-N-<slug>`.
- Up to three `gpt-5.6-sol` workers run concurrently with `xhigh` reasoning, explicitly approved by the user on 2026-07-24.
- Workers commit their complete task once and report the SHA plus test results.
- Root reviews each diff, cherry-picks it, resolves shared-file integration, and runs the wave gate.
- No two active workers modify the same file.
- Root owns shared integration files: `frontend/src/router.ts`, `frontend/src/layouts/AdminShell.vue`, package/lock files, repository composition, schema migration ordering, design/progress docs, and deployment entrypoints.

### 3.3 Standard project rules

- Preserve user data and existing unrelated changes.
- Do not commit `.env`, raw credentials, prompt/completion content, Authorization/Cookie values, or upstream raw responses.
- Worker remains loopback-only and never opens SQLite or receives the credential master key.
- Streaming cannot fail over after the first downstream chunk.
- New or modified code should stay below 300 lines per file and 50 lines per function; existing violations are removed in Task 8.
- Frontend remains a high-density, light infrastructure console with grouped navigation, responsive drawers and data-dense operational pages; it is not a simplified demo UI.
- Frontend dependencies use `--registry=https://registry.npmmirror.com`.

## 4. Parallel waves

| Wave | Root task | Worker A | Worker B | Worker C | Wave gate |
|---|---|---|---|---|---|
| 1 | Task 1 | Task 2 | Task 3 | Task 4 | Python full suite + frontend full gate + real two-process smoke |
| 2 | integration | Task 5 | Task 6 | Task 7 | Python full suite + frontend full gate + fresh/migrated startup smoke |
| 3 | Task 8 | — | — | — | code-limit check + full suite |
| 4 | Task 9 | — | — | — | final completion audit |

## 5. Delivery tasks

### Task 1: Security and Proxy access closure

**Owner:** root

**Purpose:** finish the complete administrator-login and Proxy API Key vertical slice.

**Scope:**

- allow trusted remote HTTP only when `QB2API_ADMIN_COOKIE_SECURE=false` is explicitly configured;
- keep `auto` secure-by-default and reject remote HTTP;
- show a prominent remote-HTTP warning on the login page;
- finish Proxy API Key create/list/rotate/revoke/expiry behavior;
- add a dedicated Proxy Key console page with one-time reveal and copy;
- never write a raw Proxy Key to SQLite, logs, browser storage or Worker environment;
- prove that a newly created key works in the running Worker and a revoked key immediately returns 401 after runtime snapshot reload;
- update design and environment documentation to support trusted Tailscale/LAN HTTP without claiming public-Internet safety.

**Primary files:**

```text
src/qb2api/admin/auth.py
src/qb2api/admin/session_routes.py
src/qb2api/admin/proxy_key_routes.py
src/qb2api/control/runtime_snapshot.py
src/qb2api/control/health.py
src/qb2api/worker/proxy_state.py
tests/test_admin_auth.py
tests/integration/test_auth_matrix.py
tests/integration/test_proxy_keys.py
tests/integration/test_two_process_runtime.py
frontend/src/pages/LoginPage.vue
frontend/src/pages/ProxyKeysPage.vue
frontend/tests/auth.spec.ts
frontend/tests/proxy-keys.spec.ts
frontend/src/router.ts
frontend/src/layouts/AdminShell.vue
docs/design/macmini-multi-account-proxy-checkin.md
.env.example
```

**Required API behavior:**

```text
GET  /api/admin/proxy-keys
POST /api/admin/proxy-keys
POST /api/admin/proxy-keys/{key_id}/rotate
POST /api/admin/proxy-keys/{key_id}/revoke
```

Create/rotate responses reveal the raw key once. List/revoke responses never include raw key or hash.

**Task verification:**

```bash
rtk pytest -q tests/test_admin_auth.py tests/integration/test_auth_matrix.py tests/integration/test_proxy_keys.py tests/integration/test_two_process_runtime.py
rtk npm run test -- auth.spec.ts proxy-keys.spec.ts
rtk npm run typecheck
rtk npm run lint
rtk npm run build
rtk git diff --check
```

**Commit:**

```text
feat(security): close admin and proxy access
```

### Task 2: Complete management backend contracts

**Owner:** Worker A

**Purpose:** implement the missing management APIs and make operations observable and consistently validated.

**Scope:**

- persist secret-safe service lifecycle events;
- implement `GET /api/admin/service/events` with bounded cursor pagination;
- implement account refresh and probe operations;
- implement model probe with a fixed internal minimal request, timeout and content discard;
- implement metrics detail per provider/account;
- make metrics refresh return a trackable operation result;
- add consistent pagination/range/filter validation for accounts, models, usage, audit, check-in runs and service events;
- ensure all mutations write audit events and all responses use stable error codes;
- keep arbitrary upstream URLs, headers and credentials out of request bodies.

**Primary files:**

```text
src/qb2api/control/service_router.py
src/qb2api/control/supervisor.py
src/qb2api/admin/account_routes.py
src/qb2api/admin/catalog_routes.py
src/qb2api/admin/observability_routes.py
src/qb2api/admin/validation.py
src/qb2api/accounts/repo_service_events.py
src/qb2api/accounts/repo_catalog.py
src/qb2api/accounts/repo_telemetry.py
tests/control/test_service_events.py
tests/integration/test_management_contracts.py
tests/integration/test_model_probe.py
```

**Required new endpoints:**

```text
GET  /api/admin/service/events
POST /api/admin/accounts/{provider}/{account_id}/refresh
POST /api/admin/accounts/{provider}/{account_id}/probe
POST /api/admin/models/{provider}/{model_id}/probe
GET  /api/admin/metrics/accounts/{provider}/{account_id}
```

**Task verification:**

```bash
rtk pytest -q tests/control/test_service_events.py tests/integration/test_management_contracts.py tests/integration/test_model_probe.py tests/integration/test_control_api_domains.py tests/integration/test_account_admin.py
rtk ruff check src/qb2api/control src/qb2api/admin src/qb2api/accounts tests/control tests/integration
rtk git diff --check
```

**Commit:**

```text
feat(admin): complete management contracts
```

### Task 3: Provider and check-in protocol hardening

**Owner:** Worker B

**Purpose:** make CodeBuddy/WorkBuddy/Qoder provider and check-in behavior production-ready and evidence-backed.

**Scope:**

- split the oversized Qoder provider and Qoder check-in client by responsibility;
- preserve per-account Qoder sessions, model encoding, headers and SSE parsing;
- preserve pre-first-chunk-only failover and cancellation cleanup;
- harden WorkBuddy bearer/cookie/bearer+cookie account isolation;
- confirm daily-checkin POST and `HTTP 400 + code=10001` classification;
- keep `checkin-status` method disabled unless explicitly configured;
- harden Qoder status/claim/refresh classification and credential rotation CAS;
- complete Windows Qoder exporter/import documentation and validation;
- update `docs/spike/spike-results.md` with only evidence actually observed from local reference projects or authorized runtime probes;
- add multi-account batch tests proving one account failure does not stop subsequent accounts or affect chat purpose state.

**Primary files:**

```text
src/qb2api/providers/qoder.py
src/qb2api/providers/qoder_auth.py
src/qb2api/providers/qoder_payload.py
src/qb2api/checkin/qoder.py
src/qb2api/checkin/qoder_status.py
src/qb2api/checkin/codebuddy.py
src/qb2api/checkin/executors.py
src/qb2api/checkin/service.py
tests/test_checkin_clients.py
tests/test_dynamic_pool.py
tests/integration/test_checkin_service.py
tools/qoder-checkin-exporter/
docs/spike/spike-results.md
```

**Task verification:**

```bash
rtk pytest -q tests/test_checkin_clients.py tests/test_dynamic_pool.py tests/integration/test_stream_contract.py tests/integration/test_checkin_service.py tests/accounts/test_credential_cas.py
rtk ruff check src/qb2api/providers src/qb2api/checkin tests/test_checkin_clients.py tests/test_dynamic_pool.py tests/integration/test_checkin_service.py
rtk python -m compileall -q src/qb2api
rtk git diff --check
```

**Commit:**

```text
refactor(checkin): harden provider protocols
```

### Task 4: Complete the operations console

**Owner:** Worker C

**Purpose:** turn the existing Vue pages into a complete operations console rather than a collection of partial tables.

**Scope:**

- complete Service page with persisted event feed, operation history, draining/in-flight state and error details;
- complete Models page with filters, enable/disable, refresh, probe, model detail and usage/latency/error summaries;
- complete Accounts page with provider/source/status/purpose search and filters, batch selection, metrics status and action feedback;
- complete Usage page pagination, summary/trend/event coordination, safe detail and export status;
- complete Credentials, Check-in, Settings and Audit/Backup pages with real mutation feedback and confirmations;
- add reusable confirmation dialog, notification region, paginated table state and operation polling components;
- provide loading, empty, stale, unavailable and error states throughout;
- keep page-specific styles scoped or in frontend-owned files so this task does not modify root-owned router/layout files;
- add Vitest coverage for service, models, accounts, settings and backup workflows.

**Primary files:**

```text
frontend/src/pages/ServicePage.vue
frontend/src/pages/ModelsPage.vue
frontend/src/pages/AccountsPage.vue
frontend/src/pages/UsagePage.vue
frontend/src/pages/CredentialsPage.vue
frontend/src/pages/CheckinPage.vue
frontend/src/pages/SettingsPage.vue
frontend/src/pages/AuditPage.vue
frontend/src/components/ConfirmDialog.vue
frontend/src/components/OperationStatus.vue
frontend/src/components/PaginatedTable.vue
frontend/src/components/NotificationRegion.vue
frontend/src/styles/main.css
frontend/tests/
```

**Task verification:**

```bash
rtk npm run test
rtk npm run typecheck
rtk npm run lint
rtk npm run build
rtk git diff --check
```

**Commit:**

```text
feat(console): complete operations workflows
```

### Task 5: Accounts, credentials and OAuth end-to-end

**Owner:** Worker A after Wave 1 integration

**Purpose:** close every account onboarding, detail, credential and reauthorization workflow.

**Scope:**

- add dedicated Add Account and Account Detail routes;
- complete CodeBuddy OAuth start/poll/expiry/retry/resume behavior;
- complete CodeBuddy manual bearer import;
- complete Qoder chat PAT import and Qoder check-in access+refresh import;
- implement WorkBuddy check-in credential import for verified bearer/cookie modes;
- validate before commit and preserve the old credential on failure;
- normalize credential rotate/revoke API contracts while keeping explicit compatibility aliases for one cycle;
- show purpose-specific status, verification, expiry, metrics, request events, check-in history and credential metadata on account detail;
- support enable/disable, label edit, refresh, probe, promote, reauthorize and delete with confirmation;
- never display fingerprint, raw credential or upstream response.

**Primary files:**

```text
src/qb2api/admin/import_routes.py
src/qb2api/admin/account_routes.py
src/qb2api/admin/security_routes.py
src/qb2api/accounts/imports.py
src/qb2api/auth/codebuddy_oauth.py
src/qb2api/auth/flows.py
frontend/src/pages/AddAccountPage.vue
frontend/src/pages/AccountDetailPage.vue
frontend/src/pages/CredentialsPage.vue
frontend/src/components/AccountImportPanel.vue
frontend/src/router.ts
tests/integration/test_account_admin.py
tests/integration/test_import_atomicity.py
tests/test_codebuddy_oauth.py
frontend/tests/accounts.spec.ts
```

**Task verification:**

```bash
rtk pytest -q tests/integration/test_account_admin.py tests/integration/test_import_atomicity.py tests/test_codebuddy_oauth.py tests/test_registry_resolver.py tests/accounts/test_credential_cas.py
rtk npm run test -- accounts.spec.ts
rtk npm run typecheck
rtk npm run lint
rtk npm run build
rtk ruff check src/qb2api/admin src/qb2api/accounts src/qb2api/auth tests
rtk git diff --check
```

**Commit:**

```text
feat(accounts): close onboarding workflows
```

### Task 6: Scheduler, metrics, settings, audit and backup closure

**Owner:** Worker B after Wave 1 integration

**Purpose:** finish the background operations and their complete management surfaces.

**Scope:**

- make CheckinScheduler and MetricsScheduler settings apply atomically;
- expose next run, catch-up decision, active batch, backoff and last error;
- return operation IDs for manual check-in and metric refresh;
- preserve unknown/stale/unavailable metrics instead of showing zero;
- complete runtime setting schema, optimistic version conflict and apply status;
- implement scheduler reschedule, Worker reload/restart and control-restart-required results;
- complete backup creation, checksum, dry-run restore validation and audit records;
- expose filtered/paginated audit and backup history;
- align Check-in, Settings and Audit/Backup pages with the completed backend operations;
- add cancellation, restart recovery and apply-failure integration tests.

**Primary files:**

```text
src/qb2api/checkin/scheduler.py
src/qb2api/checkin/metrics.py
src/qb2api/checkin/service.py
src/qb2api/control/settings.py
src/qb2api/control/app.py
src/qb2api/admin/settings_routes.py
src/qb2api/admin/checkin_routes.py
src/qb2api/admin/observability_routes.py
src/qb2api/admin/backup.py
src/qb2api/admin/security_routes.py
frontend/src/pages/CheckinPage.vue
frontend/src/pages/SettingsPage.vue
frontend/src/pages/AuditPage.vue
tests/integration/test_scheduler.py
tests/metrics/test_metrics_scheduler.py
tests/control/test_settings.py
tests/control/test_backup.py
```

**Task verification:**

```bash
rtk pytest -q tests/integration/test_scheduler.py tests/integration/test_checkin_service.py tests/metrics/test_metrics_scheduler.py tests/control/test_settings.py tests/control/test_backup.py tests/integration/test_control_api_domains.py
rtk npm run test -- checkin.spec.ts
rtk npm run typecheck
rtk npm run lint
rtk npm run build
rtk ruff check src/qb2api/checkin src/qb2api/control src/qb2api/admin tests
rtk git diff --check
```

**Commit:**

```text
feat(ops): close schedulers and recovery
```

### Task 7: Deployment, migration and operational documentation

**Owner:** Worker C after Wave 1 integration

**Purpose:** make the refactor installable and recoverable on the Mac Mini and development systems.

**Scope:**

- update English and Chinese README files for Control Plane/Worker topology;
- document trusted remote HTTP, HTTPS recommendation, Tailscale/LAN boundary and forwarded-header trust;
- complete `.env.example` without secrets and with all new variables;
- provide launchd template for the Control Plane; Worker remains Supervisor-owned;
- provide optional systemd development template;
- document old single-process migration, port mapping, key separation, data paths, backup, rollback and session invalidation;
- add fresh-database and migrated-database startup smoke scripts/tests;
- verify Control restart, Worker crash/restart and backup restore dry-run behavior;
- document Qoder Windows exporter and WorkBuddy credential import runbooks;
- add a deployment checklist that never instructs users to put keys in URLs or browser storage.

**Primary files:**

```text
README.md
README.zh.md
.env.example
deploy/launchd/cn.qb2api.control.plist
deploy/systemd/qb2api-control.service
docs/deployment/macmini.md
docs/migration/single-process-to-control-worker.md
scripts/smoke_fresh_install.sh
scripts/smoke_migrated_install.sh
tests/integration/test_migration_smoke.py
tools/qoder-checkin-exporter/README.md
```

**Task verification:**

```bash
rtk pytest -q tests/integration/test_migration_smoke.py tests/accounts/test_schema_migrations.py tests/control/test_backup.py tests/integration/test_two_process_runtime.py
rtk python -m compileall -q src/qb2api
rtk git diff --check
```

**Commit:**

```text
docs(deploy): add migration and runbooks
```

### Task 8: Code-quality and legacy cleanup

**Owner:** root after Wave 2 integration

**Purpose:** remove accumulated refactor debt without changing behavior.

**Scope:**

- split every remaining production file over 300 lines;
- split every remaining production function over 50 lines or complexity 10;
- reduce long constructor parameter lists with explicit dependency/config objects where useful;
- split account repository/schema/registry responsibilities cleanly;
- split Qoder and check-in tests into focused files where they exceed 300 lines;
- remove unused legacy `src/qb2api/web/admin.html`, `admin.js` and `admin.css` after proving no runtime reference remains;
- disable production source maps unless an explicit debug build requests them;
- remove stale build assets and rebuild `src/qb2api/web/dist` deterministically;
- remove dead imports, compatibility branches no longer required by the approved migration window, and unused helper code;
- add a lightweight code-limit checker so new violations fail verification.

**Primary files:**

```text
src/qb2api/accounts/
src/qb2api/checkin/
src/qb2api/providers/
src/qb2api/config.py
src/qb2api/anthropic.py
src/qb2api/worker/
src/qb2api/web/admin.*
frontend/vite.config.ts
tests/
tools/check_code_limits.py
```

**Task verification:**

```bash
rtk python tools/check_code_limits.py
rtk pytest -q
rtk ruff check src tests
rtk python -m compileall -q src/qb2api
rtk npm run test
rtk npm run typecheck
rtk npm run lint
rtk npm run build
rtk git diff --check
```

**Commit:**

```text
refactor(core): remove legacy and size debt
```

### Task 9: Browser acceptance and final completion audit

**Owner:** root

**Purpose:** prove the entire approved design works as an integrated product and update every document to the final implementation.

**Scope:**

- install/configure Playwright using the npm mirror;
- run a real Control Plane and supervised Worker for browser tests;
- cover login, remote HTTP warning, Proxy Key, service lifecycle, account import/detail, credential rotation, model probe, usage filters/export, check-in, settings, audit and backup;
- capture desktop and narrow/mobile screenshots;
- compare the current Vue console against the locally cloned Sub2API information architecture without copying third-party code, brand, assets or color system;
- check console errors, clipped text, overflow, blank charts, inaccessible icon buttons and keyboard focus;
- run fresh-data and migrated-data smoke;
- run the full Python/frontend/static/build gates;
- inspect the design acceptance checklist item by item and record evidence or remaining external protocol blockers;
- update the design, README files and progress ledger to match actual routes, configuration and deployment behavior;
- remove superseded plan documents only after confirming this plan and Git history preserve the needed execution record;
- leave the working tree clean and provide the final commit/verification summary.

**Primary files:**

```text
frontend/playwright.config.ts
frontend/e2e/
frontend/package.json
frontend/package-lock.json
docs/design/macmini-multi-account-proxy-checkin.md
README.md
README.zh.md
.superpowers/sdd/progress.md
docs/superpowers/plans/
```

**Final verification:**

```bash
rtk pytest -q
rtk ruff check src tests
rtk python -m compileall -q src/qb2api
rtk python tools/check_code_limits.py
rtk npm run test
rtk npm run typecheck
rtk npm run lint
rtk npm run build
rtk npm run test:e2e
rtk git diff --check
rtk git status --short --branch
```

**Commit:**

```text
test(e2e): complete product acceptance
```

## 6. Progress ledger format

After each task is integrated, append:

```text
Task N: complete
Commit: <sha>
Verification: <commands and pass counts>
Remaining: <9-N>/9 tasks
```

Do not report percentage from lines changed or partial substeps. Only integrated task commits count.

## 7. Completion rule

The project is complete only when all 9 task commits are integrated, Task 9 verification is green, the final design checklist has evidence for every locally verifiable item, external protocol gaps are explicitly recorded, documentation matches the implementation, and the working tree is clean.
