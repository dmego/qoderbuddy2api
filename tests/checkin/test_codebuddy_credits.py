"""CodeBuddy/WorkBuddy credit balance client contracts."""

from __future__ import annotations

import pytest

from qb2api.checkin.codebuddy_credits import (
    CodeBuddyCreditsClient,
    CodeBuddyCreditsUnavailableError,
    normalize_credits,
)


def _body():
    return {
        "code": 0,
        "msg": "OK",
        "data": {"Response": {"Data": {"TotalCount": 2, "Accounts": [
            {
                "AccountId": 1,
                "Uin": "secret-uin",
                "DealName": "secret-deal",
                "CapacityUnit": "credits",
                "CapacityRemain": 100,
                "CapacityUsed": 0,
                "CapacitySize": 500,
                "CycleCapacityRemain": 90,
                "CycleCapacitySize": 500,
                "Status": 0,
                "Threshold": 10,
                "ExpiredTime": "",
                "CycleEndTime": "2026-08-31 23:59:59",
                "PackageName": "CodeBuddy个人体验版",
                "AccountAttributes": [{"Key": "payerUin", "Value": "secret"}],
            },
            {
                "AccountId": 2,
                "Uin": "secret-uin",
                "DealName": "secret-deal",
                "CapacityUnit": "credits",
                "CapacityRemain": 0,
                "CapacityUsed": 500,
                "CapacitySize": 500,
                "CycleCapacityRemain": 0,
                "CycleCapacitySize": 500,
                "Status": 0,
                "Threshold": 0,
                "ExpiredTime": "1784517058000",
                "CycleEndTime": "2026-08-20 11:10:58",
                "PackageName": "CodeBuddy个人版国内运营裂变包",
                "AccountAttributes": [],
            },
        ]}}},
    }


def test_normalize_credits_keeps_only_business_fields():
    value = normalize_credits(_body())
    assert value == {
        "unit": "credits",
        "total_remaining": 100,
        "total_used": 500,
        "total_capacity": 1000,
        "cycle_remaining": 90,
        "cycle_capacity": 1000,
        "package_count": 2,
        "depleted_packages": 1,
        "lowest_remaining": 0,
        "expires_at": "2026-07-20T03:10:58+00:00",
        "packages": [
            {
                "name": "CodeBuddy个人体验版", "remaining": 100, "used": 0, "total": 500,
                "unit": "credits", "expires_at": "2026-08-31T23:59:59+00:00",
            },
            {
                "name": "CodeBuddy个人版国内运营裂变包", "remaining": 0, "used": 500, "total": 500,
                "unit": "credits", "expires_at": "2026-07-20T03:10:58+00:00",
            },
        ],
    }
    dumped = str(value)
    assert "secret" not in dumped


def test_normalize_credits_rejects_missing_data():
    assert normalize_credits(None) == {}
    assert normalize_credits({"code": 1, "msg": "boom"}) == {}


def test_normalize_credits_accepts_space_separated_expiry():
    body = _body()
    account = body["data"]["Response"]["Data"]["Accounts"][1]
    account["ExpiredTime"] = "2026-07-22 22:34:16"
    value = normalize_credits(body)
    assert value["expires_at"] == "2026-07-22T22:34:16+00:00"


@pytest.mark.asyncio
async def test_client_rejects_empty_token():
    client = CodeBuddyCreditsClient()
    try:
        with pytest.raises(CodeBuddyCreditsUnavailableError, match="access credential"):
            await client.fetch("")
    finally:
        await client.aclose()
