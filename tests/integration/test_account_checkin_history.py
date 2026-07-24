"""Account check-in history response contracts."""

from __future__ import annotations

import httpx
import pytest


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer admin-secret"}


@pytest.mark.asyncio
async def test_checkin_run_history_is_paginated_and_secret_safe(admin_context) -> None:
    app, repository, _vault, _registry = admin_context
    for run_id in ("run-earlier", "run-later"):
        await repository.create_checkin_run(
            run_id=run_id, local_date="2026-07-23", timezone="Asia/Shanghai", trigger="scheduler"
        )
        await repository.finish_checkin_run(run_id)
        await repository.upsert_checkin_attempt(
            run_id=run_id, provider="qoder", account_id="qd-main", outcome="CLAIMED",
            redacted_error="upstream-secret-must-not-appear",
        )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        response = await client.get(
            "/api/admin/checkin/runs?limit=1&status=finished&trigger=scheduled", headers=_headers()
        )
        await repository.create_checkin_run(
            run_id="run-newest", local_date="2026-07-23", timezone="Asia/Shanghai", trigger="scheduler"
        )
        await repository.finish_checkin_run("run-newest")
        next_page = await client.get(
            "/api/admin/checkin/runs", headers=_headers(),
            params={
                "limit": "1",
                "cursor": response.json()["next_cursor"],
                "status": "finished",
                "trigger": "scheduled",
            },
        )
        invalid_limit = await client.get("/api/admin/checkin/runs?limit=abc", headers=_headers())
        invalid_cursor = await client.get("/api/admin/checkin/runs?cursor=not-a-cursor", headers=_headers())
        detail = await client.get(
            f"/api/admin/checkin/runs/{response.json()['runs'][0]['run_id']}", headers=_headers()
        )

    assert response.status_code == 200
    history = response.json()
    assert history["limit"] == 1
    assert len(history["runs"]) == 1
    assert history["runs"][0]["run_id"] == "run-later"
    assert history["runs"][0]["attempt_count"] == 1
    assert history["next_cursor"]
    assert next_page.status_code == 200
    assert [item["run_id"] for item in next_page.json()["runs"]] == ["run-earlier"]
    assert invalid_limit.status_code == 400
    assert invalid_limit.json()["detail"] == "invalid_limit"
    assert invalid_cursor.status_code == 400
    assert invalid_cursor.json()["detail"] == "invalid_cursor"
    assert "upstream-secret-must-not-appear" not in response.text
    assert detail.status_code == 200
    assert detail.json()["attempts"][0]["outcome"] == "claimed"
    assert detail.json()["attempts"][0]["error_code"] == "checkin_failed"
    assert "upstream-secret-must-not-appear" not in detail.text
