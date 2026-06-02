"""Lock backend abstraction for single-process and multi-process deployments."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Protocol


class LockBackend(Protocol):
    """Protocol for lock backends.

    Implementations:
    - AsyncioLockBackend: Single-process asyncio.Lock (default)
    - FileLockBackend: Multi-process file-based lock (fcntl/msvcrt)
    - SQLiteLockBackend: Multi-process SQLite advisory lock (cross-platform)
    """

    @asynccontextmanager
    async def acquire(self, key: str, timeout: float = 30.0) -> AsyncIterator[None]:
        """Acquire a lock for the given key.

        Args:
            key: Lock identifier (e.g., "workspace:agent_id:workspace_dir")
            timeout: Maximum time to wait for lock acquisition in seconds

        Yields:
            None when lock is acquired

        Raises:
            TimeoutError: If lock cannot be acquired within timeout
        """
        ...
