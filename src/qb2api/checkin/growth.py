"""WorkBuddy 成长中心只读客户端（WG-GROWTH-01）。

调 /v2/activity/growth/* 拉取任务、档案、跃地图、连登、抽奖摘要，
对齐 workbuddy.cn/profile/growth-center 页面只读展示。鉴权复用 codebuddy OAuth
access_token（Bearer），必须带浏览器请求头（Origin/Referer/User-Agent），否则
APISIX 网关返回 401。
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from .base import parse_json_body


class GrowthUnavailableError(RuntimeError):
    """成长中心端点不可用或鉴权被拒。"""


class WorkBuddyGrowthClient:
    """只读拉取成长中心任务、档案、跃地图与连登。"""

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
        """返回 {profile, tasks, heatmap, streak, lottery}；任一端点失败抛错。"""
        if not access_token:
            raise GrowthUnavailableError("access token unavailable")
        headers = self._headers(access_token)
        profile = await self._get("/v2/activity/growth/profile", headers)
        tasks = await self._get("/v2/activity/growth/tasks", headers)
        heatmap = await self._get("/activity/growth/heatmap", headers)
        streak = await self._get("/activity/growth/streak", headers)
        lottery = await self._get("/activity/growth/lottery/summary", headers)
        return {
            "profile": _profile(profile),
            "tasks": [_task(t) for t in _tasks(tasks)],
            "heatmap": _heatmap(heatmap),
            "streak": _streak(streak),
            "lottery": _lottery(lottery),
        }

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

    async def _post(self, path: str, headers: dict[str, str], body: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = await self._client.post(
                f"{self.base_url}{path}", headers=headers, json=body or {},
            )
        except httpx.HTTPError as error:
            raise GrowthUnavailableError(f"transport:{type(error).__name__}") from error
        resp_body = parse_json_body(response.text)
        if response.status_code == 401:
            raise GrowthUnavailableError("auth_rejected")
        if not 200 <= response.status_code < 300:
            raise GrowthUnavailableError(f"http:{response.status_code}")
        if not isinstance(resp_body, dict) or resp_body.get("code") not in (0, "0"):
            raise GrowthUnavailableError("upstream_error")
        data = resp_body.get("data")
        return data if isinstance(data, dict) else {}

    async def accept_tasks(self, access_token: str, task_codes: list[str]) -> dict[str, Any]:
        return await self._post(
            "/activity/growth/tasks/accept", self._headers(access_token),
            {"task_codes": task_codes},
        )

    async def claim_task(self, access_token: str, task_code: str) -> dict[str, Any]:
        return await self._post(
            f"/activity/growth/tasks/{task_code}/claim", self._headers(access_token),
        )

    async def lottery_draw(self, access_token: str) -> dict[str, Any]:
        token = f"draw-{uuid.uuid4()}"
        return await self._post(
            "/activity/growth/lottery/draw", self._headers(access_token),
            {"client_token": token},
        )

    async def travel_depart(self, access_token: str, location_id: int) -> dict[str, Any]:
        return await self._post(
            "/activity/growth/buddy/travel/depart", self._headers(access_token),
            {"location_id": location_id},
        )

    async def travel_claim(self, access_token: str) -> dict[str, Any]:
        return await self._post(
            "/activity/growth/buddy/travel/claim", self._headers(access_token),
        )

    async def redeem(self, access_token: str, tier: str) -> dict[str, Any]:
        token = f"redeem-{tier}-{uuid.uuid4()}"
        return await self._post(
            "/activity/growth/redeem", self._headers(access_token),
            {"tier": tier, "client_token": token},
        )

    async def buddy_open(self, access_token: str, count: int = 1) -> dict[str, Any]:
        return await self._post(
            "/activity/growth/buddy/open", self._headers(access_token),
            {"count": count},
        )

    async def makeup_use(self, access_token: str, target_date: str) -> dict[str, Any]:
        return await self._post(
            "/activity/growth/makeup-cards/use", self._headers(access_token),
            {"target_date": target_date},
        )

    async def travel_status(self, access_token: str) -> dict[str, Any]:
        return await self._get(
            "/activity/growth/buddy/travel/status", self._headers(access_token),
        )

    async def travel_config(self, access_token: str) -> dict[str, Any]:
        return await self._get(
            "/activity/growth/buddy/travel/config", self._headers(access_token),
        )

    async def redeem_summary(self, access_token: str) -> dict[str, Any]:
        return await self._get(
            "/activity/growth/redeem/summary", self._headers(access_token),
        )

    async def buddy_quota(self, access_token: str) -> dict[str, Any]:
        return await self._get(
            "/activity/growth/buddy/quota", self._headers(access_token),
        )


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
        "reward_claimed": item.get("reward_claimed"),
        "claimed": item.get("claimed"),
        "is_claimed": item.get("is_claimed"),
        "receive_status": item.get("receive_status"),
        "locked": item.get("locked"),
        "is_new": item.get("is_new"),
        "icon_url": item.get("icon_url"),
    }


def _heatmap(data: dict[str, Any]) -> dict[str, Any]:
    cells = data.get("cells")
    return {
        "cells": _heatmap_cells(cells),
        "today": _heatmap_today(data.get("today")),
        "range_start": _range_field(data.get("range"), "start_date"),
        "range_end": _range_field(data.get("range"), "end_date"),
    }


def _heatmap_cells(cells: Any) -> list[dict[str, Any]]:
    if not isinstance(cells, list):
        return []
    return [
        {"date": c.get("date"), "score": c.get("score"), "has_new_buddy": c.get("has_new_buddy")}
        for c in cells if isinstance(c, dict)
    ]


def _heatmap_today(today: Any) -> dict[str, Any] | None:
    if not isinstance(today, dict):
        return None
    return {
        "date": today.get("date"),
        "score": today.get("score"),
        "is_active": today.get("is_active"),
        "status_text": today.get("status_text"),
    }


def _range_field(rng: Any, key: str) -> str | None:
    return rng.get(key) if isinstance(rng, dict) else None


def _streak(data: dict[str, Any]) -> dict[str, Any]:
    streak = data.get("streak")
    makeup = data.get("makeup_cards")
    redemption = data.get("redemption_status")
    return {
        "days": streak.get("days") if isinstance(streak, dict) else None,
        "next_tier": streak.get("next_tier") if isinstance(streak, dict) else None,
        "next_tier_remaining": streak.get("next_tier_remaining") if isinstance(streak, dict) else None,
        "makeup_balance": makeup.get("balance") if isinstance(makeup, dict) else None,
        "makeup_max": makeup.get("max") if isinstance(makeup, dict) else None,
        "remaining_days": redemption.get("remaining_days") if isinstance(redemption, dict) else None,
        "timezone": data.get("timezone"),
    }


def _lottery(data: dict[str, Any]) -> dict[str, Any]:
    """抽奖摘要：可用次数、已抽次数。只读，不含抽奖动作。"""
    return {
        "available_chances": data.get("available_chances") or data.get("chances"),
        "total_draws": data.get("total_draws"),
        "summary": data.get("summary"),
    }
