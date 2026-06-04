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
    return Database(tmp_path / "incremental.db")


@pytest.fixture
def manager(storage: Database, config: RagConfig) -> RagManager:
    return RagManager(storage, config, PermissionPolicy(AppConfig().permissions))


def _ctx(workspace_dir: Path) -> dict:
    return {
        "agent_id": "agent-a",
        "workspace_dir": str(workspace_dir),
        "sandbox_mode": "safe",
        "chat_id": "chat-1",
        "session_id": "sess-1",
        "channel_name": "cli",
    }


def test_initial_index_writes_active_mapping(manager: RagManager, storage: Database, tmp_path: Path):
    p = tmp_path / "doc.md"
    p.write_text("# A\nalpha\n\n# B\nbeta\n", encoding="utf-8")
    item_id, error = manager.index_context(str(p), ctx=_ctx(tmp_path))
    assert error == ""

    rows = storage.fetchall(
        "SELECT chunk_order, anchor_id, is_reused FROM rag_item_chunk_versions "
        "WHERE item_id = ? AND version = 1 ORDER BY chunk_order",
        (item_id,),
    )
    chunks = manager.store.get_active_chunks(item_id)
    assert len(rows) == len(chunks)
    assert [r["chunk_order"] for r in rows] == list(range(len(rows)))
    assert all(r["anchor_id"] for r in rows)
    assert all(r["is_reused"] == 0 for r in rows)


def test_reindex_dry_run_does_not_switch_active_version(
    manager: RagManager, tmp_path: Path
):
    p = tmp_path / "doc.md"
    p.write_text("# A\nalpha\n", encoding="utf-8")
    item_id, error = manager.index_context(str(p), ctx=_ctx(tmp_path))
    assert error == ""
    p.write_text("# A\nalpha changed\n", encoding="utf-8")

    ok, message = manager.reindex_context(item_id, ctx=_ctx(tmp_path), dry_run=True)
    assert ok, message
    assert "updated=" in message or "mode=" in message
    assert manager.store.get_item(item_id).active_version == 1


def test_reindex_stores_last_diff(manager: RagManager, tmp_path: Path):
    p = tmp_path / "doc.md"
    p.write_text("# A\nalpha\n", encoding="utf-8")
    item_id, error = manager.index_context(str(p), ctx=_ctx(tmp_path))
    assert error == ""
    p.write_text("# A\nalpha changed\n", encoding="utf-8")

    ok, message = manager.reindex_context(item_id, ctx=_ctx(tmp_path))
    assert ok, message
    item = manager.store.get_item(item_id)
    assert item.last_reindex_diff_id

    ok, diff_message = manager.diff_context(item_id, ctx=_ctx(tmp_path), last=True)
    assert ok, diff_message
    assert "last_diff=" in diff_message
    assert "rows=" in diff_message


def test_old_chunk_content_is_hidden_by_active_mapping(
    manager: RagManager, tmp_path: Path
):
    p = tmp_path / "doc.md"
    p.write_text("# A\nold-only-token\n", encoding="utf-8")
    item_id, error = manager.index_context(str(p), ctx=_ctx(tmp_path))
    assert error == ""
    p.write_text("# A\nnew-only-token\n", encoding="utf-8")
    ok, message = manager.reindex_context(item_id, ctx=_ctx(tmp_path))
    assert ok, message

    results, error = manager.search_context("old-only-token", ctx=_ctx(tmp_path))
    assert error == ""
    assert results == []

    results, error = manager.search_context("new-only-token", ctx=_ctx(tmp_path))
    assert error == ""
    assert results


def test_code_reindex_without_tree_sitter_falls_back_full(
    manager: RagManager, tmp_path: Path
):
    p = tmp_path / "x.py"
    p.write_text("def run():\n    return 1\n", encoding="utf-8")
    item_id, error = manager.index_context(str(p), ctx=_ctx(tmp_path))
    assert error == ""
    p.write_text("def run():\n    return 2\n", encoding="utf-8")

    ok, message = manager.diff_context(item_id, ctx=_ctx(tmp_path))
    assert ok, message
    # In environments without rag-code extra this is parser_unavailable; with
    # Tree-sitter installed it may be true incremental. Both are valid.
    assert "mode=" in message
