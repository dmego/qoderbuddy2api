"""Check-in schedule parsing and jitter helpers."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta


def parse_checkin_at(value: str) -> tuple[int, int]:
    parts = (value or "00:10").strip().split(":")
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid CHECKIN_AT: {value}")
    return hour, minute


def next_run_after(now: datetime, hour: int, minute: int) -> datetime:
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate + timedelta(days=1) if candidate <= now else candidate


def jitter_seconds(minimum: int, maximum: int) -> float:
    lower, upper = min(minimum, maximum), max(minimum, maximum)
    if upper <= 0:
        return 0.0
    return float(secrets.randbelow(upper - lower + 1) + lower)
