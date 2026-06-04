"""Phase 9 M9.5 tests: Context isolation hardening.

Verifies:
1. Four-section injection (Context, Memory, Workspace Memory, Chat History) — never merged
2. search_memory scope parameter (agent / workspace / user) with fail-closed
3. Cross-workspace memory isolation
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mini_claw.config import AppConfig, RagConfig
from mini_claw.permissions.policy import PermissionPolicy
from mini_claw.rag.injector import (
    CHAT_HISTORY_HEADER,
    CONTEXT_UNTRUSTED_HEADER,
    MEMORY_TRUSTED_HEADER,
    WORKSPACE_MEMORY_HEADER,
    build_chat_history_block,
    build_workspace_memory_block,
    inject_chat_history_section,
    inject_workspace_memory_section,
)
from mini_claw.rag.manager import RagManager
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
    return Database(tmp_path / "isolation.db")


@pytest.fixture
def manager(storage, config_with_memory) -> RagManager:
    return RagManager(
        storage, config_with_memory, PermissionPolicy(AppConfig().permissions)
    )


# ===================== Four-section injection =====================


def test_workspace_memory_block_uses_dedicated_header():
    class _Mem:
        def __init__(self, mtype, scope_id, content):
            self.memory_type = mtype
            self.scope_id = scope_id
            self.content = content

    mems = [_Mem("project_constraint", "ws-1", "use python 3.10")]
    block = build_workspace_memory_block(mems)
    assert WORKSPACE_MEMORY_HEADER in block
    # Content rendered
    assert "project_constraint" in block
    assert "use python 3.10" in block


def test_chat_history_block_uses_dedicated_header():
    items = [
        {"role": "user", "content": "hello", "created_at": 1000},
        {"role": "assistant", "content": "hi there", "created_at": 1001},
    ]
    block = build_chat_history_block(items)
    assert CHAT_HISTORY_HEADER in block
    assert "hello" in block
    assert "hi there" in block


def test_inject_workspace_memory_keeps_separate_system_message():
    """Workspace memory must be its own system message, never merged."""
    class _Mem:
        memory_type = "project_constraint"
        scope_id = "ws-1"
        content = "rule x"

    base = [{"role": "system", "content": "agent prompt"}, {"role": "user", "content": "go"}]
    result = inject_workspace_memory_section(base, [_Mem()])

    # Should have agent prompt + workspace memory + user (3 messages now)
    assert len(result) == 3
    # The injected one is at index 1 (after agent system, before user)
    assert result[0]["role"] == "system"
    assert result[1]["role"] == "system"
    assert result[2]["role"] == "user"
    # And it must use the workspace header, not the regular memory header
    assert WORKSPACE_MEMORY_HEADER in result[1]["content"]
    assert MEMORY_TRUSTED_HEADER not in result[1]["content"]


def test_inject_chat_history_keeps_separate_system_message():
    base = [{"role": "system", "content": "x"}, {"role": "user", "content": "go"}]
    result = inject_chat_history_section(base, [{"role": "user", "content": "old", "created_at": 1}])

    assert len(result) == 3
    assert result[1]["role"] == "system"
    assert CHAT_HISTORY_HEADER in result[1]["content"]


def test_four_headers_are_distinct():
    """The four headers must be distinguishable strings."""
    headers = {
        CONTEXT_UNTRUSTED_HEADER,
        MEMORY_TRUSTED_HEADER,
        WORKSPACE_MEMORY_HEADER,
        CHAT_HISTORY_HEADER,
    }
    assert len(headers) == 4  # All distinct


# ===================== search_memory scope parameter =====================


def test_search_memory_scope_workspace_fails_closed_without_workspace_dir(
    manager: RagManager,
):
    """fail-closed: scope='workspace' but ctx.workspace_dir is None → empty + error."""
    ctx = {"agent_id": "agent-a", "channel_name": "cli"}  # no workspace_dir
    results, error = manager.search_memory("anything", ctx=ctx, scope="workspace")
    assert results == []
    assert "workspace_dir" in error.lower()


def test_search_memory_scope_agent_fails_closed_without_agent_id(
    manager: RagManager,
):
    """fail-closed: scope='agent' but ctx.agent_id is None → empty + error."""
    ctx = {"chat_id": "c", "channel_name": "cli"}  # no agent_id
    results, error = manager.search_memory("anything", ctx=ctx, scope="agent")
    assert results == []
    assert "agent_id" in error.lower()


def test_search_memory_unknown_scope_returns_error(manager: RagManager):
    ctx = {"agent_id": "a", "chat_id": "c", "channel_name": "cli"}
    results, error = manager.search_memory("anything", ctx=ctx, scope="unknown_scope")
    assert results == []
    assert "scope" in error.lower()


# ===================== Cross-workspace memory isolation =====================


def _insert_workspace_memory(storage: Database, item_id: str, workspace_dir: str, content: str):
    """Helper: insert a workspace-scoped memory item directly."""
    import time
    now = int(time.time())
    storage.execute(
        """
        INSERT INTO rag_items (
            item_id, namespace, source_type, scope_type, scope_id,
            owner_agent_id, status, importance, pinned, confidence,
            workspace_dir, created_at, updated_at, active_version, sensitivity_level
        ) VALUES (?, 'memory', 'project_constraint', 'workspace', ?,
                  'agent-a', 'active', 3, 0, 0.9,
                  ?, ?, ?, 1, 'low')
        """,
        (item_id, workspace_dir, workspace_dir, now, now),
    )
    storage.execute(
        """
        INSERT INTO rag_chunks (chunk_id, item_id, chunk_index, content, token_count, version)
        VALUES (?, ?, 0, ?, ?, 1)
        """,
        (f"{item_id}-0", item_id, content, len(content) // 4),
    )
    # FTS index too
    try:
        storage.execute(
            "INSERT INTO rag_chunks_fts (chunk_id, item_id, content, section_title, symbol_name) "
            "VALUES (?, ?, ?, '', '')",
            (f"{item_id}-0", item_id, content),
        )
    except Exception:
        pass
    # Active version mapping
    storage.execute(
        """
        INSERT INTO rag_item_chunk_versions (item_id, version, chunk_id, chunk_order, status, created_at)
        VALUES (?, 1, ?, 0, 'active', ?)
        """,
        (item_id, f"{item_id}-0", now),
    )


def test_workspace_memory_isolated_across_workspaces(manager: RagManager, storage: Database):
    """A memory in workspace ws-A must NOT appear in workspace ws-B searches."""
    _insert_workspace_memory(storage, "wm-A", "ws-A", "alpha rule for project A")
    _insert_workspace_memory(storage, "wm-B", "ws-B", "beta rule for project B")

    # Search from workspace A
    ctx_a = {"agent_id": "agent-a", "chat_id": "c", "channel_name": "cli", "workspace_dir": "ws-A"}
    results_a, err_a = manager.search_memory("rule", ctx=ctx_a, scope="workspace")
    assert err_a == ""
    a_ids = {r.item_id for r in results_a}
    assert "wm-A" in a_ids
    assert "wm-B" not in a_ids  # cross-workspace isolation

    # Search from workspace B
    ctx_b = {"agent_id": "agent-a", "chat_id": "c", "channel_name": "cli", "workspace_dir": "ws-B"}
    results_b, err_b = manager.search_memory("rule", ctx=ctx_b, scope="workspace")
    assert err_b == ""
    b_ids = {r.item_id for r in results_b}
    assert "wm-B" in b_ids
    assert "wm-A" not in b_ids
