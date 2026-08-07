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
    return _aggregate_credits(accounts)


def _aggregate_credits(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    total_remaining = 0
    total_used = 0
    total_capacity = 0
    cycle_remaining = 0
    cycle_capacity = 0
    depleted = 0
    lowest: int | None = None
    unit = ""
    expires: list[int] = []
    packages: list[dict[str, Any]] = []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        summary = _summarize_account(account, len(packages) + 1)
        unit = unit or summary["unit"]
        total_remaining += summary["remaining"]
        total_used += summary["used"]
        total_capacity += summary["total"]
        cycle_remaining += summary["cycle_remaining"]
        cycle_capacity += summary["cycle_capacity"]
        depleted += int(summary["remaining"] <= 0)
        lowest = summary["remaining"] if lowest is None else min(lowest, summary["remaining"])
        if summary["expiry_ms"] is not None:
            expires.append(summary["expiry_ms"])
        packages.append(summary["package"])
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
        "packages": packages,
    }


def _account_numbers(account: dict[str, Any]) -> tuple[int, int, int, int, int]:
    return (
        _number(account.get("CapacityRemain")),
        _number(account.get("CapacityUsed")),
        _number(account.get("CapacitySize")),
        _number(account.get("CycleCapacityRemain")),
        _number(account.get("CycleCapacitySize")),
    )


def _summarize_account(account: dict[str, Any], index: int) -> dict[str, Any]:
    unit = str(account.get("CapacityUnit") or "credits")
    remain, used, size, cycle_remain, cycle_size = _account_numbers(account)
    # WorkBuddy leaves ExpiredTime empty for active packages. The package's
    # effective expiry shown in its own usage page is CycleEndTime instead.
    expiry_ms = _epoch_ms(account.get("ExpiredTime"))
    if expiry_ms is None:
        expiry_ms = _epoch_ms(account.get("CycleEndTime"))
    package_name = account.get("PackageName") or account.get("ProductName")
    if not isinstance(package_name, str) or not package_name.strip():
        package_name = f"积分包 {index}"
    package = {"name": package_name.strip(), "remaining": remain, "used": used, "total": size, "unit": unit}
    if expiry_ms is not None:
        package["expires_at"] = _epoch_ms_to_iso(expiry_ms)
    return {
        "unit": unit,
        "remaining": remain,
        "used": used,
        "total": size,
        "cycle_remaining": cycle_remain,
        "cycle_capacity": cycle_size,
        "expiry_ms": expiry_ms,
        "package": package,
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
