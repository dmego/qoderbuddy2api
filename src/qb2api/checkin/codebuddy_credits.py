"""CodeBuddy/WorkBuddy credit balance client (CB-CREDITS-01)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from .base import join_url, parse_json_body


class CodeBuddyCreditsUnavailableError(RuntimeError):
    """The upstream credits endpoint did not provide a usable snapshot."""


class CodeBuddyCreditsClient:
    """Fetch only the aggregate credit fields required by the console."""

    def __init__(
        self,
        *,
        base_url: str = "https://www.workbuddy.cn",
        path: str = "/billing/meter/get-user-resource",
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.path = path
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0))
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self, access_token: str) -> dict[str, Any]:
        if not access_token:
            raise CodeBuddyCreditsUnavailableError("access credential unavailable")
        try:
            response = await self._client.post(
                join_url(self.base_url, self.path),
                json={},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-Client-Platform": "web",
                },
            )
        except httpx.HTTPError as error:
            raise CodeBuddyCreditsUnavailableError(
                f"transport:{type(error).__name__}"
            ) from error
        body = parse_json_body(response.text)
        if not 200 <= response.status_code < 300:
            raise CodeBuddyCreditsUnavailableError(f"http:{response.status_code}")
        normalized = normalize_credits(body)
        if not normalized:
            raise CodeBuddyCreditsUnavailableError("empty credits response")
        return normalized


def normalize_credits(body: dict[str, Any] | None) -> dict[str, Any]:
    """Return a bounded, secret-free aggregate of the credits response."""
    if not isinstance(body, dict) or body.get("code") not in (0, "0"):
        return {}
    data = body.get("data")
    response = data.get("Response") if isinstance(data, dict) else None
    payload = response.get("Data") if isinstance(response, dict) else None
    accounts = payload.get("Accounts") if isinstance(payload, dict) else None
    if not isinstance(accounts, list) or not accounts:
        return {}
    total_remaining = 0
    total_used = 0
    total_capacity = 0
    cycle_remaining = 0
    cycle_capacity = 0
    depleted = 0
    lowest: int | None = None
    unit = ""
    expires: list[int] = []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        unit = unit or str(account.get("CapacityUnit") or "")
        remain = _number(account.get("CapacityRemain"))
        used = _number(account.get("CapacityUsed"))
        size = _number(account.get("CapacitySize"))
        cycle_remain = _number(account.get("CycleCapacityRemain"))
        cycle_size = _number(account.get("CycleCapacitySize"))
        total_remaining += remain
        total_used += used
        total_capacity += size
        cycle_remaining += cycle_remain
        cycle_capacity += cycle_size
        if remain <= 0:
            depleted += 1
        if lowest is None or remain < lowest:
            lowest = remain
        epoch_ms = _epoch_ms(account.get("ExpiredTime"))
        if epoch_ms is not None:
            expires.append(epoch_ms)
    return {
        "unit": unit or "credits",
        "total_remaining": total_remaining,
        "total_used": total_used,
        "total_capacity": total_capacity,
        "cycle_remaining": cycle_remaining,
        "cycle_capacity": cycle_capacity,
        "package_count": len(accounts),
        "depleted_packages": depleted,
        "lowest_remaining": lowest if lowest is not None else 0,
        "expires_at": _epoch_ms_to_iso(min(expires)) if expires else None,
    }


def _number(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _epoch_ms_to_iso(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()


def _epoch_ms(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)
