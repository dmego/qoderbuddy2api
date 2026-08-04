"""Qoder daily free-model activity client (QD-ACTIVITY-01).

Calls the COSY-authenticated Algo API `/api/v2/activity` to list each model's
daily free quota (limit / used / remaining). Auth reuses an authenticated
QoderSession's COSY signature material, recomputed for the activity path with
an empty body — the same scheme the chat path and the reference
`qoderwork-account-switcher` quota.rs use.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from ..providers.qoder_auth import GATEWAY, QoderError, QoderSession, _md5
from .base import parse_json_body
from .quota import QuotaUnavailableError

logger = logging.getLogger("qb2api.checkin.activity")


class QoderActivityClient:
    """Fetch the daily free-model activity list via COSY auth."""

    def __init__(
        self,
        *,
        base_url: str = GATEWAY,
        path: str = "/algo/api/v2/activity",
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

    async def fetch(self, pat: str) -> list[dict[str, Any]]:
        """Return one normalized entry per activity (model name, tag, quota).

        Raises QuotaUnavailableError on transport / HTTP / shape failures so the
        collector records a backoff instead of crashing the scheduler.
        """
        if not pat:
            raise QuotaUnavailableError("pat unavailable")
        headers = await _activity_headers(pat, self.path)
        try:
            response = await self._client.get(
                f"{self.base_url}{self.path}",
                headers=headers,
            )
        except httpx.HTTPError as error:
            raise QuotaUnavailableError(f"transport:{type(error).__name__}") from error
        return _parse_activity_response(response)


def _parse_activity_response(response: httpx.Response) -> list[dict[str, Any]]:
    if not 200 <= response.status_code < 300:
        raise QuotaUnavailableError(f"http:{response.status_code}")
    activities = _extract_activities(parse_json_body(response.text))
    normalized = [_normalize_activity(item) for item in activities if isinstance(item, dict)]
    normalized = [item for item in normalized if item["model"] or item["remaining"] is not None]
    if not normalized:
        raise QuotaUnavailableError("empty activity response")
    return normalized


async def _activity_headers(pat: str, path: str = "/algo/api/v2/activity") -> dict[str, str]:
    """Build COSY-signed GET headers for the activity endpoint.

    Reuses an ephemeral authenticated QoderSession for the payload/cosy_key
    material, then recomputes the MD5 signature for the activity path (with the
    `/algo` prefix stripped, matching the reference client) and an empty body.
    """
    session = QoderSession(pat)
    try:
        await session.authenticate()
    except QoderError as error:
        raise QuotaUnavailableError(f"auth:{error.status_code}") from error
    except Exception as error:
        raise QuotaUnavailableError(f"auth:{type(error).__name__}") from error
    finally:
        await session.close()

    cosy_date = str(int(time.time()))
    sign_path = _sign_path(path)
    signature = _md5("\n".join([session.payload_b64, session.cosy_key, cosy_date, "", sign_path]))
    auth_token = f"Bearer COSY.{session.payload_b64}.{signature}"
    return {
        "X-Request-Id": session.machine_id,
        "X-IDE-Platform": "qoder_work",
        "X-Version": "1.0.0",
        "X-Machine-OS": "win32",
        "Cosy-User": session.user_id,
        "Cosy-Key": session.cosy_key,
        "Cosy-Date": cosy_date,
        "Authorization": auth_token,
        "Accept": "application/json",
        "User-Agent": "QoderWork",
        "cosy-version": "0.1.43",
        "cosy-clienttype": "5",
        "cosy-machineid": session.machine_id,
        "cosy-machinetoken": session.machine_token,
        "login-version": "v2",
    }


def _sign_path(path: str) -> str:
    """Strip the `/algo` gateway prefix so the COSY signature matches the chat path convention."""
    return path[5:] if path.startswith("/algo") else path


def _extract_activities(body: dict[str, Any] | None) -> list[Any]:
    if not isinstance(body, dict):
        return []
    data = body.get("data")
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return body.get("activities") if isinstance(body.get("activities"), list) else []
    activities = data.get("activities") or data.get("activity")
    return activities if isinstance(activities, list) else []


def _normalize_activity(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": _safe_text(item.get("modelName") or item.get("model_name")),
        "tag": _safe_text(item.get("tag")),
        "limit": _safe_number(item.get("limit")),
        "used": _safe_number(item.get("used")),
        "remaining": _safe_number(item.get("remaining")),
        "reset_at": _safe_scalar(item.get("resetAt")),
        "status_text": _safe_text(item.get("statusText")),
        "eligible": item.get("eligible") if isinstance(item.get("eligible"), bool) else None,
        "activity_end_at": _safe_scalar(item.get("activityEndAt")),
    }


def _safe_text(value: Any) -> str | None:
    return value.strip()[:120] if isinstance(value, str) and value.strip() else None


def _safe_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _safe_scalar(value: Any) -> int | float | str | None:
    if isinstance(value, bool) or value is None:
        return value
    return value if isinstance(value, (int, float, str)) else None
