# Full Control Plane / Proxy Worker Refactor Implementation Plan

> **Execution contract:** follow this plan task by task. Keep the existing branch and user changes. Do not commit or push unless explicitly requested. Every task must leave the repository in a runnable state and update `.superpowers/sdd/progress.md`.

## Goal

Transform `/Users/dmego/vibeCoding/2api` from a single-process proxy with partial account/check-in work into a persistent Control Plane plus independently supervised Proxy Worker. Deliver a rich Vue operations console that can manage service lifecycle, accounts, credentials, models, request/token usage, points/quota snapshots, check-in, runtime settings, audit, and backup without exposing secrets.

## Non-negotiable constraints

- Keep OpenAI/Anthropic compatibility and existing provider protocol behavior unless a focused regression test proves the old behavior is unsafe.
- Control Plane is long-lived; Worker is loopback-only and may be started, drained, stopped, restarted, or reloaded by `ServiceSupervisor`.
- `QB2API_PROXY_API_KEY`, `QB2API_ADMIN_KEY`, and the Worker internal token are different credentials. Legacy `QB2API_API_KEY` is Proxy-only during migration.
- Stable account IDs are persisted random IDs; credential rotation increments `credential_version` and never changes the account ID.
- Provider pools are stable `DynamicProviderPool(0..N)` instances keyed by `(provider, account_id)`. Streaming failover is allowed only before the first downstream chunk.
- All SQLite access goes through an asynchronous repository boundary with WAL, foreign keys, busy timeout, short transactions, and versioned credential cache invalidation.
- No prompt, completion, Authorization, Cookie, refresh token, master key, or raw upstream response may reach UI, logs, audit records, request events, or exports.
- Settings mutations are schema-validated, versioned, auditable, and report actual apply state; no fake save buttons.
- Frontend is Vue 3 + TypeScript + Vite + Pinia + Vue Router + TanStack Vue Query + ECharts + Lucide, served as same-origin static files by Control Plane. No Node process is required in production.
- Follow repository limits: functions <=50 lines, files <=300 lines, nesting <=3, no unrelated refactors.

## Verification policy

Use `rtk` command prefix for shell commands. Before claiming completion, run the focused tests for the task, `pytest -q`, `ruff check src tests`, frontend typecheck/lint/unit/build, Playwright desktop/mobile smoke, `python -m compileall -q src/qb2api`, and `git diff --check`. Report any unavailable external Spike separately.

## Task 1: Establish baseline and frontend build contract

**Files:** `package.json`, `package-lock.json` or `pnpm-lock.yaml`, `frontend/`, `src/qb2api/web/dist/`, `pyproject.toml`, `tests/integration/test_admin_ui.py`, `Makefile` or `scripts/build_frontend.sh`.

1. Capture the current branch/status and the existing green test baseline in the progress ledger.
2. Create a Vite Vue TypeScript app with strict typechecking and a deterministic build output copied to `src/qb2api/web/dist`.
3. Add Vue Query, Pinia, Vue Router, ECharts, Lucide and the existing repository-compatible test/lint tools. Do not add a second runtime server.
4. Add a minimal route shell and test fixture that proves `/admin` assets are same-origin, disabled UI returns 404, and source code does not use `innerHTML` for data.
5. Run frontend install/typecheck/unit/build and the existing Python suite. Record artifact paths and commands.

## Task 2: Split Control Plane and Worker entrypoints

**Files:** `src/qb2api/app.py`, `src/qb2api/control/app.py`, `src/qb2api/worker/app.py`, `src/qb2api/worker/proxy_router.py`, `src/qb2api/worker/runtime.py`, `src/qb2api/config.py`, `tests/integration/test_runtime_boot.py`, existing proxy tests.

1. Add `create_control_app(settings_factory=...)` and `create_worker_app(settings_factory=...)`; keep `create_app` as a compatibility alias while callers migrate.
2. Move mutable provider/runtime assembly behind Worker state and make Control Plane composition-only. Preserve current test injection points through explicit app state, not module globals.
3. Make Worker bind to `QB2API_WORKER_HOST/PORT`, expose loopback `/health/live`, `/internal/health/ready`, and the existing OpenAI/Anthropic routes.
4. Make Control Plane bind to `QB2API_CONTROL_HOST/PORT`, serve the SPA and admin API, and remain healthy when Worker is absent.
5. Add a protocol/versioned runtime snapshot DTO shared by both processes; reject incompatible Worker handshakes.
6. Run the existing OpenAI, Anthropic, stream, model, and app factory tests plus a fresh-data two-process startup smoke.

