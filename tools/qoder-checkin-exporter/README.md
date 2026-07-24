# Qoder Check-in Exporter (Windows one-shot)

This helper decrypts the current Windows user's QoderWork CN `auth-v2.dat` and
writes the minimum credential payload accepted by the protected 2api Qoder
check-in import workflow. It is a one-shot operations tool, not a daemon and
not part of the Mac Mini service.

The implementation follows the locally inspected
`qoderwork-account-switcher` commit `022c1d4`: `Local State` contains a
DPAPI-wrapped 32-byte key, while `auth-v2.dat` uses `v10` + AES-256-GCM.
Encrypted profile export is intentionally Windows-only because DPAPI is tied to
the logged-in Windows user.

## Requirements

- Run as the same Windows user that signed in to QoderWork CN.
- Python 3.10 or newer.
- `cryptography>=42` (`py -3 -m pip install "cryptography>=42"`).
- Default profile location: `%APPDATA%\QoderWork CN`.
- Close QoderWork account switching while exporting so the profile is stable.

## Export

```powershell
py -3 .\export_stub.py export `
  --out "$env:USERPROFILE\qoder-checkin-export.json" `
  --account-hint "qoder-main"
```

The historical filename `export_stub.py` is retained for compatibility; it is
now the complete one-shot exporter. The command:

- never accepts access or refresh tokens as command-line arguments;
- never prints credentials to stdout or stderr;
- refuses to overwrite an existing output file;
- writes with mode `0600` and applies a current-user-only Windows ACL;
- rejects chat PATs (`pt_...`) and COSY Authorization values;
- emits no Cookie, desktop profile, PAT, COSY key, or Authorization header.

For a non-default installation, pass `--app-data-dir`. For an account profile
backup, pass its `auth-v2.dat` with `--auth-file` while keeping
`--app-data-dir` pointed at the live QoderWork directory containing `Local
State`; its DPAPI-wrapped master key is required for decryption.

## Validate

Validation reports only schema status and never prints credential values:

```powershell
py -3 .\export_stub.py validate `
  "$env:USERPROFILE\qoder-checkin-export.json"
```

The strict export schema is:

```json
{
  "version": 1,
  "provider": "qoder",
  "account_hint": "optional-label",
  "access_token": "<device or session access token>",
  "refresh_token": "<refresh token>",
  "expires_at": "2026-07-25T00:00:00Z"
}
```

`account_hint` and `expires_at` are optional. Unknown fields are rejected by
the validator to prevent accidental export of full profile data.

## Import Runbook

1. Promote/import the Qoder chat account first if only a transient `qd-env-N`
   account exists. Record the durable `account_id` selected in the admin UI.
2. Open the 2api management console over HTTPS or an explicitly trusted admin
   connection. Use the dedicated Qoder check-in import workflow when it is
   available in the integrated Admin UI.
3. The protected Control Plane contract is `POST /api/admin/auth/qoder/checkin`
   with only `account_id`, `access_token`, and `refresh_token`. It does not
   accept an `auth-v2.dat` file, a complete desktop profile, a chat PAT, or a
   COSY Authorization value. Do not put this JSON into curl arguments, shell
   history, a URL, browser storage, or a generic credential endpoint; submit it
   through the dedicated authenticated UI workflow.
4. Treat import as successful only after the server-side Qoder status probe
   reports the check-in purpose as active and verified. A failed probe must not
   replace the existing stored credential.
5. Delete the temporary JSON after import and empty any transfer-location
   trash/recycle bin according to local policy.
6. On a later access expiry, confirm a redacted refresh/status/claim result.
   The Mac Mini must not require QoderWork or this exporter to remain running.

The current profile path, DPAPI/AES format, endpoint behavior, and token fields
come from local reference source inspection. No real upstream credential was
used by automated tests, so runtime QD-CHECKIN-01 remains unverified until an
authorized redacted probe records HTTP status and outcome.

This Task 3 verification ran on macOS. The Windows DPAPI and `icacls` paths are
covered by platform simulation tests but were not exercised on a real Windows
host in this round.

## Boundaries and handoff

The exporter only prepares the Qoder access/refresh pair. It does not import
anything into 2api, run daily check-in, retain profile data, or make Windows a
part of the Mac Mini service. After a successful import, delete the temporary
JSON according to local policy and confirm only redacted status in the Admin UI.

WorkBuddy/CodeBuddy check-in credentials use a separate provider-specific
workflow. Do not repurpose this exporter, reuse Qoder fields, or save a
WorkBuddy Cookie/Bearer in `.env`, a URL, browser storage, or an unsupported
generic credential API while that workflow is unavailable.
