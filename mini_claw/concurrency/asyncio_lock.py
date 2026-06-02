"""Asyncio-based lock backend for single-process deployments."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator


class AsyncioLockBackend:
    """Single-process lock backend using asyncio.Lock.

    This is the default backend for single-process deployments.
    Thread-safe but not multi-process safe.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def acquire(self, key: str, timeout: float = 30.0) -> AsyncIterator[None]:
        """Acquire an asyncio.Lock for the given key.

        Args:
            key: Lock identifier
            timeout: Maximum time to wait (default 30s)

        Yields:
            None when lock is acquired

        Raises:
            TimeoutError: If lock cannot be acquired within timeout
        """
        # Get or create lock for this key
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()

        lock = self._locks[key]

        try:
            # Use asyncio.wait_for for Python 3.10 compatibility
            await asyncio.wait_for(lock.acquire(), timeout=timeout)
        except asyncio.TimeoutError as e:
            raise TimeoutError(f"Failed to acquire lock '{key}' within {timeout}s") from e

        try:
            yield
        finally:
            lock.release()
