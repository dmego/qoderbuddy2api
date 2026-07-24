"""Single-task scheduler and bounded catch-up contracts."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from qb2api.checkin.scheduler import CheckinScheduler
from qb2api.config import Settings


class _Service:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def run_batch(self, **values):
        self.calls.append(values)


def test_disabled_scheduler_does_not_create_task() -> None:
    scheduler = CheckinScheduler(_Service(), Settings(checkin_enabled=False))

    scheduler.start()

    assert scheduler._task is None  # noqa: SLF001
    assert scheduler.next_run_at is None


@pytest.mark.asyncio
async def test_start_is_idempotent_and_stop_clears_task() -> None:
    settings = Settings(
        checkin_enabled=True,
        checkin_catch_up=False,
        checkin_at="23:59",
    )
    scheduler = CheckinScheduler(_Service(), settings)

    scheduler.start()
    first = scheduler._task  # noqa: SLF001
    scheduler.start()

    assert scheduler._task is first  # noqa: SLF001
    await scheduler.stop()
    assert scheduler._task is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_invalid_reschedule_keeps_existing_timer_alive() -> None:
    scheduler = CheckinScheduler(
        _Service(),
        Settings(checkin_enabled=True, checkin_catch_up=False, checkin_at="23:59"),
    )
    scheduler.start()
    original = scheduler._task  # noqa: SLF001

    with pytest.raises(ValueError):
        await scheduler.reconfigure({"checkin_at": "25:00"})

    assert scheduler._task is original  # noqa: SLF001
    await scheduler.stop()


@pytest.mark.asyncio
async def test_catch_up_runs_once_inside_window(monkeypatch) -> None:
    timezone = "Asia/Shanghai"
    now = datetime.now(ZoneInfo(timezone))
    planned = now - timedelta(minutes=1)
    settings = Settings(
        checkin_enabled=True,
        checkin_catch_up=True,
        checkin_catch_up_window_hours=1,
        checkin_jitter_min_seconds=0,
        checkin_jitter_max_seconds=0,
        checkin_at=planned.strftime("%H:%M"),
        checkin_timezone=timezone,
    )
    service = _Service()
    scheduler = CheckinScheduler(service, settings)
    monkeypatch.setattr("qb2api.checkin.scheduler.jitter_seconds", lambda *_args: 0)

    await scheduler._maybe_catch_up()  # noqa: SLF001

    assert service.calls == [
        {"trigger": "catch_up", "skip_already_done": True}
    ]
