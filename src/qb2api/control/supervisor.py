"""Safe lifecycle supervisor for the independent Proxy Worker."""

from __future__ import annotations

import asyncio
import signal
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from qb2api.config import Settings

from .health import WorkerHealthChecker
from .operations import OperationStore
from .process import process_group, send_signal
from .service_models import ServiceSnapshot, SupervisorOperation, WorkerIdentity
from .worker_process import spawn_worker, wait_process, worker_command, worker_environment

ProcessFactory = Callable[[list[str], dict[str, str]], Any]
HealthChecker = Callable[[Any], Awaitable[bool]]
SignalSender = Callable[[Any, int, signal.Signals], None]
RuntimeReloader = Callable[[Any], Awaitable[int]]
StateWriter = Callable[[ServiceSnapshot], Awaitable[None]]
OperationWriter = Callable[[SupervisorOperation], Awaitable[None]]


class ServiceSupervisor:
    """Own one Worker process and never terminate an unverified process."""

    def __init__(
        self,
        settings: Settings,
        *,
        process_factory: ProcessFactory | None = None,
        health_checker: HealthChecker | None = None,
        signal_sender: SignalSender | None = None,
        state_writer: StateWriter | None = None,
        operation_writer: OperationWriter | None = None,
        runtime_reloader: RuntimeReloader | None = None,
        drain_timeout: float | None = None,
    ) -> None:
        self.settings = settings
        self._process_factory = process_factory or spawn_worker
        self._snapshot = ServiceSnapshot()
        self._health_checker = health_checker or WorkerHealthChecker(
            settings, lambda: self._snapshot.identity
        )
        self._signal_sender = signal_sender or send_signal
        self._state_writer = state_writer
        self._operation_writer = operation_writer
        self._runtime_reloader = runtime_reloader or self._default_reload
        self._drain_timeout = drain_timeout or float(settings.provider_drain_timeout_seconds)
        self._process: Any | None = None
        self._operations = OperationStore()
        self._lock = asyncio.Lock()
        self._auth_version = 0

    @property
    def snapshot(self) -> ServiceSnapshot:
        return self._snapshot

    def operation(self, operation_id: str) -> SupervisorOperation | None:
        return self._operations.get(operation_id)

    async def restore(self, values: dict[str, Any] | None) -> None:
        if not values:
            return
        self._snapshot.desired_state = "STOPPED"
        self._snapshot.observed_state = "STOPPED"
        self._snapshot.last_exit_code = values.get("last_exit_code")
        self._snapshot.last_error = values.get("last_error")
        await self._persist()

    async def start(self, *, idempotency_key: str | None = None) -> SupervisorOperation:
        async with self._lock:
            existing = self._operations.existing(idempotency_key)
            if existing:
                return existing
            operation = self._operations.begin("start", idempotency_key)
            try:
                await self._start_locked()
                await self._complete_operation(operation)
            except Exception as error:
                await self._reject_operation(operation, error)
            return operation

    async def stop(self, *, idempotency_key: str | None = None) -> SupervisorOperation:
        async with self._lock:
            existing = self._operations.existing(idempotency_key)
            if existing:
                return existing
            operation = self._operations.begin("stop", idempotency_key)
            try:
                await self._stop_locked()
                await self._complete_operation(operation)
            except Exception as error:
                await self._reject_operation(operation, error)
            return operation

    async def restart(self, *, idempotency_key: str | None = None) -> SupervisorOperation:
        async with self._lock:
            existing = self._operations.existing(idempotency_key)
            if existing:
                return existing
            operation = self._operations.begin("restart", idempotency_key)
            try:
                await self._stop_locked()
                await self._start_locked()
                await self._complete_operation(operation)
            except Exception as error:
                await self._reject_operation(operation, error)
            return operation

    async def reload(self, *, idempotency_key: str | None = None) -> SupervisorOperation:
        async with self._lock:
            existing = self._operations.existing(idempotency_key)
            if existing:
                return existing
            operation = self._operations.begin("reload", idempotency_key)
            try:
                if not self._is_alive():
                    await self._start_locked()
                else:
                    self._snapshot.runtime_snapshot_version = await self._runtime_reloader(self._process)
                    self._snapshot.last_health_at = time.time()
                await self._complete_operation(operation)
            except Exception as error:
                await self._reject_operation(operation, error)
            return operation

    async def begin_request(self) -> None:
        async with self._lock:
            self._snapshot.in_flight += 1

    async def end_request(self) -> None:
        async with self._lock:
            self._snapshot.in_flight = max(0, self._snapshot.in_flight - 1)

    async def reconcile(self) -> ServiceSnapshot:
        async with self._lock:
            if self._process is not None and not self._is_alive():
                self._snapshot.observed_state = "FAILED"
                self._snapshot.last_exit_code = self._process.poll()
                self._snapshot.last_error = "worker exited unexpectedly"
                self._process = None
                self._snapshot.identity = None
                await self._persist()
            return self._snapshot

    async def _start_locked(self) -> None:
        if self._is_alive() and self._snapshot.observed_state == "HEALTHY":
            self._snapshot.desired_state = "RUNNING"
            return
        self._snapshot.desired_state = "RUNNING"
        self._snapshot.observed_state = "STARTING"
        self._auth_version += 1
        owner = str(uuid.uuid4())
        process = await asyncio.to_thread(self._process_factory, self._command(), self._worker_env(owner))
        self._process = process
        self._snapshot.identity = self._capture_identity(process, owner)
        self._snapshot.started_at = time.time()
        if not await self._wait_ready(process):
            self._snapshot.observed_state = "FAILED"
            self._snapshot.last_error = "worker readiness timeout"
            self._terminate_verified()
            raise RuntimeError("proxy worker did not become ready")
        self._snapshot.observed_state = "HEALTHY"
        self._snapshot.last_health_at = time.time()
        self._snapshot.last_error = None

    async def _stop_locked(self) -> None:
        self._snapshot.desired_state = "STOPPED"
        if not self._is_alive():
            self._snapshot.observed_state = "STOPPED"
            self._process = None
            self._snapshot.identity = None
            return
        self._snapshot.observed_state = "STOPPING"
        deadline = time.monotonic() + self._drain_timeout
        while self._snapshot.in_flight and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        self._terminate_verified()
        stopped = await asyncio.to_thread(wait_process, self._process, deadline)
        if not stopped:
            self._kill_verified()
            await asyncio.to_thread(wait_process, self._process, time.monotonic() + 1)
        self._snapshot.observed_state = "STOPPED"
        self._snapshot.stopped_at = time.time()
        self._snapshot.last_exit_code = self._process.poll() if self._process else None
        self._process = None
        self._snapshot.identity = None

    async def _persist(self) -> None:
        if self._state_writer is not None:
            await self._state_writer(self._snapshot)

    async def _persist_operation(self, operation: SupervisorOperation) -> None:
        operation.in_flight = self._snapshot.in_flight
        if self._operation_writer is not None:
            await self._operation_writer(operation)

    async def _complete_operation(self, operation: SupervisorOperation) -> None:
        self._operations.succeed(operation)
        await self._persist()
        await self._persist_operation(operation)

    async def _reject_operation(self, operation: SupervisorOperation, error: Exception) -> None:
        self._operations.fail(operation, RuntimeError(type(error).__name__))
        await self._persist()
        await self._persist_operation(operation)

    async def _wait_ready(self, process: Any) -> bool:
        deadline = time.monotonic() + float(self.settings.worker_start_timeout_seconds)
        while time.monotonic() < deadline:
            if not self._is_process_alive(process):
                return False
            if await self._health_checker(process):
                return True
            await asyncio.sleep(float(self.settings.worker_health_interval_seconds))
        return False

    def _terminate_verified(self) -> None:
        if not self._process or not self._identity_matches(self._process):
            raise RuntimeError("refusing to terminate unverified worker process")
        process_group_id = self._snapshot.identity.process_group_id if self._snapshot.identity else 0
        self._signal_sender(self._process, process_group_id, signal.SIGTERM)

    def _kill_verified(self) -> None:
        if not self._process or not self._identity_matches(self._process):
            raise RuntimeError("refusing to kill unverified worker process")
        process_group_id = self._snapshot.identity.process_group_id if self._snapshot.identity else 0
        self._signal_sender(self._process, process_group_id, signal.SIGKILL)

    def _capture_identity(self, process: Any, owner: str) -> WorkerIdentity:
        pid = int(process.pid)
        group_id = int(getattr(process, "process_group_id", process_group(pid)))
        started = float(getattr(process, "start_time", time.time()))
        self._tag_process(process, owner)
        return WorkerIdentity(pid, started, group_id, owner, self._auth_version)

    def _identity_matches(self, process: Any) -> bool:
        identity = self._snapshot.identity
        if identity is None or process is None:
            return False
        return (
            int(getattr(process, "pid", -1)) == identity.pid
            and float(getattr(process, "start_time", identity.process_start_time)) == identity.process_start_time
            and int(getattr(process, "process_group_id", identity.process_group_id)) == identity.process_group_id
            and getattr(process, "owner_instance_id", None) == identity.owner_instance_id
            and getattr(process, "internal_auth_version", None) == identity.internal_auth_version
        )

    def _tag_process(self, process: Any, owner: str) -> None:
        process.owner_instance_id = owner
        process.internal_auth_version = self._auth_version

    def _is_alive(self) -> bool:
        return self._is_process_alive(self._process)

    @staticmethod
    def _is_process_alive(process: Any) -> bool:
        return process is not None and process.poll() is None

    def _command(self) -> list[str]:
        return worker_command(self.settings)

    def _worker_env(self, owner: str) -> dict[str, str]:
        return worker_environment(self.settings, owner, self._auth_version)

    async def _default_reload(self, process: Any) -> int:
        checker = self._health_checker
        reloader = getattr(checker, "reload", None)
        if reloader is None:
            raise RuntimeError("worker runtime reloader is not configured")
        return await reloader(process)
