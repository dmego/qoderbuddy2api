"""Safe Qoder quota client and response normalization."""

from __future__ import annotations

from datetime import UTC, datetime
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
        "is_quota_exceeded", "isQuotaExceeded",
    )
    for key in fields:
        if key in body and _safe_scalar(body[key]):
            result[_snake_key(key)] = body[key]
    # 到期时间归一为 ISO 串（上游给毫秒时间戳或 ISO 串，两种都要兼容）
    expires = _extract_expires_at(body)
    if expires:
        result["expires_at"] = expires
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
    packages = _extract_packages(body)
    if packages:
        result["packages"] = packages
    return result


def _quota_detail(value: dict[str, Any]) -> dict[str, Any]:
    keys = ("total", "used", "remaining", "percentage", "unit", "cap", "available")
    result = {key: value[key] for key in keys if key in value and _safe_scalar(value[key])}
    expires = _to_iso(value.get("expires_at", value.get("expiresAt")))
    if expires:
        result["expires_at"] = expires
    return result


def _extract_packages(body: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[Any] = []
    for key in ("packages", "quotaPackages", "quota_packages", "earnedPackages", "earned_packages", "earned"):
        value = body.get(key)
        if isinstance(value, list):
            candidates.extend(value)
    packages: list[dict[str, Any]] = []
    for item in candidates[:100]:
        package = _normalize_package(item)
        if package:
            packages.append(package)
    return packages


def _normalize_package(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    package = _package_labels(item)
    _package_amounts(item, package)
    expires = _to_iso(item.get("expires_at", item.get("expiresAt", item.get("expireAt"))))
    if expires:
        package["expires_at"] = expires
    if not any(key in package for key in ("remaining", "total", "used")):
        return None
    package.setdefault("name", "积分包")
    return package


def _package_labels(item: dict[str, Any]) -> dict[str, Any]:
    package: dict[str, Any] = {}
    for key in ("name", "title", "type", "unit"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            package["name" if key in {"title", "type"} else key] = value.strip()
    return package


def _package_amounts(item: dict[str, Any], package: dict[str, Any]) -> None:
    for key in ("remaining", "total", "used", "available", "amount", "credits"):
        value = item.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            package[key if key not in {"available", "amount", "credits"} else "remaining"] = value


def _extract_expires_at(body: dict[str, Any]) -> str | None:
    """上游 expires_at 可能是 epoch 毫秒（数字/数字串）或 ISO 串，统一成 ISO。"""
    raw = body.get("expires_at")
    if raw is None:
        raw = body.get("expiresAt")
    return _to_iso(raw)


def _to_iso(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _epoch_ms_to_iso(int(value))
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return _epoch_ms_to_iso(int(text))
    return _iso_normalize(text)


def _epoch_ms_to_iso(value: int) -> str | None:
    # 毫秒戳太长（如 253402214400000）仍合法；秒级戳也兜底
    seconds = value / 1000 if value > 10_000_000_000 else value
    try:
        return datetime.fromtimestamp(seconds, tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _iso_normalize(text: str) -> str | None:
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.isoformat()


def _safe_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float, str))


def _snake_key(value: str) -> str:
    aliases = {
        "userType": "user_type",
        "totalUsagePercentage": "total_usage_percentage",
        "isQuotaExceeded": "is_quota_exceeded",
    }
    return aliases.get(value, value)
