"""Serializable Control Plane service state and operation models."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

ObservedState = Literal["STOPPED", "STARTING", "HEALTHY", "DEGRADED", "STOPPING", "FAILED"]
DesiredState = Literal["STOPPED", "RUNNING"]
OperationStatus = Literal["running", "succeeded", "failed", "cancelled"]


@dataclass(frozen=True, slots=True)
class WorkerIdentity:
    pid: int
    process_start_time: float
    process_group_id: int
    owner_instance_id: str
    internal_auth_version: int


@dataclass(slots=True)
class ServiceSnapshot:
    desired_state: DesiredState = "STOPPED"
    observed_state: ObservedState = "STOPPED"
    identity: WorkerIdentity | None = None
    started_at: float | None = None
    stopped_at: float | None = None
    last_health_at: float | None = None
    last_exit_code: int | None = None
    last_error: str | None = None
    in_flight: int = 0
    runtime_snapshot_version: int = 0


@dataclass(slots=True)
class SupervisorOperation:
    operation_id: str
    action: str
    status: OperationStatus = "running"
    error: str | None = None
    in_flight: int = 0
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