## Task 3: Implement ServiceSupervisor and safe lifecycle operations

**Files:** `src/qb2api/control/supervisor.py`, `src/qb2api/control/operations.py`, `src/qb2api/control/service_router.py`, `src/qb2api/control/runtime_state.py`, `src/qb2api/worker/internal_router.py`, `tests/control/test_supervisor.py`, `tests/integration/test_service_lifecycle.py`.

1. Implement states `STOPPED`, `STARTING`, `HEALTHY`, `DEGRADED`, `STOPPING`, `FAILED` and idempotent operation records.
2. Start Worker with owner instance ID, process group, start time, internal token version, bounded environment, and explicit command path.
3. Require PID + process start time + process group + owner + successful internal handshake before treating a process as owned.
4. Implement start/stop/restart/reload with drain, grace deadline, SIGTERM, and only-after-identity-confirmed SIGKILL. Never kill by port.
5. Reconcile orphan state on Control Plane startup without terminating an unverified process; expose diagnostic state to UI.
6. Add authenticated `/api/admin/service/*` operations and loopback-only `/api/control/*` handshake/health/telemetry routes. Return operation IDs for long actions.
7. Test duplicate starts, wrong PID/start time, orphan process, draining stream, timeout, crash, reload failure, and Control Plane availability while Worker is stopped.

## Task 4: Complete asynchronous persistence and runtime settings

**Files:** `src/qb2api/accounts/schema.py`, `src/qb2api/accounts/repository.py`, new repository modules, `src/qb2api/control/settings.py`, `src/qb2api/admin/backup.py`, `tests/accounts/`, `tests/control/test_settings.py`.

1. Add migrations for `runtime_settings`, `service_runtime`, `proxy_api_keys`, `model_catalog`, `request_events`, `usage_rollups`, `account_metric_snapshots`, `audit_events`, and `backup_runs` while preserving existing account/purpose/check-in tables.
2. Enforce WAL, `foreign_keys=ON`, `busy_timeout`, and async repository access. No handler, provider, scheduler, or vault receives a raw SQLite connection.
3. Implement optimistic `value_version`, typed setting schema, `source`, `apply_mode`, pending/error state, and atomic scheduler/Worker application hooks.
4. Implement secret-safe proxy key hashes, one-time reveal/rotation/revocation, backup dry-run, checksum, and restore validation without overwriting the live database.
5. Add concurrency, migration, credential CAS, backup integrity, settings conflict, and event-loop blocking tests.

## Task 5: Harden account registry, credentials, and dynamic pools

**Files:** `src/qb2api/accounts/models.py`, `registry.py`, `resolver.py`, `vault.py`, `src/qb2api/providers/lb.py`, provider adapters, `tests/test_dynamic_pool.py`, account/import/check-in tests.

1. Complete persistent random account IDs, purpose state, verification state, identity matching, environment shadowing, promotion, and deletion guards for env accounts.
2. Ensure resolver cache keys include provider/account/purpose/version; refresh uses single-flight and conditional credential CAS.
3. Make CodeBuddy and Qoder providers account-backed and keep independent sessions per account/PAT.
4. Ensure one stable pool object handles 0, 1, and N slots. Use stable slot keys, lease/drain retirement, snapshot replacement, and dynamic model visibility.
5. Enforce pre-first-chunk-only failover. After any downstream chunk, terminate the stream and record the original account failure.
6. Add tests for first/last account changes, duplicate env/database credentials, refresh without pool rebuild, partial streams, cancellation, retirement, and cross-purpose failure isolation.

## Task 6: Implement full management API domains

**Files:** `src/qb2api/admin/router.py`, `account_routes.py`, `session_routes.py`, `import_routes.py`, `src/qb2api/control/service_router.py`, new `model_routes.py`, `usage_routes.py`, `settings_routes.py`, `audit_routes.py`, `backup.py`, `tests/integration/`.

