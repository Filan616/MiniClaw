"""Tests for Phase 8 M3: RAG lifecycle (state transitions, pinned protection, stale/orphan)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mini_claw.config import AppConfig, RagConfig
from mini_claw.permissions.policy import PermissionPolicy
from mini_claw.rag.lifecycle import RagLifecycle
from mini_claw.rag.manager import RagManager
from mini_claw.rag.store import RagStore
from mini_claw.storage.db import Database


@pytest.fixture
def config() -> RagConfig:
    cfg = RagConfig()
    cfg.enabled = True
    cfg.namespaces.context_enabled = True
    return cfg


@pytest.fixture
def storage(tmp_path: Path) -> Database:
    return Database(tmp_path / "lifecycle.db")


@pytest.fixture
def policy() -> PermissionPolicy:
    return PermissionPolicy(AppConfig().permissions)


@pytest.fixture
def manager(storage, config, policy) -> RagManager:
    return RagManager(storage, config, policy)


@pytest.fixture
def lifecycle(storage, config) -> RagLifecycle:
    return RagLifecycle(storage, config)


def _ctx(workspace_dir: Path, agent_id: str = "agent-a") -> dict:
    return {
        "agent_id": agent_id,
        "workspace_dir": str(workspace_dir),
        "sandbox_mode": "safe",
        "chat_id": "chat-1",
        "session_id": "sess-1",
        "channel_name": "cli",
    }


def _index_doc(manager: RagManager, tmp_path: Path, name: str = "doc.md") -> str:
    p = tmp_path / name
    p.write_text("# title\nsome content here\n", encoding="utf-8")
    item_id, error = manager.index_context(str(p), ctx=_ctx(tmp_path))
    assert error == ""
    return item_id


# ===================== State transitions =====================


def test_lifecycle_active_to_warm(
    manager: RagManager, lifecycle: RagLifecycle, tmp_path: Path
):
    """active items past warm_after_days become warm."""
    item_id = _index_doc(manager, tmp_path)
    # Backdate the item to bypass the time threshold
    past = int(time.time()) - 8 * 86400
    manager.store.storage.execute(
        "UPDATE rag_items SET last_accessed_at = ?, updated_at = ? WHERE item_id = ?",
        (past, past, item_id),
    )
    counts = lifecycle.cleanup_expired()
    assert counts["warm"] == 1
    assert manager.store.get_item(item_id).status == "warm"


def test_lifecycle_warm_to_archived(
    manager: RagManager, lifecycle: RagLifecycle, tmp_path: Path
):
    item_id = _index_doc(manager, tmp_path)
    past = int(time.time()) - 31 * 86400
    manager.store.storage.execute(
        "UPDATE rag_items SET status = 'warm', last_accessed_at = ?, updated_at = ? WHERE item_id = ?",
        (past, past, item_id),
    )
    counts = lifecycle.cleanup_expired()
    assert counts["archived"] == 1
    assert manager.store.get_item(item_id).status == "archived"


def test_lifecycle_archived_to_cold(
    manager: RagManager, lifecycle: RagLifecycle, tmp_path: Path
):
    item_id = _index_doc(manager, tmp_path)
    past = int(time.time()) - 91 * 86400
    manager.store.storage.execute(
        "UPDATE rag_items SET status = 'archived', last_accessed_at = ?, updated_at = ? WHERE item_id = ?",
        (past, past, item_id),
    )
    counts = lifecycle.cleanup_expired()
    assert counts["cold"] == 1
    assert manager.store.get_item(item_id).status == "cold"


def test_lifecycle_cold_to_deleted_chunks_gone(
    manager: RagManager, lifecycle: RagLifecycle, tmp_path: Path
):
    item_id = _index_doc(manager, tmp_path)
    past = int(time.time()) - 181 * 86400
    manager.store.storage.execute(
        "UPDATE rag_items SET status = 'cold', last_accessed_at = ?, updated_at = ? WHERE item_id = ?",
        (past, past, item_id),
    )
    counts = lifecycle.cleanup_expired()
    assert counts["deleted"] == 1
    # Tombstone preserved (default keep_tombstone=True)
    item = manager.store.get_item(item_id)
    assert item is not None
    assert item.status == "deleted"
    # Chunks gone
    assert manager.store.get_chunks(item_id) == []


# ===================== Pinned protection =====================


def test_lifecycle_pinned_never_transitions(
    manager: RagManager, lifecycle: RagLifecycle, tmp_path: Path
):
    """User feedback 7: pinned items must never auto-transition or auto-delete."""
    item_id = _index_doc(manager, tmp_path)
    # Pin and backdate far past every threshold
    past = int(time.time()) - 365 * 86400
    manager.store.storage.execute(
        "UPDATE rag_items SET pinned = 1, status = 'cold', last_accessed_at = ?, updated_at = ? WHERE item_id = ?",
        (past, past, item_id),
    )
    counts = lifecycle.cleanup_expired()
    # All counts must be 0 for this item
    assert counts["deleted"] == 0
    assert counts["archived"] == 0
    item = manager.store.get_item(item_id)
    assert item.status == "cold"
    assert item.pinned == 1
    # Chunks still present
    assert len(manager.store.get_chunks(item_id)) >= 1


# ===================== Log TTL =====================


def test_lifecycle_log_ttl_deletes_old_logs(
    manager: RagManager, lifecycle: RagLifecycle, tmp_path: Path
):
    log = tmp_path / "errors.log"
    log.write_text("ERROR: something failed\n", encoding="utf-8")
    item_id, error = manager.index_context(str(log), ctx=_ctx(tmp_path))
    assert error == ""
    # log items past log_ttl_days (default 7) are deleted regardless of state
    past = int(time.time()) - 8 * 86400
    manager.store.storage.execute(
        "UPDATE rag_items SET last_accessed_at = ?, updated_at = ? WHERE item_id = ?",
        (past, past, item_id),
    )
    counts = lifecycle.cleanup_expired()
    assert counts["log_deleted"] >= 1


# ===================== Stale / orphan detection =====================


def test_lifecycle_orphan_when_file_missing(
    manager: RagManager, lifecycle: RagLifecycle, tmp_path: Path
):
    item_id = _index_doc(manager, tmp_path, name="will_be_deleted.md")
    # Delete the source file
    (tmp_path / "will_be_deleted.md").unlink()

    counts = lifecycle.cleanup_expired()
    assert counts["orphan"] >= 1
    assert manager.store.get_item(item_id).status == "orphan"


def test_lifecycle_stale_when_content_changed(
    manager: RagManager, lifecycle: RagLifecycle, tmp_path: Path
):
    p = tmp_path / "doc.md"
    p.write_text("# original\nold content\n", encoding="utf-8")
    item_id, _ = manager.index_context(str(p), ctx=_ctx(tmp_path))

    # Change file content
    p.write_text("# updated\ncompletely different content here\n", encoding="utf-8")

    counts = lifecycle.cleanup_expired()
    assert counts["stale"] >= 1
    assert manager.store.get_item(item_id).status == "stale"


# ===================== touch updates last_accessed_at =====================


def test_lifecycle_touch_resets_clock(
    manager: RagManager, lifecycle: RagLifecycle, tmp_path: Path
):
    item_id = _index_doc(manager, tmp_path)
    before = manager.store.get_item(item_id).last_accessed_at or 0
    time.sleep(1.1)
    lifecycle.touch(item_id)
    after = manager.store.get_item(item_id).last_accessed_at
    assert after is not None
    assert after > before
    assert manager.store.get_item(item_id).access_count >= 1
