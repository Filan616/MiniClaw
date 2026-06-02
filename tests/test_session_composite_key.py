"""Tests for Session 复合主键重建 (Phase C6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mini_claw.gateway.session import SessionManager
from mini_claw.storage.db import Database


@pytest.fixture
def storage(tmp_path: Path) -> Database:
    return Database(tmp_path / "session_composite.db")


@pytest.fixture
def session_mgr(storage: Database) -> SessionManager:
    return SessionManager(storage)


def test_same_chat_id_different_channels_are_isolated(session_mgr: SessionManager, storage: Database):
    """Phase C6: Same chat_id on different channels should produce independent sessions."""
    # Create session on feishu
    feishu_session = session_mgr.get_or_create("chat_001", "agent_a", channel_name="feishu")
    # Create session on cli with same chat_id and agent_id
    cli_session = session_mgr.get_or_create("chat_001", "agent_a", channel_name="cli")

    assert feishu_session["channel_name"] == "feishu"
    assert cli_session["channel_name"] == "cli"

    # Both rows should exist independently
    rows = storage.fetchall(
        "SELECT channel_name FROM sessions WHERE chat_id = ? AND agent_id = ?",
        ("chat_001", "agent_a"),
    )
    channels = sorted(r["channel_name"] for r in rows)
    assert channels == ["cli", "feishu"]


def test_sandbox_mode_isolated_per_channel(session_mgr: SessionManager):
    """Phase C6: sandbox_mode_override on one channel doesn't leak to another."""
    session_mgr.get_or_create("chat_002", "agent_b", channel_name="feishu")
    session_mgr.get_or_create("chat_002", "agent_b", channel_name="cli")

    # Set bypass on feishu only
    session_mgr.set_sandbox_mode("chat_002", "agent_b", "bypass", channel_name="feishu")

    # feishu should be bypass
    feishu_mode = session_mgr.get_sandbox_mode("chat_002", "agent_b", channel_name="feishu")
    assert feishu_mode == "bypass"

    # cli should be unaffected (NULL override)
    cli_mode = session_mgr.get_sandbox_mode("chat_002", "agent_b", channel_name="cli")
    assert cli_mode is None


def test_composite_index_exists(storage: Database):
    """Phase C6: Composite unique index should be created."""
    rows = storage.fetchall(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name='idx_sessions_composite'"
    )
    # Either as composite UNIQUE INDEX or built into the PK
    pk_info = storage.fetchall("PRAGMA table_info(sessions)")
    pk_columns = [r["name"] for r in pk_info if r["pk"] > 0]
    has_composite_pk = (
        "channel_name" in pk_columns
        and "chat_id" in pk_columns
        and "agent_id" in pk_columns
    )
    has_composite_index = len(rows) > 0
    assert has_composite_pk or has_composite_index


def test_get_or_create_idempotent_per_channel(session_mgr: SessionManager, storage: Database):
    """Phase C6: get_or_create returns same row for same (channel, chat, agent)."""
    s1 = session_mgr.get_or_create("chat_003", "agent_c", channel_name="feishu")
    s2 = session_mgr.get_or_create("chat_003", "agent_c", channel_name="feishu")

    assert s1["chat_id"] == s2["chat_id"]
    assert s1["channel_name"] == s2["channel_name"]
    # created_at should be the same (no duplicate insertion)
    assert s1["created_at"] == s2["created_at"]

    # Only one row should exist
    rows = storage.fetchall(
        "SELECT * FROM sessions WHERE channel_name=? AND chat_id=? AND agent_id=?",
        ("feishu", "chat_003", "agent_c"),
    )
    assert len(rows) == 1


def test_effective_sandbox_mode_per_channel(session_mgr: SessionManager):
    """Phase C6: get_effective_sandbox_mode respects channel_name."""
    session_mgr.get_or_create("chat_004", "agent_d", channel_name="feishu")
    session_mgr.get_or_create("chat_004", "agent_d", channel_name="cli")

    # Set bypass with TTL on feishu
    import time
    expires = int(time.time()) + 3600
    session_mgr.set_bypass_mode("chat_004", "agent_d", "bypass", expires, channel_name="feishu")

    feishu = session_mgr.get_effective_sandbox_mode("chat_004", "agent_d", channel_name="feishu")
    cli = session_mgr.get_effective_sandbox_mode("chat_004", "agent_d", channel_name="cli")

    assert feishu == "bypass"
    assert cli == "safe"  # No override, defaults to safe
