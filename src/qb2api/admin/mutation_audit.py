"""Reliable audit helpers for admin mutations and derived refreshes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import HTTPException


async def add_audit(
    repository: Any,
    *,
    action: str,
    resource_type: str,
    resource_id: str | None,
    result: str = "succeeded",
    metadata: dict[str, Any] | None = None,
) -> None:
    await repository.add_audit_event(
        actor_type="admin",
        actor_id=None,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        result=result,
        metadata=metadata,
    )


async def refresh_after_mutation(
    state: Any,
    *,
    mutation_action: str,
    resource_type: str,
    resource_id: str,
) -> None:
    metadata = {"mutation_action": mutation_action}
    try:
        await state.refresh_provider_pools()
    except asyncio.CancelledError:
        await add_audit(
            state.account_repo,
            action="provider_pool.refresh",
            resource_type=resource_type,
            resource_id=resource_id,
            result="cancelled",
            metadata=metadata | {"error_code": "provider_pool_refresh_cancelled"},
        )
        raise
    except Exception as error:
        await add_audit(
            state.account_repo,
            action="provider_pool.refresh",
            resource_type=resource_type,
            resource_id=resource_id,
            result="failed",
            metadata=metadata | {"error_code": "provider_pool_refresh_failed"},
        )
        raise HTTPException(status_code=503, detail="provider_pool_refresh_failed") from error


@asynccontextmanager
async def audit_operation(
    repository: Any,
    *,
    action: str,
    resource_type: str,
    resource_id: str | None,
    failure_code: str,
) -> AsyncIterator[None]:
    result = "succeeded"
    metadata = None
    try:
        yield
    except asyncio.CancelledError:
        result = "cancelled"
        metadata = {"error_code": f"{failure_code}_cancelled"}
        raise
    except Exception:
        result = "failed"
        metadata = {"error_code": failure_code}
        raise
    finally:
        await add_audit(
            repository, action=action, resource_type=resource_type,
            resource_id=resource_id, result=result, metadata=metadata,
        )
