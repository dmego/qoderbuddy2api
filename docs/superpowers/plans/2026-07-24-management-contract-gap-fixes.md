# Management Contract Gap Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining Task 2 management API correctness, lifecycle, audit, pagination, and migration gaps without changing shared dependencies or root configuration.

**Architecture:** Keep validation and pagination rules in the admin/repository layers, keep persistent operation recovery in the account repository, and make runtime shutdown own all background refresh wrappers before closing SQLite. Store schema additions in a focused migration module so schema composition and migration order remain explicit while production files stay below 300 lines.

**Tech Stack:** Python 3.11+, FastAPI, asyncio, aiosqlite/SQLite, pytest, Ruff.

## Global Constraints

- Do not modify root dependency, CI, environment template, or shared contract files outside this task.
- Use stable external error codes; never expose raw exception text or exception class names.
- Account/model probes must not accept custom upstream URL, header, or credential material.
- Service events support controlled event type/status filters and persist operation in-flight snapshots.
- Model, usage, check-in, and audit list filters must implement the documented query semantics rather than silently ignoring frontend parameters.
- Production files must be at most 300 lines; functions must be at most 50 lines.
- Use TDD for every behavior change and preserve the existing management API response fields unless this plan explicitly adds pagination fields.
- Final commit message: `fix(admin): close management contract gaps`.

---

### Task 1: Canonical time ranges and stable check-in pagination

**Files:**
- Modify: `src/qb2api/admin/validation.py`
- Modify: `src/qb2api/admin/checkin_routes.py`
- Modify: `src/qb2api/accounts/repo_checkin.py`
- Modify: `tests/integration/test_management_contracts.py`

**Interfaces:**
- `time_range(after, before)` returns canonical UTC ISO strings.
- `list_checkin_runs_page(limit, cursor, status, trigger)` returns `(runs, next_cursor)` using a stable `(started_at, run_id)` keyset.

- [x] Add tests proving `Z`, `+00:00`, and non-UTC offsets select the same usage/audit rows.
- [x] Verify from the pre-change implementation that raw time strings are passed through unchanged.
- [x] Return `after.astimezone(UTC).isoformat()` and `before.astimezone(UTC).isoformat()` from `time_range`.
- [x] Add check-in run tests for first/next page, inserts between pages, invalid limit/cursor, and controlled status/trigger filters.
- [x] Verify from the pre-change endpoint that cursor and controlled filters are unsupported.
- [x] Implement a bounded opaque cursor encoding the last `(started_at, run_id)` pair; reject malformed cursors with `invalid_cursor`.
- [x] Query `limit + 1` rows with `WHERE started_at < ? OR (started_at = ? AND run_id < ?)` and return `next_cursor`.
- [x] Run the Task 1 focused tests to green.

### Task 2: Durable metrics refresh terminal states

**Files:**
- Modify: `src/qb2api/accounts/repo_service_events.py`
- Modify: `src/qb2api/admin/observability_routes.py`
- Modify: `src/qb2api/runtime.py`
- Modify: `tests/integration/test_management_contracts.py`
- Modify: `tests/integration/test_control_api_domains.py`

**Interfaces:**
- `recover_metric_refresh_operations()` marks persisted `running` rows `cancelled` with `refresh_interrupted`.
- `cancel_metrics_refresh_tasks(app)` cancels and awaits all wrapper tasks before repository close.
- Runtime startup performs recovery after migration; runtime shutdown cancels wrappers before scheduler/repository close.

- [x] Add tests for fixed `metrics_refresh_failed`, task cancellation, startup recovery, and shutdown awaiting wrapper tasks.
- [x] Verify the pre-change detached-task and exception-name behavior from runtime/repository code.
- [x] Persist fixed public error codes while logging internal exception details.
- [x] Add repository recovery for orphaned `running` operations and audit the recovery result.
- [x] Move task ownership to runtime state and cancel/await wrapper tasks before repository shutdown.
- [x] Run the focused metrics tests to green.

### Task 3: Reliable mutation audit closure

