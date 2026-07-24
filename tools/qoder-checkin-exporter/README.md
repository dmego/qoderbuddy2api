# Qoder Check-in Exporter (Windows one-shot)

One-shot helper for exporting **Qoder check-in** access/refresh tokens from a
Windows machine that has already logged into QoderWork CN. The exporter is
**not** part of the Mac Mini `2api` process.

## Safety

- Do not print tokens to stdout or commit export files.
- Prefer a user-only ACL temp file; delete after import.
- Never include PAT (`pt_…`) or COSY session material in the export.
- Import only over HTTPS admin UI or Admin Key Bearer.

## Minimal JSON schema

```json
{
  "schema_version": 1,
  "provider": "qoder",
  "account_hint": "optional-label",
  "access_token": "<device or session access token>",
  "refresh_token": "<refresh token>",
  "exported_at": "2026-07-22T12:00:00Z"
}
```

Required fields: `access_token`, `refresh_token`.

Optional: `account_hint`, `exported_at`, `schema_version`.

## Runbook

1. On Windows, sign in to the target QoderWork CN account. Close any account-switcher that may rewrite the profile concurrently.
2. Run the exporter against the local profile (implementation may read `auth-v2.dat` or an equivalent store; contract is the JSON above).
3. Confirm the file contains only access/refresh (no PAT, no COSY Authorization).
4. On Mac Mini admin UI: **Accounts → Add → Qoder 签到导入**.
   - If the Qoder chat account still only exists as `qd-env-N`, promote/import a dynamic chat account first and note its `account_id`.
   - Paste `account_id`, `access_token`, `refresh_token`.
5. Wait for server-side status probe success (`checkin=active`, `verification_status=verified`).
6. Delete the temporary JSON on Windows.
7. Next day / after access expiry, confirm scheduler or refresh logs (redacted) without reopening QoderWork.

## Stub

`export_stub.py` only validates/writes the schema shape for dry-runs. Replace
with a real Windows reader when the profile path is confirmed (QD-CHECKIN-01).
