# SDD Progress Ledger

- Goal: implement `docs/design/macmini-multi-account-proxy-checkin.md` fully
- Active plan: `docs/superpowers/plans/2026-07-24-2api-completion-plan.md`
- Branch: `codex/multi-account-proxy-checkin`
- Execution: root integrator plus up to three isolated-worktree subagents; one commit per large delivery task
- Last saved: `2026-07-24` post-Task 9 OAuth-to-check-in hardening verified

## Approved architecture

- Persistent FastAPI Control Plane + independent loopback Proxy Worker
- `ServiceSupervisor` owns Worker lifecycle and validates PID/start-time/owner/process-group/internal-token
- Vue 3 operations console manages service, accounts, credentials, models, usage, token/points/quota, check-in, runtime settings, audit and backup
- Single-admin local console; no regular users, billing, payments, plans or redeem codes
- External protocol gates remain `CB-CHECKIN-01`, `QD-CHECKIN-01`, and `AUTH-01`

## Status

### Active 9-task completion plan

- Tasks 1 through 7 are integrated and passed their Wave 1 and Wave 2 gates.
- Task 8 code-quality and legacy cleanup is integrated.
- Task 8 removed legacy admin assets and production source maps, split persistence,
  registry, admin, runtime, protocol, Provider and Control lifecycle duties, and
  split all remaining oversized test suites.
- The repository code-limit checker now enforces production file/function,
  complexity, nesting and positional-argument limits plus the 300-line test-file
  limit. The current repository has zero violations.
- Task 8 full gate passed: 281 Python tests, 32 frontend tests, Ruff,
  compileall, typecheck, ESLint, production build, fresh/migrated real-process
  smoke, source-map absence and `git diff --check`.
- Task 9 browser acceptance, final documentation alignment and final cleanup is
  integrated. Progress: **9/9 delivery tasks integrated**.
- The local implementation and product-acceptance scope is complete. The
  remaining external protocol gates require explicit authorization to use real
  Provider credentials and perform login/check-in side effects; they are not
  represented as successful until that separate acceptance is run.
- The existing CodeBuddy account-detail verification path now safely reuses an
  OAuth/manual Chat credential for WorkBuddy verification as an internal bearer
  credential. It does not duplicate the secret or expose it to the browser.
- Every WorkBuddy credential import or verification action requires an explicit
  console confirmation because a successful request can claim that day's points.

## Existing implementation evidence

- `create_control_app` serves the Vue SPA same-origin; `create_worker_app` owns the existing OpenAI/Anthropic compatibility surface
- `RuntimeServices` owns Control-only check-in, metrics, backup and rollup services; Worker emits bounded internal telemetry
- Credentials rotate/revoke atomically with purpose state, invalidate resolver cache, rebuild dynamic pools, and return only metadata
- Check-in history is now persisted and exposed as bounded secret-free run summaries plus separate attempt detail
- Vue console uses route-level code splitting and modular ECharts; the primary shell is about 144 KB minified, chart code is lazy
- No push has been performed; all work remains on local task/integration branches.
- The final console is a light, dense infrastructure console with five navigation
  groups, responsive desktop sidebar/mobile drawer, adaptive data tables and
  accessible status presentation. Its information architecture was informed by
  the local Sub2API reference only; no Sub2API code, branding, assets or color
  system was copied.

## Verified 2026-07-23

