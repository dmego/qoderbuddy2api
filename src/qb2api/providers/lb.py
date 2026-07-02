"""Load-balancing wrapper that distributes requests across multiple provider instances.

ponytail: round-robin with failover, no health-check goroutines.
Add health-checks + circuit-breaker if upstream instability is observed.
"""

import asyncio
import logging
import random
from typing import AsyncIterator

from .base import Provider
from ..openai import ChatCompletionRequest

logger = logging.getLogger("qb2api")


class LoadBalancedProvider(Provider):
    """Wraps N provider instances of the same type, round-robins requests.

    On failure, marks instance as tainted for 30s and retries next in pool.
    """

    name: str  # set in __init__

    _COOLDOWN = 30  # seconds before retrying a failed instance

    def __init__(self, instances: list[Provider]):
        if not instances:
            raise ValueError("Need at least one provider instance")
        self.name = instances[0].name
        self._instances = instances
        self._idx = 0
        self._lock = asyncio.Lock()
        self._failed: dict[int, float] = {}  # idx → cooldown_until

    @property
    def instance_count(self) -> int:
        return len(self._instances)

    def _pick(self) -> int:
        """Pick next healthy instance index."""
        now = asyncio.get_event_loop().time()
        # Try round-robin until we find a live one
        for _ in range(len(self._instances)):
            idx = self._idx
            self._idx = (self._idx + 1) % len(self._instances)
            cooldown = self._failed.get(idx)
            if cooldown and cooldown > now:
                continue
            return idx
        # All failed — pick random, reset cooldowns
        logger.warning(f"{self.name}: all instances failed, resetting cooldowns")
        self._failed.clear()
        return random.randrange(len(self._instances))

    async def _do(self, idx: int, request: ChatCompletionRequest, stream: bool):
        inst = self._instances[idx]
        if stream:
            return inst.stream(request)
        else:
            return await inst.complete(request)

    async def complete(self, request: ChatCompletionRequest) -> dict:
        for attempt in range(len(self._instances)):
            async with self._lock:
                idx = self._pick()
            try:
                return await self._instances[idx].complete(request)
            except Exception as e:
                logger.warning(f"{self.name}[{idx}]: failed — {e}")
                self._failed[idx] = asyncio.get_event_loop().time() + self._COOLDOWN
                if attempt == len(self._instances) - 1:
                    raise
        raise RuntimeError("unreachable")

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[bytes]:
        for attempt in range(len(self._instances)):
            async with self._lock:
                idx = self._pick()
            try:
                # Stream is a generator — consume it here
                async for chunk in self._instances[idx].stream(request):
                    yield chunk
                return
            except Exception as e:
                logger.warning(f"{self.name}[{idx}]: stream failed — {e}")
                self._failed[idx] = asyncio.get_event_loop().time() + self._COOLDOWN
                if attempt == len(self._instances) - 1:
                    raise

    async def close(self) -> None:
        for inst in self._instances:
            await inst.close()
