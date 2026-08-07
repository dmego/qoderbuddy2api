"""Tests for the Qoder model catalog sync scheduler."""

from __future__ import annotations

import asyncio

import pytest

from qb2api.config import Settings
from qb2api.control.model_sync_scheduler import ModelSyncScheduler


class FakeReport:
    def __init__(self, added: int = 0, updated: int = 0, disabled: int = 0) -> None:
        self.added = added
        self.updated = updated
        self.disabled = disabled


def _scheduler(settings: Settings, refresh_callback=None) -> ModelSyncScheduler:
    return ModelSyncScheduler(
        settings=settings,
        repo=object(),
        registry=object(),
        resolver=object(),
        refresh_callback=refresh_callback,
    )


@pytest.mark.asyncio
async def test_sync_once_triggers_refresh_on_change(monkeypatch):
    async def fake_sync(repo, registry, resolver):
        return FakeReport(added=1)

    monkeypatch.setattr(
        "qb2api.control.model_sync_scheduler.sync_qoder_models", fake_sync
    )
    refreshed: list[int] = []

    async def refresh() -> None:
        refreshed.append(1)

    scheduler = _scheduler(Settings(), refresh_callback=refresh)
    changed = await scheduler.sync_once()

    assert changed is True
    assert refreshed == [1]


@pytest.mark.asyncio
async def test_sync_once_skips_refresh_without_change(monkeypatch):
    async def fake_sync(repo, registry, resolver):
        return FakeReport(added=1, updated=1, disabled=1)

    monkeypatch.setattr(
        "qb2api.control.model_sync_scheduler.sync_qoder_models", fake_sync
    )
    refreshed: list[int] = []

    async def refresh() -> None:
        refreshed.append(1)

    scheduler = _scheduler(Settings(), refresh_callback=refresh)
    changed = await scheduler.sync_once()
    assert changed is True
    assert refreshed == [1]

    async def noop_sync(repo, registry, resolver):
        return FakeReport()

    monkeypatch.setattr(
        "qb2api.control.model_sync_scheduler.sync_qoder_models", noop_sync
    )
    changed = await scheduler.sync_once()
    assert changed is False
    assert refreshed == [1]


@pytest.mark.asyncio
async def test_loop_syncs_at_startup_and_survives_failure(monkeypatch):
    sync_calls: list[int] = []

    async def flaky_sync(repo, registry, resolver):
        sync_calls.append(1)
        if len(sync_calls) == 1:
            raise RuntimeError("upstream unavailable")
        return FakeReport()

    monkeypatch.setattr(
        "qb2api.control.model_sync_scheduler.sync_qoder_models", flaky_sync
    )
    scheduler = _scheduler(
        Settings(model_sync_interval_seconds=600),
        refresh_callback=None,
    )
    scheduler.start()
    await asyncio.sleep(0.05)

    assert scheduler._task is not None and not scheduler._task.done()
    assert sync_calls == [1]
    await scheduler.stop()


@pytest.mark.asyncio
async def test_start_noop_when_disabled():
    scheduler = _scheduler(Settings(model_sync_enabled=False))
    scheduler.start()
    assert scheduler._task is None
    await scheduler.stop()
