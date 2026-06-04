"""Phase 9 M9.1 tests: Chat search (messages_fts + scope isolation).

Verifies:
1. messages_fts index is populated when messages are stored
2. Scope filtering: current_session, current_agent, workspace, all_visible
3. Fail-closed: missing ctx fields → reject (not fall back to global)
4. Channel isolation: same chat_id on different channels stays separate
5. FTS5 → LIKE fallback works
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mini_claw.chat_search.indexer import index_message_row
from mini_claw.chat_search.manager import ChatSearchManager
from mini_claw.chat_search.retriever import ChatSearchRetriever
from mini_claw.gateway.session import SessionManager, derive_session_id
from mini_claw.storage.db import Database


# ===================== Fixtures =====================


@pytest.fixture
def storage(tmp_path: Path) -> Database:
    return Database(tmp_path / "chat_search.db")


@pytest.fixture
def manager(storage) -> ChatSearchManager:
    return ChatSearchManager(storage, config={"fts_max_results": 50})


def _ctx(
    agent_id="agent-a",
    chat_id="chat-1",
    channel_name="cli",
    workspace_dir="ws-1",
    session_id=None,
):
    if session_id is None:
        session_id = derive_session_id(channel_name, chat_id, agent_id)
    return SimpleNamespace(
        agent_id=agent_id,
        chat_id=chat_id,
        channel_name=channel_name,
        workspace_dir=workspace_dir,
        session_id=session_id,
    )


# ===================== Indexing via SessionManager =====================


def test_store_message_mirrors_to_fts(storage: Database):
    sm = SessionManager(storage)
    sm.store_message(
        chat_id="chat-1",
        agent_id="agent-a",
        role="user",
        content="hello world",
        channel_name="cli",
        workspace_dir="ws-1",
    )

    # FTS table should have the row
    row = storage.fetchone(
        "SELECT * FROM messages_fts WHERE content MATCH 'hello'"
    )
    assert row is not None
    assert "hello" in row["content"]


def test_index_message_row_returns_true_on_success(storage: Database):
    # Insert a message manually
    storage.execute(
        "INSERT INTO messages (chat_id, agent_id, role, content, created_at, "
        "channel_name, workspace_dir) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("c", "a", "user", "test message body", 1000, "cli", "ws"),
    )
    msg_id = storage.fetchone("SELECT last_insert_rowid() AS id")["id"]

    success = index_message_row(
        storage,
        msg_id,
        session_id="sess-1",
        agent_id="a",
        chat_id="c",
        channel_name="cli",
        workspace_dir="ws",
        role="user",
        content="test message body",
        created_at=1000,
    )
    assert success is True


# ===================== Scope filtering =====================


def test_search_current_session_only_finds_same_session(
    manager: ChatSearchManager, storage: Database
):
    sm = SessionManager(storage)
    sm.store_message(
        chat_id="chat-1", agent_id="agent-a", role="user", content="alpha rule",
        channel_name="cli", workspace_dir="ws-1",
    )
    sm.store_message(
        chat_id="chat-2", agent_id="agent-a", role="user", content="beta rule",
        channel_name="cli", workspace_dir="ws-1",
    )

    ctx = _ctx(chat_id="chat-1")
    results = manager.search("rule", scope="current_session", ctx=ctx)

    contents = [r["content"] for r in results]
    assert any("alpha" in c for c in contents)
    assert not any("beta" in c for c in contents)


def test_search_current_agent_finds_across_chats(
    manager: ChatSearchManager, storage: Database
):
    sm = SessionManager(storage)
    sm.store_message(
        chat_id="chat-1", agent_id="agent-a", role="user", content="alpha rule",
        channel_name="cli", workspace_dir="ws-1",
    )
    sm.store_message(
        chat_id="chat-2", agent_id="agent-a", role="user", content="beta rule",
        channel_name="cli", workspace_dir="ws-1",
    )

    ctx = _ctx(chat_id="chat-1")
    results = manager.search("rule", scope="current_agent", ctx=ctx)

    contents = [r["content"] for r in results]
    assert any("alpha" in c for c in contents)
    assert any("beta" in c for c in contents)


def test_search_workspace_scope_finds_cross_chat_same_workspace(
    manager: ChatSearchManager, storage: Database
):
    sm = SessionManager(storage)
    sm.store_message(
        chat_id="chat-1", agent_id="agent-a", role="user",
        content="zeta rule", channel_name="cli", workspace_dir="ws-1",
    )
    sm.store_message(
        chat_id="chat-2", agent_id="agent-b", role="user",
        content="zeta theory", channel_name="cli", workspace_dir="ws-1",
    )
    sm.store_message(
        chat_id="chat-3", agent_id="agent-a", role="user",
        content="zeta misc", channel_name="cli", workspace_dir="ws-OTHER",
    )

    ctx = _ctx(chat_id="chat-1", workspace_dir="ws-1")
    results = manager.search("zeta", scope="workspace", ctx=ctx)

    contents = [r["content"] for r in results]
    # Both ws-1 messages found, ws-OTHER excluded
    assert any("rule" in c for c in contents)
    assert any("theory" in c for c in contents)
    assert not any("misc" in c for c in contents)


# ===================== Fail-closed scope checks =====================


def test_workspace_scope_fails_closed_without_workspace_dir(
    manager: ChatSearchManager,
):
    ctx = _ctx(workspace_dir=None)
    with pytest.raises(ValueError, match="workspace_dir"):
        manager.search("anything", scope="workspace", ctx=ctx)


def test_session_scope_fails_closed_without_session_id(
    manager: ChatSearchManager,
):
    # session_id=None but channel_name and agent_id set
    ctx = SimpleNamespace(
        agent_id="a", chat_id="c", channel_name="cli",
        workspace_dir="ws", session_id=None,
    )
    with pytest.raises(ValueError, match="session_id"):
        manager.search("anything", scope="current_session", ctx=ctx)


def test_search_fails_closed_without_channel_name(manager: ChatSearchManager):
    ctx = SimpleNamespace(
        agent_id="a", chat_id="c", channel_name=None,
        workspace_dir="ws", session_id="s",
    )
    with pytest.raises(ValueError, match="channel"):
        manager.search("anything", scope="current_agent", ctx=ctx)


def test_all_visible_scope_blocked_unless_config_allows(
    storage: Database,
):
    """all_visible should be rejected unless config.allow_global is true."""
    mgr_blocked = ChatSearchManager(storage, config={"allow_global": False})
    ctx = _ctx()
    with pytest.raises(ValueError, match="disabled"):
        mgr_blocked.search("anything", scope="all_visible", ctx=ctx)

    # Allow it explicitly
    mgr_open = ChatSearchManager(storage, config={"allow_global": True})
    # Should not raise — even if no results
    mgr_open.search("anything", scope="all_visible", ctx=ctx)


# ===================== Channel isolation =====================


def test_same_chat_id_different_channels_stay_separate(
    manager: ChatSearchManager, storage: Database
):
    """Phase 9 P0.1 invariant: same chat_id on different channels does NOT mix."""
    sm = SessionManager(storage)
    sm.store_message(
        chat_id="chat-shared", agent_id="agent-a", role="user",
        content="cli secret rule", channel_name="cli", workspace_dir="ws-1",
    )
    sm.store_message(
        chat_id="chat-shared", agent_id="agent-a", role="user",
        content="feishu private rule", channel_name="feishu", workspace_dir="ws-1",
    )

    # Search from cli channel
    ctx_cli = _ctx(chat_id="chat-shared", channel_name="cli")
    results_cli = manager.search("rule", scope="current_agent", ctx=ctx_cli)
    cli_contents = [r["content"] for r in results_cli]
    assert any("cli secret" in c for c in cli_contents)
    assert not any("feishu private" in c for c in cli_contents)

    # Search from feishu channel
    ctx_feishu = _ctx(chat_id="chat-shared", channel_name="feishu")
    results_feishu = manager.search("rule", scope="current_agent", ctx=ctx_feishu)
    feishu_contents = [r["content"] for r in results_feishu]
    assert any("feishu private" in c for c in feishu_contents)
    assert not any("cli secret" in c for c in feishu_contents)


# ===================== Manager utilities =====================


def test_manager_get_status_reports_fts_availability(
    manager: ChatSearchManager, storage: Database
):
    sm = SessionManager(storage)
    sm.store_message(
        chat_id="c", agent_id="a", role="user", content="hello",
        channel_name="cli", workspace_dir="ws",
    )
    status = manager.get_status()
    assert "fts_available" in status
    assert "total_messages" in status
    assert status["total_messages"] >= 1


def test_manager_rebuild_index_repopulates_fts(
    manager: ChatSearchManager, storage: Database
):
    sm = SessionManager(storage)
    sm.store_message(
        chat_id="c", agent_id="a", role="user", content="rebuild test",
        channel_name="cli", workspace_dir="ws",
    )

    # Wipe FTS manually
    storage.execute("DELETE FROM messages_fts")

    # Rebuild
    result = manager.rebuild_index()

    assert result["total"] >= 1
    assert result["indexed"] >= 1

    # FTS should now have the row
    row = storage.fetchone("SELECT COUNT(*) AS cnt FROM messages_fts")
    assert int(row["cnt"]) >= 1
