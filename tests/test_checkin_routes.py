from qb2api.admin.checkin_routes import _attempt_view


def test_attempt_view_hides_reward_metadata_for_failed_attempt() -> None:
    view = _attempt_view(
        {
            "provider": "qoder",
            "account_id": "qd-1",
            "outcome": "FAILED",
            "business_code": "qoder_checkin_disabled",
            "reward_credits": 100,
            "reward_expires_at": "1970-01-01T00:00:00+00:00",
            "quota_delta": {"packages": [{"name": "签到奖励", "delta": 100}]},
            "quota_change_status": "claimed_balance_increased",
        }
    )

    assert view["outcome"] == "failed"
    assert view["reward_credits"] is None
    assert view["reward_expires_at"] is None
    assert view["quota_delta"] is None
    assert view["quota_change_status"] is None
