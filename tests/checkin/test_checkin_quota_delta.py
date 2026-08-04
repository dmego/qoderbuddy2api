from qb2api.checkin.models import CheckInOutcome, CheckInResult
from qb2api.checkin.service_execution import _quota_change_status, _quota_delta


def test_quota_delta_compares_each_package() -> None:
    before = {"packages": [{"name": "user_quota", "remaining": 0}, {"name": "add_on_quota", "remaining": 298}]}
    after = {"packages": [{"name": "user_quota", "remaining": 100}, {"name": "add_on_quota", "remaining": 298}]}
    assert _quota_delta(before, after) == {
        "packages": [
            {"name": "add_on_quota", "delta": 0},
            {"name": "user_quota", "delta": 100},
        ]
    }


def test_claimed_balance_status_distinguishes_increase_and_unchanged() -> None:
    result = CheckInResult(
        outcome=CheckInOutcome.CLAIMED,
        provider="qoder",
        quota_delta={"packages": [{"name": "user", "delta": 100}]},
    )
    assert _quota_change_status(result) == "claimed_balance_increased"
    result.quota_delta = {"packages": [{"name": "user", "delta": 0}]}
    assert _quota_change_status(result) == "claimed_balance_unchanged"
