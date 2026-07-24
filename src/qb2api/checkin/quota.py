"""Safe Qoder quota client and response normalization."""

from __future__ import annotations

from typing import Any

import httpx

from .base import join_url, parse_json_body


class QuotaUnavailableError(RuntimeError):
    """The upstream quota endpoint did not provide a usable snapshot."""


class QoderQuotaClient:
    """Fetch only the fields required by the operations console."""

    def __init__(
        self,
        *,
        base_url: str = "https://openapi.qoder.com.cn",
        path: str = "/api/v2/quota/usage",
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
            raise QuotaUnavailableError("access credential unavailable")
        try:
            response = await self._client.get(
                join_url(self.base_url, self.path),
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    "User-Agent": "QoderWork",
                },
            )
        except httpx.HTTPError as error:
            raise QuotaUnavailableError(f"transport:{type(error).__name__}") from error
        body = parse_json_body(response.text)
        if not 200 <= response.status_code < 300:
            raise QuotaUnavailableError(f"http:{response.status_code}")
        normalized = normalize_quota(body)
        if not normalized:
            raise QuotaUnavailableError("empty quota response")
        return normalized


def normalize_quota(body: dict[str, Any] | None) -> dict[str, Any]:
    """Return a bounded, secret-free representation of the quota response."""
    if not isinstance(body, dict):
        return {}
    result: dict[str, Any] = {}
    fields = (
        "user_type", "userType", "total_usage_percentage", "totalUsagePercentage",
        "is_quota_exceeded", "isQuotaExceeded", "expires_at", "expiresAt",
    )
    for key in fields:
        if key in body and _safe_scalar(body[key]):
            result[_snake_key(key)] = body[key]
    for source, target in (
        ("user_quota", "user_quota"),
        ("userQuota", "user_quota"),
        ("add_on_quota", "add_on_quota"),
        ("addOnQuota", "add_on_quota"),
        ("org_resource_package", "org_resource_package"),
        ("orgResourcePackage", "org_resource_package"),
    ):
        value = body.get(source)
        if isinstance(value, dict):
            result[target] = _quota_detail(value)
    return result


def _quota_detail(value: dict[str, Any]) -> dict[str, Any]:
    keys = ("total", "used", "remaining", "percentage", "unit", "cap", "available")
    return {key: value[key] for key in keys if key in value and _safe_scalar(value[key])}


def _safe_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float, str))


def _snake_key(value: str) -> str:
    aliases = {
        "userType": "user_type",
        "totalUsagePercentage": "total_usage_percentage",
        "isQuotaExceeded": "is_quota_exceeded",
        "expiresAt": "expires_at",
    }
    return aliases.get(value, value)
