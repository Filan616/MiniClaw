"""File-based lock backend for multi-process deployments."""

from __future__ import annotations

import asyncio
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator


class FileLockBackend:
    """Multi-process lock backend using file locks.

    Uses fcntl on Unix and msvcrt on Windows for file locking.
    Suitable for multi-process deployments on the same machine.
    """

    def __init__(self, lock_dir: Path | str = "./data/locks") -> None:
        """Initialize file lock backend.

        Args:
            lock_dir: Directory to store lock files (default: ./data/locks)
        """
        self.lock_dir = Path(lock_dir)
        self.lock_dir.mkdir(parents=True, exist_ok=True)

    def _get_lock_path(self, key: str) -> Path:
        """Get lock file path for a key."""
        # Sanitize key for filesystem
        safe_key = key.replace("/", "_").replace(":", "_").replace(" ", "_")
        return self.lock_dir / f"{safe_key}.lock"

    @asynccontextmanager
    async def acquire(self, key: str, timeout: float = 30.0) -> AsyncIterator[None]:
        """Acquire a file lock for the given key.

        Args:
            key: Lock identifier
            timeout: Maximum time to wait (default 30s)

        Yields:
            None when lock is acquired

        Raises:
            TimeoutError: If lock cannot be acquired within timeout
        """
        lock_path = self._get_lock_path(key)
        lock_file = None

        try:
            # Open lock file
            lock_file = open(lock_path, "w")

            # Try to acquire lock with timeout
            start_time = time.monotonic()
            while True:
                try:
                    if sys.platform == "win32":
                        # Windows: use msvcrt
                        import msvcrt
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        # Unix: use fcntl
                        import fcntl
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

                    # Lock acquired
                    break
                except (IOError, OSError):
                    # Lock held by another process
                    if time.monotonic() - start_time > timeout:
                        raise TimeoutError(
                            f"Failed to acquire file lock '{key}' within {timeout}s"
                        )
                    # Wait a bit and retry
                    await asyncio.sleep(0.1)

            yield

        finally:
            if lock_file:
                try:
                    if sys.platform == "win32":
                        import msvcrt
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except (IOError, OSError):
                    pass
                lock_file.close()