**Files:**
- Modify: `src/qb2api/admin/account_routes.py`
- Modify: `src/qb2api/admin/catalog_routes.py`
- Modify: `src/qb2api/admin/checkin_routes.py`
- Modify: `src/qb2api/control/service_router.py`
- Modify: `tests/integration/test_management_contracts.py`
- Modify: `tests/integration/test_account_admin.py`
- Modify: `tests/control/test_service_events.py`

**Interfaces:**
- Database mutations and their success audit records commit in the same repository transaction where practical.
- Derived refresh failures use a separate stable failed audit record.
- Manual check-in always audits succeeded, failed, or cancelled terminal outcomes.

- [x] Add tests where account pool refresh fails after delete/update and assert the committed mutation has a corresponding audit plus a stable refresh-failure audit.
- [x] Add manual check-in success, conflict/failure, and cancellation audit tests.
- [x] Add service/model failure-path audit coverage where the mutation can already have taken effect.
- [x] Verify the pre-change audit ordering leaves committed mutations unaudited on refresh failure.
- [x] Introduce small audit helpers that work inside the current repository transaction.
- [x] Record the primary mutation audit before leaving its transaction; wrap external/long operations with terminal-result auditing.
- [x] Record derived refresh failures separately without duplicating successful mutation records.
- [x] Run the audit-focused tests to green.

### Task 4: Complete management query contracts

**Files:**
- Modify: `src/qb2api/accounts/repo_service_events.py`
- Modify: `src/qb2api/accounts/repo_telemetry.py`
- Modify: `src/qb2api/admin/catalog_routes.py`
- Modify: `src/qb2api/admin/observability_routes.py`
- Modify: `src/qb2api/control/service_router.py`
- Modify: `src/qb2api/control/supervisor.py`
- Modify: `tests/control/test_service_events.py`
- Modify: `tests/integration/test_management_contracts.py`

**Interfaces:**
- `/service/events` accepts controlled `event_type` and `status`/`result` filters and returns operation `in_flight` snapshots.
- `/models` accepts a bounded `query` name search over model ID/display name.
- Usage endpoints accept controlled `status`; summary returns `latency_avg_ms` and `latency_p95_ms`, both null with no latency samples.
- `/audit` supports explicit `action_prefix` and controlled `category` semantics in addition to exact `action`.

- [x] Add failing API tests for every query/filter contract and secret-safe service operation events.
- [x] Add repository-level query support with parameterized predicates and deterministic ordering.
- [x] Persist operation in-flight snapshots without raw error text and expose only stable error codes.
- [x] Compute latency average and nearest-rank p95 from the exact filtered request window.
- [x] Implement bounded model query matching and audit action prefix/category mapping.
- [x] Run query contract tests to green.

### Task 5: Split schema and prove v3 to v4 safety

**Files:**
- Create: `src/qb2api/accounts/schema_management.py`
- Modify: `src/qb2api/accounts/schema.py`
- Modify: `src/qb2api/accounts/repository.py`
- Modify: `tests/accounts/test_schema_migrations.py`

**Interfaces:**
- `MANAGEMENT_SCHEMA_V4` contains only `service_events`, its index, and `metric_refresh_operations`.
- `SCHEMA` composes the base schema and v4 schema in the existing migration order.

- [x] Add a realistic v3 migration test with preserved account/audit/usage data, absent v4 objects, double migration, and assertions for both new tables and index.
- [x] Verify the pre-change migration coverage omits v3 data preservation and idempotency.
- [x] Extract management schema SQL into `schema_management.py` and compose it without changing public imports.
- [x] Keep `schema.py` and every other touched production file at or below 300 lines.
- [x] Run migration and file-size tests to green.

### Task 6: Aggregate verification and commit

**Files:**
- Verify all files modified by Tasks 1-5.

- [x] Run the focused Task 2 and regression test aggregation.
- [x] Run Ruff for affected source and test directories.
- [x] Run `python -m compileall -q src/qb2api`.
- [x] Run a production file/function size check.
- [x] Run `git diff --check` and inspect `git diff --stat` plus `git status --short`.
- [x] Commit exactly `fix(admin): close management contract gaps` without pushing or merging.
