#!/usr/bin/env python3
"""Windows one-shot Qoder check-in credential exporter.

The filename is retained for compatibility. The implementation reads the local
QoderWork CN profile and never accepts secrets as command-line arguments.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAX_INPUT_BYTES = 1024 * 1024
MAX_SECRET_CHARS = 16_384
_ALLOWED_EXPORT_KEYS = frozenset(
    {
        "version",
        "provider",
        "account_hint",
        "access_token",
        "refresh_token",
        "expires_at",
    }
)


def build_payload(
    *,
    access_token: str,
    refresh_token: str,
    expires_at: str | int | float | None = None,
    account_hint: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": 1,
        "provider": "qoder",
        "access_token": _validate_secret(access_token, "access_token"),
        "refresh_token": _validate_secret(refresh_token, "refresh_token"),
    }
    if account_hint is not None:
        hint = account_hint.strip()
        if not hint or len(hint) > 128 or any(char in hint for char in "\r\n"):
            raise ValueError("account_hint must be 1..128 characters on one line")
        payload["account_hint"] = hint
    if expires_at is not None:
        payload["expires_at"] = _validate_expiry(expires_at)
    return payload


def export_from_profile(
    *,
    auth_file: Path,
    app_data_dir: Path,
    account_hint: str | None = None,
) -> dict[str, Any]:
    auth = _read_auth_file(auth_file, app_data_dir)
    access = auth.get("token") or auth.get("device_token") or auth.get("access_token")
    refresh = auth.get("refreshToken") or auth.get("refresh_token")
    expires = auth.get("expiresAt") or auth.get("expires_at")
    return build_payload(
        access_token=access,
        refresh_token=refresh,
        expires_at=expires,
        account_hint=account_hint,
    )


def validate_export_payload(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("export must be a JSON object")
    unknown = set(payload) - _ALLOWED_EXPORT_KEYS
    if unknown:
        raise ValueError(f"unsupported export fields: {', '.join(sorted(unknown))}")
    if payload.get("version") != 1 or payload.get("provider") != "qoder":
        raise ValueError("expected Qoder export version 1")
    return build_payload(
        access_token=payload.get("access_token"),
        refresh_token=payload.get("refresh_token"),
        expires_at=payload.get("expires_at"),
        account_hint=payload.get("account_hint"),
    )


def _read_auth_file(auth_file: Path, app_data_dir: Path) -> dict[str, Any]:
    raw = _read_limited(auth_file)
    if raw.startswith(b"{"):
        plain = raw
    else:
        if sys.platform != "win32":
            raise ValueError("encrypted auth-v2.dat can only be decrypted on Windows")
        plain = _decrypt_auth_data(raw, app_data_dir)
    try:
        payload = json.loads(plain.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("auth-v2.dat did not contain valid UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("auth-v2.dat JSON root must be an object")
    return payload


def _decrypt_auth_data(raw: bytes, app_data_dir: Path) -> bytes:
    if not raw.startswith(b"v10"):
        raise ValueError("unsupported auth-v2.dat format")
    payload = raw[3:]
    if len(payload) < 28:
        raise ValueError("auth-v2.dat encrypted payload is too short")
    key = _load_master_key(app_data_dir)
    if len(key) != 32:
        raise ValueError("QoderWork AES master key must be 32 bytes")
    try:
        return AESGCM(key).decrypt(payload[:12], payload[12:], None)
    except Exception as error:
        raise ValueError("auth-v2.dat AES-GCM decryption failed") from error


def _load_master_key(app_data_dir: Path) -> bytes:
    state = _load_json_file(app_data_dir / "Local State")
    try:
        encoded = state["os_crypt"]["encrypted_key"]
        encrypted = base64.b64decode(encoded, validate=True)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Local State is missing a valid encrypted_key") from error
    if not encrypted.startswith(b"DPAPI"):
        raise ValueError("Local State encrypted_key is not DPAPI wrapped")
    return _dpapi_decrypt(encrypted[5:])


def _dpapi_decrypt(ciphertext: bytes) -> bytes:
    if sys.platform != "win32":
        raise ValueError("DPAPI is only available on Windows")
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]

    source = ctypes.create_string_buffer(ciphertext)
    input_blob = DataBlob(len(ciphertext), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte)))
    output_blob = DataBlob()
    crypt32 = ctypes.windll.crypt32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob), None, None, None, None, 0, ctypes.byref(output_blob)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.data, output_blob.size)
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.data)


def _validate_secret(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    secret = value.strip()
    if not secret or len(secret) > MAX_SECRET_CHARS or any(c.isspace() for c in secret):
        raise ValueError(f"{field} is empty or malformed")
    lowered = secret.lower()
    if field == "access_token" and (
        lowered.startswith("pt_") or lowered.startswith("bearer cosy.")
    ):
        raise ValueError("access_token must be a device/session token, not PAT or COSY")
    return secret


def _validate_expiry(value: str | int | float) -> str | int | float:
    if isinstance(value, (int, float)):
        if value <= 0:
            raise ValueError("expires_at must be positive")
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expires_at must be an ISO-8601 string or epoch")
    normalized = value.strip()
    if normalized.isdigit():
        return normalized
    try:
        datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("expires_at must be valid ISO-8601") from error
    return normalized


def _read_limited(path: Path) -> bytes:
    if not path.is_file():
        raise ValueError(f"input file not found: {path}")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError(f"input file exceeds {MAX_INPUT_BYTES} bytes")
    return path.read_bytes()


def _load_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(_read_limited(path).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid JSON file: {path}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def _write_secure(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
        if sys.platform == "win32":
            _restrict_windows_acl(path)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _restrict_windows_acl(path: Path) -> None:
    domain = os.getenv("USERDOMAIN", "").strip()
    username = os.getenv("USERNAME", "").strip() or getpass.getuser()
    identity = f"{domain}\\{username}" if domain else username
    command = [
        "icacls",
        str(path),
        "/inheritance:r",
        "/grant:r",
        f"{identity}:(R,W)",
    ]
    result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode != 0:
        raise ValueError("failed to restrict output ACL to the current Windows user")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export minimal Qoder check-in credentials")
    subparsers = parser.add_subparsers(dest="command", required=True)
    export = subparsers.add_parser("export", help="decrypt the local QoderWork profile")
    export.add_argument("--app-data-dir", type=Path)
    export.add_argument("--auth-file", type=Path)
    export.add_argument("--account-hint")
    export.add_argument("--out", type=Path, required=True)
    validate = subparsers.add_parser("validate", help="validate an export without printing it")
    validate.add_argument("path", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            validate_export_payload(_load_json_file(args.path))
            print("valid Qoder check-in export (version 1)", file=sys.stderr)
            return 0
        app_data = args.app_data_dir or _default_app_data_dir()
        auth_file = args.auth_file or app_data / "auth-v2.dat"
        payload = export_from_profile(
            auth_file=auth_file,
            app_data_dir=app_data,
            account_hint=args.account_hint,
        )
        _write_secure(args.out, payload)
        print(f"wrote protected export to {args.out}", file=sys.stderr)
        return 0
    except (OSError, ValueError) as error:
        print(f"export failed: {error}", file=sys.stderr)
        return 2


def _default_app_data_dir() -> Path:
    if sys.platform != "win32":
        raise ValueError("automatic QoderWork profile discovery requires Windows")
    appdata = os.getenv("APPDATA")
    if not appdata:
        raise ValueError("APPDATA is not set")
    return Path(appdata) / "QoderWork CN"


if __name__ == "__main__":
    raise SystemExit(main())
