"""Phase 9 M9.6 tests: Memory maintenance (dedupe / conflict / stale).

Verifies:
1. Duplicates detected by Jaccard similarity above threshold
2. Conflicts detected when topic overlaps but polarity differs
3. Stale candidates detected when low-access + old + non-pinned
4. NEVER mutates rag_items (suggestions only)
5. Scope isolation (workspace-only suggestions don't span workspaces)
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mini_claw.config import AppConfig, RagConfig
from mini_claw.permissions.policy import PermissionPolicy
from mini_claw.rag.manager import RagManager
from mini_claw.rag.memory.maintenance import (
    MemoryMaintenance,
    _has_negation,
    _jaccard,
    _tokenize,
)
from mini_claw.rag.models import RagItem
from mini_claw.storage.db import Database


# ===================== Fixtures =====================


@pytest.fixture
def config_with_memory() -> RagConfig:
    cfg = RagConfig()
    cfg.enabled = True
    cfg.namespaces.context_enabled = True
    cfg.namespaces.memory_enabled = True
    return cfg


@pytest.fixture
def storage(tmp_path: Path) -> Database:
    return Database(tmp_path / "maintenance.db")


@pytest.fixture
def manager(storage, config_with_memory) -> RagManager:
    return RagManager(
        storage, config_with_memory, PermissionPolicy(AppConfig().permissions)
    )


def _ctx(agent_id="agent-a", workspace_dir=None) -> dict:
    return {
        "agent_id": agent_id,
        "chat_id": "chat-1",
        "channel_name": "cli",
        "workspace_dir": workspace_dir,
    }


def _insert_memory_item(
    storage: Database,
    item_id: str,
    content: str,
    *,
    owner_agent_id: str = "agent-a",
    scope_type: str = "agent",
    scope_id: str | None = None,
    workspace_dir: str | None = None,
    pinned: int = 0,
    access_count: int = 0,
    created_at: int | None = None,
    last_accessed_at: int | None = None,
):
    """Insert a memory item directly for maintenance tests."""
    now = int(time.time())
    created_at = created_at or now
    storage.execute(
        """
        INSERT INTO rag_items (
            item_id, namespace, source_type, scope_type, scope_id,
            owner_agent_id, status, importance, pinned, confidence,
            workspace_dir, created_at, updated_at, last_accessed_at, access_count,
            active_version, sensitivity_level
        ) VALUES (?, 'memory', 'project_rule', ?, ?, ?, 'active', 3, ?, 0.9,
                  ?, ?, ?, ?, ?, 1, 'low')
        """,
        (
            item_id,
            scope_type,
            scope_id or owner_agent_id,
            owner_agent_id,
            pinned,
            workspace_dir,
            created_at,
            created_at,
            last_accessed_at,
            access_count,
        ),
    )
    storage.execute(
        """
        INSERT INTO rag_chunks (chunk_id, item_id, chunk_index, content, token_count, version)
        VALUES (?, ?, 0, ?, ?, 1)
        """,
        (f"{item_id}-0", item_id, content, len(content) // 4),
    )


# ===================== Helper function tests =====================


def test_tokenize_handles_empty():
    assert _tokenize("") == set()


def test_tokenize_strips_punctuation():
    tokens = _tokenize("hello, world! foo-bar.")
    assert "hello" in tokens
    assert "world" in tokens
    assert "foo" in tokens or "bar" in tokens


def test_jaccard_identical_strings_returns_one():
    a = _tokenize("project must use python 3.10")
    b = _tokenize("project must use python 3.10")
    assert _jaccard(a, b) == 1.0


def test_jaccard_disjoint_returns_zero():
    a = _tokenize("apple banana")
    b = _tokenize("car door")
    assert _jaccard(a, b) == 0.0


def test_has_negation_detects_english():
    assert _has_negation("we must not commit secrets")
    assert _has_negation("never use eval")
    assert not _has_negation("we must commit early")


def test_has_negation_detects_chinese():
    assert _has_negation("禁止 使用 eval")
    assert _has_negation("不要 提交 密钥")


# ===================== Duplicate detection =====================


def test_dedupe_finds_high_similarity_pair(storage: Database):
    _insert_memory_item(storage, "m1", "project must use python 3.10 for backend")
    _insert_memory_item(storage, "m2", "project must use python 3.10 for backend services")

    maintenance = MemoryMaintenance(storage, config={"dupe_threshold": 0.6})
    result = maintenance.run(ctx=_ctx(), scope="agent")

    assert result.scanned_count == 2
    assert len(result.duplicates) == 1
    group = result.duplicates[0]
    assert group.similarity >= 0.6


def test_dedupe_skips_low_similarity_items(storage: Database):
    _insert_memory_item(storage, "m1", "use python for backend")
    _insert_memory_item(storage, "m2", "use rust for cli tools")

    maintenance = MemoryMaintenance(storage, config={"dupe_threshold": 0.7})
    result = maintenance.run(ctx=_ctx(), scope="agent")

    assert len(result.duplicates) == 0


def test_dedupe_does_not_cross_scope_boundary(storage: Database):
    """Same-content memories in different scopes are intentional, not duplicates."""
    _insert_memory_item(
        storage, "m1", "must run pytest before commit",
        scope_type="agent", scope_id="agent-a",
    )
    _insert_memory_item(
        storage, "m2", "must run pytest before commit",
        scope_type="workspace", scope_id="ws-1",
        workspace_dir="ws-1",
    )

    maintenance = MemoryMaintenance(storage, config={"dupe_threshold": 0.5})
    result = maintenance.run(ctx=_ctx(workspace_dir="ws-1"), scope="agent")

    # Even though content matches, scope differs → not flagged
    assert len(result.duplicates) == 0


# ===================== Conflict detection =====================


def test_conflict_detects_negation_mismatch(storage: Database):
    _insert_memory_item(storage, "m1", "must use redis for caching layer")
    _insert_memory_item(storage, "m2", "must not use redis for caching layer")

    maintenance = MemoryMaintenance(
        storage,
        config={"dupe_threshold": 0.99, "conflict_threshold": 0.5},
    )
    result = maintenance.run(ctx=_ctx(), scope="agent")

    # Should not be in duplicates (negation flips polarity)
    assert all("m2" not in d.duplicate_ids for d in result.duplicates)
    # Should be in conflicts
    assert len(result.conflicts) == 1
    pair = result.conflicts[0]
    assert {pair.item_id_a, pair.item_id_b} == {"m1", "m2"}


def test_conflict_skips_when_same_polarity(storage: Database):
    _insert_memory_item(storage, "m1", "must use redis for caching")
    _insert_memory_item(storage, "m2", "must use redis for sessions")

    maintenance = MemoryMaintenance(storage, config={"conflict_threshold": 0.4})
    result = maintenance.run(ctx=_ctx(), scope="agent")

    # No conflicts: both positive, no polarity mismatch
    assert len(result.conflicts) == 0


# ===================== Stale detection =====================


def test_stale_detects_old_low_access_unpinned(storage: Database):
    long_ago = int(time.time()) - 100 * 86400  # 100 days
    _insert_memory_item(
        storage, "m_stale", "old rule rarely used",
        access_count=0, created_at=long_ago, pinned=0,
    )
    _insert_memory_item(
        storage, "m_fresh", "recent rule",
        access_count=5, created_at=int(time.time()), pinned=0,
    )

    maintenance = MemoryMaintenance(storage, config={"stale_age_days": 90})
    result = maintenance.run(ctx=_ctx(), scope="agent")

    stale_ids = {s.item_id for s in result.stale}
    assert "m_stale" in stale_ids
    assert "m_fresh" not in stale_ids


def test_stale_skips_pinned_items(storage: Database):
    long_ago = int(time.time()) - 100 * 86400
    _insert_memory_item(
        storage, "m_pinned", "old but pinned",
        access_count=0, created_at=long_ago, pinned=1,
    )

    maintenance = MemoryMaintenance(storage, config={"stale_age_days": 90})
    result = maintenance.run(ctx=_ctx(), scope="agent")

    stale_ids = {s.item_id for s in result.stale}
    assert "m_pinned" not in stale_ids


def test_stale_skips_high_access_items(storage: Database):
    long_ago = int(time.time()) - 100 * 86400
    _insert_memory_item(
        storage, "m_busy", "old but heavily used",
        access_count=50, created_at=long_ago, pinned=0,
    )

    maintenance = MemoryMaintenance(
        storage, config={"stale_age_days": 90, "stale_max_access": 1},
    )
    result = maintenance.run(ctx=_ctx(), scope="agent")

    stale_ids = {s.item_id for s in result.stale}
    assert "m_busy" not in stale_ids


# ===================== Suggestions are non-mutating =====================


def test_maintenance_does_not_mutate_rag_items(storage: Database):
    """Critical invariant: maintenance generates suggestions only, never deletes."""
    _insert_memory_item(storage, "m1", "rule one alpha beta gamma")
    _insert_memory_item(storage, "m2", "rule one alpha beta gamma delta")

    before_count = storage.fetchone(
        "SELECT COUNT(*) AS cnt FROM rag_items"
    )["cnt"]
    before_status = storage.fetchone(
        "SELECT status FROM rag_items WHERE item_id = ?", ("m1",)
    )["status"]

    maintenance = MemoryMaintenance(storage, config={"dupe_threshold": 0.5})
    result = maintenance.run(ctx=_ctx(), scope="agent")

    # Verify suggestion produced
    assert len(result.duplicates) >= 1

    # Verify NOTHING in rag_items changed
    after_count = storage.fetchone(
        "SELECT COUNT(*) AS cnt FROM rag_items"
    )["cnt"]
    after_status = storage.fetchone(
        "SELECT status FROM rag_items WHERE item_id = ?", ("m1",)
    )["status"]

    assert after_count == before_count
    assert after_status == before_status


# ===================== RagManager.run_memory_maintenance =====================


def test_manager_run_memory_maintenance_returns_dict(manager: RagManager):
    """High-level API returns a JSON-shaped dict."""
    result = manager.run_memory_maintenance(ctx=_ctx(), scope="agent")
    assert "duplicates" in result
    assert "conflicts" in result
    assert "stale" in result
    assert "scanned_count" in result
    assert isinstance(result["duplicates"], list)


def test_manager_run_memory_maintenance_workspace_scope_requires_workspace(
    manager: RagManager,
):
    """Workspace scope without workspace_dir returns empty (not an error)."""
    # Don't set workspace_dir; scope='workspace' should yield zero results
    result = manager.run_memory_maintenance(
        ctx={"agent_id": "agent-a"}, scope="workspace"
    )
    assert result["scanned_count"] == 0
