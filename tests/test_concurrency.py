"""Tests for concurrency primitives and multi-process safety."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mini_claw.concurrency import AsyncioLockBackend
from mini_claw.concurrency.file_lock import FileLockBackend


@pytest.mark.asyncio
async def test_asyncio_lock_backend_basic():
    """AsyncioLockBackend acquires and releases locks."""
    backend = AsyncioLockBackend()
    key = "test_lock"

    async with backend.acquire(key, timeout=1.0):
        # Lock acquired
        pass
    # Lock released


@pytest.mark.asyncio
async def test_asyncio_lock_backend_timeout():
    """AsyncioLockBackend times out if lock is held."""
    backend = AsyncioLockBackend()
    key = "test_lock"

    async def hold_lock_for_2s():
        async with backend.acquire(key, timeout=5.0):
            await asyncio.sleep(2.0)

    # Start a task that holds the lock for 2 seconds
    task = asyncio.create_task(hold_lock_for_2s())
    await asyncio.sleep(0.1)  # Let the task acquire the lock

    # Try to acquire with 0.5s timeout - should fail
    with pytest.raises(TimeoutError, match="Failed to acquire lock"):
        async with backend.acquire(key, timeout=0.5):
            pass

    # Wait for the first task to finish
    await task


@pytest.mark.asyncio
async def test_asyncio_lock_backend_concurrent_access():
    """AsyncioLockBackend serializes concurrent access."""
    backend = AsyncioLockBackend()
    key = "shared_resource"
    results = []

    async def append_with_delay(value: str):
        async with backend.acquire(key, timeout=5.0):
            results.append(f"{value}_start")
            await asyncio.sleep(0.1)
            results.append(f"{value}_end")

    # Run 3 concurrent tasks
    await asyncio.gather(
        append_with_delay("A"),
        append_with_delay("B"),
        append_with_delay("C"),
    )

    # Each task's start/end should be consecutive (not interleaved)
    assert len(results) == 6
    # Check no interleaving (e.g., A_start, B_start is bad)
    for i in range(0, len(results), 2):
        assert results[i].endswith("_start")
        assert results[i + 1].endswith("_end")
        assert results[i][0] == results[i + 1][0]  # Same task


@pytest.mark.asyncio
async def test_file_lock_backend_basic(tmp_path: Path):
    """FileLockBackend acquires and releases file locks."""
    backend = FileLockBackend(lock_dir=tmp_path)
    key = "test_file_lock"

    async with backend.acquire(key, timeout=1.0):
        # Lock acquired
        lock_file = tmp_path / f"{key}.lock"
        assert lock_file.exists()
    # Lock released


@pytest.mark.asyncio
async def test_file_lock_backend_timeout(tmp_path: Path):
    """FileLockBackend times out if file lock is held."""
    backend = FileLockBackend(lock_dir=tmp_path)
    key = "test_file_lock"

    async def hold_lock_for_2s():
        async with backend.acquire(key, timeout=5.0):
            await asyncio.sleep(2.0)

    # Start a task that holds the lock for 2 seconds
    task = asyncio.create_task(hold_lock_for_2s())
    await asyncio.sleep(0.2)  # Let the task acquire the lock

    # Try to acquire with 0.5s timeout - should fail
    with pytest.raises(TimeoutError, match="Failed to acquire file lock"):
        async with backend.acquire(key, timeout=0.5):
            pass

    # Wait for the first task to finish
    await task


@pytest.mark.asyncio
async def test_file_lock_sanitizes_key(tmp_path: Path):
    """FileLockBackend sanitizes keys for filesystem."""
    backend = FileLockBackend(lock_dir=tmp_path)
    key = "workspace:agent_id:path/to/dir"

    async with backend.acquire(key, timeout=1.0):
        # Key should be sanitized (no colons or slashes)
        lock_files = list(tmp_path.glob("*.lock"))
        assert len(lock_files) == 1
        assert ":" not in lock_files[0].name
        assert "/" not in lock_files[0].name