- `rtk npm run typecheck`: exit 0
- `rtk npm run test`: 4 tests passed
- `rtk npm run lint`: exit 0
- `rtk npm run build`: exit 0; Vite emitted code-split assets in `src/qb2api/web/dist`
- `rtk pytest -q tests/integration/test_admin_ui.py ... test_stream_contract.py`: 43 passed
- `rtk pytest -q tests/control/test_supervisor.py ... test_auth_matrix.py`: 9 passed
- `rtk pytest -q tests/integration/test_account_admin.py tests/accounts/test_repository_transactions.py tests/test_accounts_vault.py tests/metrics/test_metrics_scheduler.py tests/usage/test_rollups.py`: 20 passed
- Focused Ruff for control/worker/runtime/config/CLI tests: no issues
- Focused Ruff for accounts/admin/check-in/control/worker and related tests: no issues after import formatting
- `rtk python -m compileall -q src/qb2api`: exit 0
- Supervisor integration smoke started and stopped a real Worker; no listener/process remained on port 10001
- Worker handler extraction regression: 49 focused Python tests pass, including Worker OpenAI/Anthropic/SSE/model routing, import boundaries, Control/Worker role isolation, legacy config auth, and compatibility app factory
- `src/qb2api/app.py` reduced from 848 lines to 8-line Control Plane facade; no remaining tests import its legacy global proxy state
- Full Python suite after the Control/Worker facade: `rtk pytest -q` reports 209 passed
- Usage API regression: 9 focused tests pass for compound filters, safe event detail, timeseries, CSV export, audit and rollups
- Usage console regression: frontend unit tests, typecheck, lint and production build pass; the rebuilt route exposes the same filters for summary, trend, events and CSV export
- `RuntimeSnapshotService` now sends versioned models, account slots, and hashed Proxy API Keys over the authenticated loopback handshake; Worker startup does not open SQLite, load the vault, or receive raw proxy credentials
- `ServiceSupervisor` reload now performs an authenticated Worker runtime reload and checks owner/auth-version/snapshot-version; a real TCP two-process smoke covered handshake, ready, models, reload, and shutdown
- Proxy API Key route regression: `rtk pytest -q tests/integration/test_proxy_keys.py` reports 3 passed, covering secret-safe create/list/revoke and atomic rotation
- Latest core checkpoint: `rtk pytest -q tests/integration/test_proxy_keys.py tests/control/test_runtime_snapshot.py tests/integration/test_two_process_runtime.py tests/control/test_supervisor.py tests/integration/test_auth_matrix.py` reports 15 passed
- Latest frontend checkpoint: `rtk npm run test` reports 4 tests passed; `rtk git diff --check` produced no diagnostics
- `repo_control.py` is currently 294 lines and remains within the repository file-size limit, but Proxy Key persistence should be split if further additions would exceed 300 lines

## Verified 2026-07-24 baseline commit gate

- `rtk pytest -q`: 221 passed
- `rtk ruff check src tests`: no issues found after import/style-only cleanup
- `rtk python -m compileall -q src/qb2api`: exit 0
- `rtk npm run test`: 4 tests passed
- `rtk npm run typecheck`: exit 0
- `rtk npm run lint`: exit 0
- `rtk npm run build`: exit 0
- `rtk git diff --check`: no diagnostics
- Secret-pattern scan returned only variable-expression false positives in `accounts/registry.py` and `accounts/resolver.py`; no hard-coded credential was identified
- Known file-size gate failures remain and must be planned explicitly: `tests/test_app.py`, `tests/test_checkin_clients.py`, `checkin/qoder.py`, `accounts/schema.py`, `accounts/repo_accounts.py`, `providers/qoder.py`, and legacy `web/admin.js`/`web/admin.css` exceed 300 lines

## Verified 2026-07-24 final product gate

- `rtk env PYTHONPATH=src pytest -q`: 282 passed
- `rtk ruff check src tests`: no issues found
- `rtk python -m compileall -q src/qb2api`: exit 0
- `rtk python tools/check_code_limits.py`: no violations
- `rtk npm run test`: 32 frontend unit tests passed
- `rtk npm run typecheck`, `rtk npm run lint` and `rtk npm run build`: exit 0
- `rtk npm run test:e2e`: 4 Playwright flows passed for secret-safe login,
  Worker start/stop, empty management states, and narrow-screen navigation
- Desktop and mobile Lighthouse audits: Accessibility, Best Practices, SEO and
  Agentic Browsing are all 100; 49 audits passed with no console messages
- `rtk env PYTHON_BIN=.venv/bin/python bash scripts/smoke_fresh_install.sh` and
  `rtk env PYTHON_BIN=.venv/bin/python bash scripts/smoke_migrated_install.sh`:
  both passed
- Production static assets contain no source maps or `sourceMappingURL` markers;
  `rtk git diff --check` produced no diagnostics
- Playwright used an isolated generated control configuration and test-only admin
  key. It did not read a real `.env`, decrypt real credentials, or contact an
  upstream Provider.

## Verified 2026-07-24 post-Task 9 hardening

- `rtk env PYTHONPATH=src pytest -q`: 283 passed
- `rtk ruff check src tests`, `rtk python -m compileall -q src/qb2api`, and
  `rtk python tools/check_code_limits.py`: passed
- `rtk npm run test`: 33 frontend unit tests passed; `rtk npm run typecheck`,
  `rtk npm run lint`, and `rtk npm run build`: passed
- `rtk npm run test:e2e`: 4 Playwright management-console flows passed
- Config-default tests bind an empty `env_file`, ensuring a developer's local
  `.env` cannot pollute an environment-isolation assertion.
- All WorkBuddy calls in this checkpoint were mocked or isolated. No real
  `.env` credential, browser session, or upstream Provider was used.

## Verified 2026-07-24 Chinese console visual refinement

- Reworked the Vue console as a Chinese, table-first operations interface:
  grouped navigation, header actions, filters and data surfaces now use one
  compact visual system; no Sub2API source, branding, assets or color tokens
  were copied.
