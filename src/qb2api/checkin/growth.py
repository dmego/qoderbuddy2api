"""WorkBuddy 成长中心只读客户端（WG-GROWTH-01）。

调 /v2/activity/growth/tasks 与 /v2/activity/growth/profile 拉取任务列表与成长档案，
对齐 workbuddy.cn/profile/growth-center 页面只读展示。鉴权复用 codebuddy OAuth
access_token（Bearer），必须带浏览器请求头（Origin/Referer/User-Agent），否则
APISIX 网关返回 401。
"""

from __future__ import annotations

from typing import Any

import httpx

from .base import parse_json_body


class GrowthUnavailableError(RuntimeError):
    """成长中心端点不可用或鉴权被拒。"""


class WorkBuddyGrowthClient:
    """只读拉取成长中心任务与档案。"""

    def __init__(
        self,
        *,
        base_url: str = "https://www.workbuddy.cn",
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0))
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self, access_token: str) -> dict[str, Any]:
        """返回 {profile, tasks}；任一端点失败抛 GrowthUnavailableError。"""
        if not access_token:
            raise GrowthUnavailableError("access token unavailable")
        headers = self._headers(access_token)
        profile = await self._get("/v2/activity/growth/profile", headers)
        tasks = await self._get("/v2/activity/growth/tasks", headers)
        return {"profile": _profile(profile), "tasks": [_task(t) for t in _tasks(tasks)]}

    def _headers(self, access_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/profile/growth-center",
        }

    async def _get(self, path: str, headers: dict[str, str]) -> dict[str, Any]:
        try:
            response = await self._client.get(f"{self.base_url}{path}", headers=headers)
        except httpx.HTTPError as error:
            raise GrowthUnavailableError(f"transport:{type(error).__name__}") from error
        body = parse_json_body(response.text)
        if response.status_code == 401:
            raise GrowthUnavailableError("auth_rejected")
        if not 200 <= response.status_code < 300:
            raise GrowthUnavailableError(f"http:{response.status_code}")
        if not isinstance(body, dict) or body.get("code") not in (0, "0"):
            raise GrowthUnavailableError("upstream_error")
        data = body.get("data")
        return data if isinstance(data, dict) else {}


def _profile(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "level": data.get("level"),
        "completed": data.get("completed"),
        "total": data.get("total"),
        "max_level": data.get("max_level"),
        "first_visit": data.get("first_visit"),
    }


def _tasks(data: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = data.get("tasks")
    return tasks if isinstance(tasks, list) else []


def _task(item: dict[str, Any]) -> dict[str, Any]:
    progress = item.get("progress")
    current = None
    target = None
    if isinstance(progress, dict):
        current = progress.get("current")
        target = progress.get("target")
    return {
        "task_code": item.get("task_code"),
        "title": item.get("title"),
        "task_desc": item.get("task_desc"),
        "task_type": item.get("task_type"),
        "tag": item.get("tag"),
        "accept_status": item.get("accept_status"),
        "progress_current": current,
        "progress_target": target,
        "reward_credit": item.get("reward_credit"),
        "reward_energy": item.get("reward_energy"),
        "has_reward": item.get("has_reward"),
        "locked": item.get("locked"),
        "is_new": item.get("is_new"),
        "icon_url": item.get("icon_url"),
    }
