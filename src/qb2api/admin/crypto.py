"""Constant-time hashing helpers shared by admin authentication modules."""

from __future__ import annotations

import hashlib
import secrets


def constant_time_equal(first: str, second: str) -> bool:
    return secrets.compare_digest(first.encode("utf-8"), second.encode("utf-8"))


def hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
