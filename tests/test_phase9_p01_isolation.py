"""Phase 9 P0.1 tests: AgentContext + derive_session_id + messages migration.

Verifies:
1. derive_session_id is deterministic across calls (stable session_id)
2. Different (channel, chat, agent) combos produce different ids
3. AgentContext has session_id and channel_name fields
4. messages table has channel_name, workspace_dir, workspace_dir_inferred columns
5. get_history filters by channel
6. count_messages filters by channel
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mini_claw.agent.context import AgentContext
from mini_claw.gateway.session import SessionManager, derive_session_id
from mini_claw.storage.db import Database


# ===================== derive_session_id =====================


def test_derive_session_id_is_deterministic():
    """Same (channel, chat, agent) → same session_id."""
    sid1 = derive_session_id("cli", "chat-1", "agent-a")
    sid2 = derive_session_id("cli", "chat-1", "agent-a")
    assert sid1 == sid2


def test_derive_session_id_changes_with_channel():
    sid_cli = derive_session_id("cli", "chat-1", "agent-a")
    sid_feishu = derive_session_id("feishu", "chat-1", "agent-a")
    assert sid_cli != sid_feishu


def test_derive_session_id_changes_with_chat():
    sid1 = derive_session_id("cli", "chat-1", "agent-a")
    sid2 = derive_session_id("cli", "chat-2", "agent-a")
    assert sid1 != sid2


def test_derive_session_id_changes_with_agent():
    sid1 = derive_session_id("cli", "chat-1", "agent-a")
    sid2 = derive_session_id("cli", "chat-1", "agent-b")
    assert sid1 != sid2


def test_derive_session_id_thread_id_optional():
    sid_no_thread = derive_session_id("cli", "chat-1", "agent-a")
    sid_with_thread = derive_session_id("cli", "chat-1", "agent-a", thread_id="t1")
    # Different threads → different sessions
    assert sid_no_thread != sid_with_thread


def test_derive_session_id_returns_short_hex():
    """16-char hex (sha1[:16])."""
    sid = derive_session_id("cli", "chat", "agent")
    assert len(sid) == 16
    int(sid, 16)  # Must be valid hex


# ===================== AgentContext fields =====================


def test_agent_context_has_session_id_field():
    ctx = AgentContext(
        chat_id="c", agent_id="a", workspace_dir=Path("/tmp"),
        session_id="sess-123",
    )
    assert ctx.session_id == "sess-123"


def test_agent_context_has_channel_name_field():
    ctx = AgentContext(
        chat_id="c", agent_id="a", workspace_dir=Path("/tmp"),
        channel_name="cli",
    )
    assert ctx.channel_name == "cli"


def test_agent_context_session_id_defaults_to_none():
    ctx = AgentContext(chat_id="c", agent_id="a", workspace_dir=Path("/tmp"))
    assert ctx.session_id is None
    assert ctx.channel_name is None


# ===================== messages migration =====================


def test_messages_table_has_channel_name_column(tmp_path: Path):
    db = Database(tmp_path / "p0.db")
    cols = db.fetchall("PRAGMA table_info(messages)")
    col_names = {c["name"] for c in cols}
    assert "channel_name" in col_names


def test_messages_table_has_workspace_dir_column(tmp_path: Path):
    db = Database(tmp_path / "p0.db")
    cols = db.fetchall("PRAGMA table_info(messages)")
    col_names = {c["name"] for c in cols}
    assert "workspace_dir" in col_names


def test_messages_table_has_workspace_dir_inferred_column(tmp_path: Path):
    db = Database(tmp_path / "p0.db")
    cols = db.fetchall("PRAGMA table_info(messages)")
    col_names = {c["name"] for c in cols}
    assert "workspace_dir_inferred" in col_names


# ===================== store_message writes new columns =====================


def test_store_message_writes_channel_name(tmp_path: Path):
    db = Database(tmp_path / "p0.db")
    sm = SessionManager(db)
    sm.store_message(
        chat_id="c", agent_id="a", role="user", content="hi",
        channel_name="cli", workspace_dir="ws",
    )
    row = db.fetchone(
        "SELECT channel_name, workspace_dir, workspace_dir_inferred FROM messages "
        "WHERE chat_id='c' AND agent_id='a' ORDER BY id DESC LIMIT 1"
    )
    assert row["channel_name"] == "cli"
    assert row["workspace_dir"] == "ws"
    assert row["workspace_dir_inferred"] == 0  # 0 = trustworthy (just written)


# ===================== get_history channel filter =====================


def test_get_history_filters_by_channel(tmp_path: Path):
    db = Database(tmp_path / "p0.db")
    sm = SessionManager(db)
    sm.store_message(
        chat_id="c-shared", agent_id="a", role="user", content="cli msg",
        channel_name="cli", workspace_dir="ws",
    )
    sm.store_message(
        chat_id="c-shared", agent_id="a", role="user", content="feishu msg",
        channel_name="feishu", workspace_dir="ws",
    )

    cli_history = sm.get_history("c-shared", "a", channel_name="cli")
    cli_contents = [m.get("content", "") for m in cli_history]
    assert any("cli msg" in c for c in cli_contents)
    assert not any("feishu msg" in c for c in cli_contents)


def test_count_messages_filters_by_channel(tmp_path: Path):
    db = Database(tmp_path / "p0.db")
    sm = SessionManager(db)
    sm.store_message(
        chat_id="c-shared", agent_id="a", role="user", content="cli msg",
        channel_name="cli", workspace_dir="ws",
    )
    sm.store_message(
        chat_id="c-shared", agent_id="a", role="user", content="feishu msg",
        channel_name="feishu", workspace_dir="ws",
    )

    cli_count = sm.count_messages("c-shared", "a", channel_name="cli")
    feishu_count = sm.count_messages("c-shared", "a", channel_name="feishu")

    assert cli_count == 1
    assert feishu_count == 1


def test_get_history_strict_channel_matching(tmp_path: Path):
    """Phase 9 P0.6: Strict channel matching enforced, legacy messages no longer visible."""
    db = Database(tmp_path / "p0.db")
    # Simulate legacy data: insert directly with channel_name='legacy'
    db.execute(
        "INSERT INTO messages (chat_id, agent_id, role, content, created_at, channel_name) "
        "VALUES ('c', 'a', 'user', 'old message', 1000, 'legacy')"
    )

    sm = SessionManager(db)
    history = sm.get_history("c", "a", channel_name="cli")
    contents = [m.get("content", "") for m in history]
    # Legacy rows should NOT be visible after strict phase enforcement
    assert not any("old message" in c for c in contents)
