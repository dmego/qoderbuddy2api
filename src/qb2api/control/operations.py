"""In-memory operation registry with idempotency support."""

from __future__ import annotations

import time
import uuid

from .service_models import SupervisorOperation


class OperationStore:
    def __init__(self) -> None:
        self._operations: dict[str, SupervisorOperation] = {}
        self._idempotency: dict[str, str] = {}

    def get(self, operation_id: str) -> SupervisorOperation | None:
        return self._operations.get(operation_id)

    def existing(self, key: str | None) -> SupervisorOperation | None:
        operation_id = self._idempotency.get(key or "")
        return self.get(operation_id) if operation_id else None

    def begin(self, action: str, key: str | None) -> SupervisorOperation:
        operation = SupervisorOperation(str(uuid.uuid4()), action)
        self._operations[operation.operation_id] = operation
        if key:
            self._idempotency[key] = operation.operation_id
        return operation

    @staticmethod
    def succeed(operation: SupervisorOperation) -> None:
        operation.status = "succeeded"
        operation.finished_at = time.time()

    @staticmethod
    def fail(operation: SupervisorOperation, error_code: str) -> None:
        operation.status = "failed"
        operation.error = error_code
        operation.finished_at = time.time()