1. Keep exact bootstrap/public shell rules and Admin session + CSRF behavior. Proxy Key must be 403 for admin routes; Admin session must not authenticate `/v1/*` or legacy `/api/config`.
2. Implement account list/detail/edit/delete/enable/probe/refresh/promotion and credential metadata endpoints without secrets.
3. Implement CodeBuddy OAuth/manual, Qoder chat/check-in import, WorkBuddy import, and atomic validation-before-commit semantics.
4. Implement service operation/status/events, model catalog/refresh/probe, usage summary/timeseries/events/export, metrics snapshots/refresh, runtime settings/schema/PATCH, audit and backup APIs.
5. Add pagination/filter validation, operation IDs, idempotency, error envelopes, audit writes, and no-secret response tests for every mutation.

## Task 7: Add telemetry, rollups, token/points monitoring, and check-in schedulers

**Files:** `src/qb2api/control/telemetry.py`, `src/qb2api/checkin/metrics.py`, `src/qb2api/checkin/usage.py`, scheduler modules, Worker request event hooks, `tests/checkin/`, `tests/usage/`, `tests/metrics/`.

1. Emit bounded, non-blocking request events from Worker, including stream commit state and final provider/account.
2. Implement minute/day/month rollups, retention, stale handling, and loss counters. Telemetry failures cannot fail proxy requests.
3. Implement MetricsScheduler for token status, points, quota, and check-in summary with per-account single-flight, rate limits, backoff, and purpose isolation.
4. Integrate CheckinScheduler with WorkBuddy `10001` classification, Qoder dual credentials, catch-up, jitter, batch lock, manual targets, and persistent run history.
5. Implement settings apply hooks for scheduler reschedule and Worker reload/restart with operation status.
6. Test no-fake-zero metrics, stale snapshots, event backpressure, rollup accuracy, retention, scheduler timezone/catch-up, retry rules, and purpose failure isolation.

## Task 8: Build the rich Vue operations console

**Files:** `frontend/src/**`, frontend tests, `src/qb2api/web/dist/**`.

1. Build authenticated `AdminShell` with sidebar navigation, service rail, session indicator, responsive desktop-first layout, design tokens, Lucide icons, accessible focus and status semantics.
2. Implement pages: Login, Overview, Service, Accounts, Account Detail, Add Account, Credentials, Models, Usage, Check-in, Settings, Audit, Backup.
3. Implement reusable DataTable, filters, pagination, detail drawers, charts, skeleton/empty/error states, confirmation dialogs, operation polling and precise Query invalidation.
4. Add service start/stop/restart/reload controls, account batch actions, model controls, settings apply status, token/points/quota snapshots, usage exports, check-in runs, and audit/backup workflows.
5. Ensure no raw secret rendering, no `innerHTML`, no fake success states, no decoration-only cards, no overlapping text, responsive narrow-screen degradation, and keyboard/contrast accessibility.
6. Run Vitest/component tests, TypeScript/lint/build, Playwright desktop/mobile smoke, screenshots and console-error checks.

## Task 9: Migration, deployment, and compatibility hardening

**Files:** `deploy/`, `tools/`, `docs/spike/spike-results.md`, `README.md`, `.env.example`, migration tests, compatibility routes.

1. Document old single-process to Control Plane/Worker migration, port mapping, env aliases, rollback and dry-run backup.
2. Add launchd template for Control Plane and optional systemd development template; Worker remains Supervisor-owned.
3. Validate Tailscale Serve/HTTPS, trusted forwarded headers, loopback cookie mode, data directory permissions, key separation, and restart recovery.
4. Keep legacy `/api/config` compatibility for one explicit cycle with deprecated fields and no secret echo; document removal separately.
5. Run fresh-data and migrated-data startup smoke tests, Worker crash/restart, Control Plane restart, backup restore dry-run, and model/proxy compatibility tests.

## Task 10: Final review and completion gate

1. Review the implementation against every checkbox in `docs/design/macmini-multi-account-proxy-checkin.md`, with findings first and exact file/line references.
2. Run all verification commands from this plan and record actual exit codes/output summaries in `.superpowers/sdd/progress.md`.
3. Inspect desktop and mobile screenshots for overlap, blank charts, inaccessible controls, clipped text, and console errors.
4. Run `git diff --check`, inspect tracked/untracked status, and leave unrelated user changes untouched.
5. Do not claim completion while an external protocol Spike, required test, frontend build, or deployment smoke remains unverified.
