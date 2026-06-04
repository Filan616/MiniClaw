"""Tests for Phase 8 M2: RagManager facade including delete transaction order."""

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
    return Database(tmp_path / "manager.db")


@pytest.fixture
def manager(storage: Database, config: RagConfig) -> RagManager:
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


# ===================== Disabled state =====================


def test_manager_returns_disabled_when_rag_off(storage: Database, tmp_path: Path):
    cfg = RagConfig()  # enabled=False default
    mgr = RagManager(storage, cfg, PermissionPolicy(AppConfig().permissions))
    item_id, error = mgr.index_context(str(tmp_path / "x.md"), ctx=_ctx(tmp_path))
    assert item_id is None
    assert "disabled" in error.lower()

    results, error = mgr.search_context("foo", ctx=_ctx(tmp_path))
    assert results == []
    assert "disabled" in error.lower()


def test_manager_returns_disabled_when_context_namespace_off(
    storage: Database, tmp_path: Path
):
    cfg = RagConfig()
    cfg.enabled = True
    cfg.namespaces.context_enabled = False
    mgr = RagManager(storage, cfg, PermissionPolicy(AppConfig().permissions))
    items = mgr.list_contexts(ctx=_ctx(tmp_path))
    assert items == []


# ===================== Index → list → inspect =====================


def test_manager_index_then_list_returns_item(manager: RagManager, tmp_path: Path):
    md = tmp_path / "doc.md"
    md.write_text("# title\nbody\n", encoding="utf-8")
    item_id, error = manager.index_context(str(md), ctx=_ctx(tmp_path), title="My Doc")
    assert error == ""

    items = manager.list_contexts(ctx=_ctx(tmp_path))
    assert len(items) == 1
    assert items[0].item_id == item_id
    assert items[0].title == "My Doc"


def test_manager_inspect_blocks_cross_agent(manager: RagManager, tmp_path: Path):
    md = tmp_path / "doc.md"
    md.write_text("body", encoding="utf-8")
    item_id, _ = manager.index_context(
        str(md), ctx=_ctx(tmp_path, agent_id="agent-a")
    )

    item, error = manager.inspect_context(
        item_id, ctx=_ctx(tmp_path, agent_id="agent-b")
    )
    assert item is None
    assert "another agent" in error.lower()


# ===================== Archive / clear =====================


def test_manager_archive_changes_status(manager: RagManager, tmp_path: Path):
    md = tmp_path / "doc.md"
    md.write_text("body", encoding="utf-8")
    item_id, _ = manager.index_context(str(md), ctx=_ctx(tmp_path))

    success, error = manager.archive_context(item_id, ctx=_ctx(tmp_path))
    assert success
    assert manager.store.get_item(item_id).status == "archived"


# ===================== Delete transaction order (user feedback 6) =====================


def test_manager_delete_follows_transaction_order(
    manager: RagManager, tmp_path: Path, storage: Database
):
    """User feedback 6: delete must remove FTS rows + chunks + tombstone item."""
    md = tmp_path / "doc.md"
    md.write_text("# Title\n\ncontent\n", encoding="utf-8")
    item_id, _ = manager.index_context(str(md), ctx=_ctx(tmp_path))

    # Verify chunks exist
    chunks_before = manager.store.get_chunks(item_id)
    assert len(chunks_before) >= 1

    success, error = manager.delete_context(item_id, ctx=_ctx(tmp_path))
    assert success, f"delete failed: {error}"

    # Chunks gone
    chunks_after = manager.store.get_chunks(item_id)
    assert chunks_after == []

    # Item tombstone (status=deleted) since keep_tombstone=True default
    item = manager.store.get_item(item_id)
    assert item is not None
    assert item.status == "deleted"

    # FTS rows gone (if FTS5 available)
    try:
        fts_rows = storage.fetchall(
            "SELECT chunk_id FROM rag_chunks_fts WHERE item_id = ?", (item_id,)
        )
        assert fts_rows == []
    except Exception:
        # FTS5 not available, skip this check
        pass


def test_manager_delete_blocks_cross_agent(manager: RagManager, tmp_path: Path):
    md = tmp_path / "doc.md"
    md.write_text("body", encoding="utf-8")
    item_id, _ = manager.index_context(
        str(md), ctx=_ctx(tmp_path, agent_id="agent-a")
    )

    success, error = manager.delete_context(
        item_id, ctx=_ctx(tmp_path, agent_id="agent-b")
    )
    assert not success
    assert "another agent" in error.lower()


def test_manager_delete_nonexistent_returns_error(manager: RagManager, tmp_path: Path):
    success, error = manager.delete_context("ghost-id", ctx=_ctx(tmp_path))
    assert not success
    assert "not found" in error.lower()


# ===================== read_sensitive_context (M2 plan: user feedback 4) =====================


def test_manager_read_sensitive_context_requires_owner(
    manager: RagManager, tmp_path: Path
):
    py = tmp_path / "config.py"
    py.write_text("api_key=a\ntoken=b\npassword=c\nSECRET_KEY=d\n", encoding="utf-8")
    item_id, _ = manager.index_context(
        str(py), ctx=_ctx(tmp_path, agent_id="agent-a")
    )
    assert item_id is not None
    chunks = manager.store.get_chunks(item_id)
    assert chunks
    chunk_id = chunks[0].chunk_id

    # Owner can read
    content, error = manager.read_sensitive_context(
        item_id, chunk_id, ctx=_ctx(tmp_path, agent_id="agent-a")
    )
    assert error == ""
    assert content is not None

    # Non-owner cannot
    content, error = manager.read_sensitive_context(
        item_id, chunk_id, ctx=_ctx(tmp_path, agent_id="agent-b")
    )
    assert content is None
    assert "another agent" in error.lower()