- Replaced user-facing English navigation, page eyebrows, drawers, filters and
  status enums with Chinese labels while retaining provider/protocol names such
  as CodeBuddy, Qoder, Token, Cookie and HTTP where they are technical terms.
- Added a centralized status-label mapper so upstream values such as
  `HEALTHY`, `STOPPED`, `needs_reauth` and `scheduled` are not rendered as raw
  internal enums in the console.
- `rtk npm run test`: 33 frontend unit tests passed; `rtk npm run typecheck`,
  `rtk npm run lint` and `rtk npm run build`: passed.
- `rtk npm run test:e2e`: 4 Playwright flows passed. The narrow-screen flow
  now asserts the mobile sidebar completes its slide-in transform before it
  treats the navigation as visible. Desktop and mobile screenshots used the
  isolated generated control configuration and a test-only administrator key.

## Completion checkpoint

1. Tasks 1 through 9 are integrated on `codex/multi-account-proxy-checkin`.
2. Wave 1, Wave 2, Task 8 and Task 9 gates passed, including the final full
   Python/frontend/E2E/fresh-install/migrated-install/browser audits; the
   post-Task 9 OAuth-to-check-in regression gate adds 283 Python and 33
   frontend unit tests.
3. No remote operation has been performed. Real-provider acceptance remains a
   separately authorized activity because it requires real account login and can
   trigger upstream check-in side effects.

## Deliberately deferred at this checkpoint

- No push, branch rewrite, or destructive cleanup has been performed. The Task 9
  code baseline is `dec1e62`; its completion record is `1640970`.
- Post-Task 9 OAuth-to-check-in hardening is `08a1d82`; it remains local and
  does not represent real-provider protocol acceptance.
- Trusted remote HTTP is explicitly supported only with
  `QB2API_ADMIN_COOKIE_SECURE=false`; secure-by-default `auto` remains enforced.
- Real `CB-CHECKIN-01`, `QD-CHECKIN-01`, and `AUTH-01` protocol acceptance is
  still pending explicit authorization to use an actual account and can cause
  a real check-in side effect.

## Commit checkpoints

- `703cdfc feat: add control plane refactor baseline`
- `545f44e docs(plan): define parallel completion work`
- `d68ea72 chore: ignore task worktrees`
- `59f1a99 feat(security): close admin and proxy access`
- `6b09701 fix(security): enforce proxy key lifecycle`
- `13dd5d8 fix(security): surface pending key revocation`
- Task 1 final review: Approved; no remaining Critical, Important or Minor.
- `f01555b refactor(checkin): harden provider protocols`
- `339db2b fix(checkin): close refresh and export races`
- Task 3 final review: Approved; no remaining Critical or Important.
- `6415805 feat(admin): complete management contracts`
- `47dd7f4 fix(admin): close management contract gaps`
- `b7848e0 fix(integration): align management contracts`
- `57c4a4e feat(console): complete operations workflows`
- `dd13ab3 fix(console): align operations contracts`
- `c701bd1 fix(console): harden operations feedback`
- Wave 1 gate at `c701bd1`: 266 Python tests and 30 frontend tests passed;
  Ruff, compileall, typecheck, ESLint, production build and diff check passed.
- `3887fd3 feat(accounts): close onboarding workflows`
- `cd79b52 feat(ops): close schedulers and recovery`
- `3b92098 docs(deploy): add migration and runbooks`
- Wave 2 integration fix: async check-in contract test alignment and busy-run
  failure audit with stable `checkin_run_in_progress` error code.
- Wave 2 gate: 278 Python tests and 32 frontend tests passed; Ruff, compileall,
  typecheck, ESLint, production build, fresh/migrated smoke, launchd plist and
  diff checks passed.
- `42d268d refactor(web): remove legacy admin assets`
- `32ffde9 refactor(runtime): reduce complexity debt`
- `27f5016 refactor(accounts): split persistence duties`
- `1964dc1 refactor(runtime): close code limit gaps`
- `5322c97 test: split oversized suites`
- Task 8 gate: 281 Python tests and 32 frontend tests passed; Ruff, compileall,
  repository code limits, typecheck, ESLint, production build, source-map
  absence, fresh/migrated smoke and diff checks passed.
- `dec1e62 test(e2e): complete product acceptance`
- Task 9 gate: 282 Python tests, 32 frontend unit tests and 4 Playwright tests
  passed; Ruff, compileall, repository code limits, typecheck, ESLint,
  production build, fresh/migrated smoke, browser audits, source-map absence
  and diff checks passed.
- `08a1d82 fix(checkin): verify OAuth chat credentials`
- Post-Task 9 hardening gate: 283 Python tests, 33 frontend unit tests and 4
  Playwright tests passed; Ruff, compileall, repository code limits, typecheck,
  ESLint, production build and diff checks passed.
