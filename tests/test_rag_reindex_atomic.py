"""Tests for Phase 8 M3: atomic versioned reindex (user feedback 3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mini_claw.config import AppConfig, RagConfig
from mini_claw.permissions.policy import PermissionPolicy
from mini_claw.rag.manager import RagManager
from mini_claw.storage.db import Database


@pytest.fixture
def config() -> RagConfig:
    cfg = RagConfig()
    cfg.enabled = True
    cfg.namespaces.context_enabled = True
    return cfg


@pytest.fixture
def storage(tmp_path: Path) -> Database:
    return Database(tmp_path / "reindex.db")


@pytest.fixture
def manager(storage, config) -> RagManager:
    return RagManager(storage, config, PermissionPolicy(AppConfig().permissions))


def _ctx(workspace_dir: Path, agent_id: str = "agent-a") -> dict:
    return {
        "agent_id": agent_id,
        "workspace_dir": str(workspace_dir),
        "sandbox_mode": "safe",
        "chat_id": "chat-1",
        "session_id": "sess-1",
        "channel_name": "cli",
    }


def _index(manager: RagManager, tmp_path: Path) -> str:
    p = tmp_path / "doc.md"
    p.write_text("# v1\nfirst version content\n", encoding="utf-8")
    item_id, error = manager.index_context(str(p), ctx=_ctx(tmp_path))
    assert error == ""
    return item_id


# ===================== Atomic version swap =====================


def test_reindex_bumps_active_version(manager: RagManager, tmp_path: Path):
    item_id = _index(manager, tmp_path)
    item_before = manager.store.get_item(item_id)
    assert item_before.active_version == 1

    # Modify source
    (tmp_path / "doc.md").write_text(
        "# v2\ncompletely new content here\n", encoding="utf-8"
    )

    success, error = manager.reindex_context(item_id, ctx=_ctx(tmp_path))
    assert success, error
    item_after = manager.store.get_item(item_id)
    assert item_after.active_version == 2
    assert item_after.content_hash != item_before.content_hash


def test_reindex_keeps_old_chunks_but_active_mapping_moves_on(manager: RagManager, tmp_path: Path):
    item_id = _index(manager, tmp_path)
    chunks_v1 = manager.store.get_chunks(item_id, version=1)
    assert chunks_v1

    # Modify source and reindex
    (tmp_path / "doc.md").write_text("# new\nnew body\n", encoding="utf-8")
    success, _ = manager.reindex_context(item_id, ctx=_ctx(tmp_path))
    assert success

    # Old version chunks are retained for audit/rollback, but active chunks
    # must come from the new active mapping/version.
    assert manager.store.get_chunks(item_id, version=1) != []
    # New version exists
    assert manager.store.get_chunks(item_id, version=2)
    assert all(c.version == 2 for c in manager.store.get_active_chunks(item_id))


def test_search_only_returns_active_version(
    manager: RagManager, storage: Database, tmp_path: Path
):
    """Searches must filter by active_version even if old version chunks linger."""
    item_id = _index(manager, tmp_path)

    # Reindex twice
    (tmp_path / "doc.md").write_text("# round 2\nintermediate\n", encoding="utf-8")
    manager.reindex_context(item_id, ctx=_ctx(tmp_path))

    (tmp_path / "doc.md").write_text(
        "# round 3\nfinal sparkly content\n", encoding="utf-8"
    )
    manager.reindex_context(item_id, ctx=_ctx(tmp_path))

    item = manager.store.get_item(item_id)
    assert item.active_version == 3

    # Search must hit ONLY active_version content
    results, _ = manager.search_context("sparkly", ctx=_ctx(tmp_path))
    if results:
        for r in results:
            # The chunk_id naming carries the version
            assert "v3" in r.chunk_id

    # Old keyword from v1 should NOT appear (old chunks are not active)
    results, _ = manager.search_context("first version", ctx=_ctx(tmp_path))
    assert results == []


def test_reindex_blocks_cross_agent(manager: RagManager, tmp_path: Path):
    item_id = _index(manager, tmp_path)
    success, error = manager.reindex_context(
        item_id, ctx=_ctx(tmp_path, agent_id="agent-b")
    )
    assert not success
    assert "another agent" in error.lower()


def test_reindex_missing_source_path_fails(manager: RagManager, tmp_path: Path):
    item_id = _index(manager, tmp_path)
    # Delete source file → reindex fails (cannot read)
    (tmp_path / "doc.md").unlink()
    success, error = manager.reindex_context(item_id, ctx=_ctx(tmp_path))
    assert not success


# ===================== Rebind =====================


def test_rebind_same_hash_succeeds(manager: RagManager, tmp_path: Path):
    item_id = _index(manager, tmp_path)
    # Move file: copy content to new path
    new_path = tmp_path / "moved.md"
    new_path.write_text(
        (tmp_path / "doc.md").read_text(encoding="utf-8"), encoding="utf-8"
    )
    success, message = manager.rebind_context(
        item_id, str(new_path), ctx=_ctx(tmp_path)
    )
    assert success
    assert "rebound" in message.lower()
    assert manager.store.get_item(item_id).source_path == str(new_path)


def test_rebind_different_hash_suggests_reindex(manager: RagManager, tmp_path: Path):
    item_id = _index(manager, tmp_path)
    new_path = tmp_path / "different.md"
    new_path.write_text("totally different content\n", encoding="utf-8")
    success, error = manager.rebind_context(
        item_id, str(new_path), ctx=_ctx(tmp_path)
    )
    assert not success
    assert "reindex" in error.lower()
