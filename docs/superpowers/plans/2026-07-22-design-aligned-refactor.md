# Design-Aligned Refactor Plan

> **For agentic workers:** Fix completion gaps vs `docs/design/macmini-multi-account-proxy-checkin.md`. No auto-commit.

**Goal:** Make the implementation deployable for Mac Mini dual check-in + multi-account proxy per design acceptance checklist §20.

**Architecture:** Keep module layout; fix broken control planes (startup gate, verification/promotion, check-in eligibility, session CSRF, CAS refresh, admin ops UI).

**Tech Stack:** existing FastAPI / aiosqlite / Fernet.

## Global Constraints

- No git commit unless user asks
- Design document is source of truth
- TDD for each critical fix
- Do not invent WorkBuddy cookie automation beyond design

## Critical Gaps (from review)

1. Default `admin_ui_enabled=True` forces ADMIN+CREDENTIAL keys → env-only proxy cannot boot
2. CodeBuddy check-in never becomes `verification_status=verified` → scheduler never selects CB accounts
3. Env → durable promotion missing → cannot attach Qoder check-in to env PAT cleanly
4. CSRF in sessionStorage (XSS risk)
5. Trusted proxy IP not used for login rate limit
6. credential refresh not CAS on `credential_version`
7. Admin UI missing: enable/disable, promote, probe, next_run, targeted check-in
8. app.py still god-file (defer structural split if time; prefer behavior first)

## Task Order

### Task A: Startup gate / env-only boot
### Task B: Check-in eligibility + CB verification path
### Task C: Env promote API + UI
### Task D: Session CSRF memory-only + trusted proxy IP
### Task E: CAS credential_version on refresh writeback
### Task F: Admin UI ops completeness
### Task G: Integration tests for auth matrix + check-in isolation
### Task H: Full pytest + design checklist report

---
