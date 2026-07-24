#!/usr/bin/env python3
"""Schema-only stub for Qoder check-in export JSON.

Does not read real profiles. Use on Windows after implementing profile parse
(QD-CHECKIN-01). Never print tokens.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def build_payload(
    *,
    access_token: str,
    refresh_token: str,
    account_hint: str | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "provider": "qoder",
        "account_hint": account_hint,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "exported_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write minimal Qoder check-in export JSON")
    parser.add_argument("--access-token", required=True)
    parser.add_argument("--refresh-token", required=True)
    parser.add_argument("--account-hint", default=None)
    parser.add_argument("--out", type=Path, required=True, help="output file path")
    args = parser.parse_args(argv)

    payload = build_payload(
        access_token=args.access_token,
        refresh_token=args.refresh_token,
        account_hint=args.account_hint,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # do not print secrets
    print(f"wrote export to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
