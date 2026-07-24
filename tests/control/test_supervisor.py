"""Supervisor lifecycle and process-identity tests."""

from __future__ import annotations

import asyncio
import signal

import pytest

from qb2api.config import Settings
from qb2api.control.supervisor import ServiceSupervisor


def send_fake_signal(process: FakeProcess, group_id: int, requested: signal.Signals) -> None:
    if requested == signal.SIGKILL:
        process.kill()
    else:
        process.terminate()


class FakeProcess:
    _next_pid = 1000

    def __init__(self) -> None:
        self.pid = FakeProcess._next_pid
        FakeProcess._next_pid += 1
        self.start_time = 1.0
        self.process_group_id = self.pid
        self._returncode: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self._returncode = 0

    def kill(self) -> None:
        self.kill_calls += 1
        self._returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self._returncode is None:
            raise TimeoutError
        return self._returncode


@pytest.mark.asyncio
async def test_start_is_idempotent_and_stop_verifies_identity() -> None:
    process = FakeProcess()
    supervisor = ServiceSupervisor(
        Settings(worker_start_timeout_seconds=1),
        process_factory=lambda command, env: process,
        health_checker=lambda current: asyncio.sleep(0, result=True),
        signal_sender=send_fake_signal,
        drain_timeout=0.1,
    )

    started = await supervisor.start(idempotency_key="boot")
    repeated = await supervisor.start(idempotency_key="boot")
    assert started.status == "succeeded"
    assert repeated.operation_id == started.operation_id
    assert supervisor.snapshot.observed_state == "HEALTHY"

    stopped = await supervisor.stop()
    assert stopped.status == "succeeded"
    assert process.terminate_calls == 1
    assert supervisor.snapshot.observed_state == "STOPPED"


@pytest.mark.asyncio
async def test_mismatched_identity_never_sends_signal() -> None:
    process = FakeProcess()
    supervisor = ServiceSupervisor(
        Settings(worker_start_timeout_seconds=1),
        process_factory=lambda command, env: process,
        health_checker=lambda current: asyncio.sleep(0, result=True),
        signal_sender=send_fake_signal,
        drain_timeout=0.01,
    )
    await supervisor.start()
    assert supervisor.snapshot.identity is not None
    process.owner_instance_id = "different-owner"

    stopped = await supervisor.stop()

    assert stopped.status == "failed"
    assert process.terminate_calls == 0
    assert process.kill_calls == 0


@pytest.mark.asyncio
async def test_stop_waits_for_in_flight_until_drain_deadline() -> None:
    process = FakeProcess()
    supervisor = ServiceSupervisor(
        Settings(worker_start_timeout_seconds=1),
        process_factory=lambda command, env: process,
        health_checker=lambda current: asyncio.sleep(0, result=True),
        signal_sender=send_fake_signal,
        drain_timeout=0.01,
    )
    await supervisor.start()
    await supervisor.begin_request()

    stopped = await supervisor.stop()

    assert stopped.status == "succeeded"
    assert process.terminate_calls == 1
    assert supervisor.snapshot.in_flight == 1


@pytest.mark.asyncio
async def test_worker_environment_uses_control_loopback_without_admin_secrets() -> None:
    process = FakeProcess()
    captured: dict[str, str] = {}

    def spawn(command: list[str], environment: dict[str, str]) -> FakeProcess:
        captured.update(environment)
        return process

    settings = Settings(
        control_host="0.0.0.0",
        control_port=8123,
        worker_host="127.0.0.1",
        worker_port=8124,
        worker_internal_token="internal-secret",
        worker_start_timeout_seconds=1,
    )
    supervisor = ServiceSupervisor(
        settings,
        process_factory=spawn,
        health_checker=lambda current: asyncio.sleep(0, result=True),
        signal_sender=send_fake_signal,
    )

    started = await supervisor.start()

    assert started.status == "succeeded"
    assert captured["QB2API_CONTROL_HOST"] == "127.0.0.1"
    assert captured["QB2API_CONTROL_PORT"] == "8123"
    assert captured["QB2API_WORKER_PORT"] == "8124"
    assert captured["QB2API_CREDENTIAL_KEY"] == ""
    assert captured["QB2API_ADMIN_KEY"] == ""


@pytest.mark.asyncio
async def test_reload_applies_worker_runtime_snapshot_version() -> None:
    process = FakeProcess()
    reload_calls: list[int] = []

    async def reload_runtime(current: FakeProcess) -> int:
        reload_calls.append(current.pid)
        return 12

    supervisor = ServiceSupervisor(
        Settings(worker_start_timeout_seconds=1),
        process_factory=lambda command, env: process,
        health_checker=lambda current: asyncio.sleep(0, result=True),
        runtime_reloader=reload_runtime,
        signal_sender=send_fake_signal,
    )
    await supervisor.start()

    reloaded = await supervisor.reload()

    assert reloaded.status == "succeeded"
    assert reload_calls == [process.pid]
    assert supervisor.snapshot.runtime_snapshot_version == 12
