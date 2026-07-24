"""Fernet-based credential encryption vault.

Never log or return plaintext secrets.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class CredentialVault:
    """Encrypt/decrypt credential dicts with a Fernet key."""

    def __init__(self, key: str) -> None:
        key_bytes = key.encode() if isinstance(key, str) else key
        try:
            self._fernet = Fernet(key_bytes)
        except Exception as exc:
            raise ValueError(f"invalid Fernet key: {exc}") from exc
        self._fingerprint_key = hashlib.sha256(
            base64.urlsafe_b64decode(key_bytes)
        ).digest()

    def encrypt(self, payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        return self._fernet.encrypt(raw).decode()

    def decrypt(self, blob: str) -> dict[str, Any]:
        try:
            raw = self._fernet.decrypt(blob.encode() if isinstance(blob, str) else blob)
        except InvalidToken as exc:
            raise ValueError("failed to decrypt credential payload") from exc
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("credential payload must be a JSON object")
        return data

    def fingerprint(self, secret: str) -> str:
        """Return a stable internal HMAC without retaining the plaintext."""
        return hmac.new(
            self._fingerprint_key,
            secret.encode(),
            hashlib.sha256,
        ).hexdigest()
