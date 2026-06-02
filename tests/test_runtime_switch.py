"""Tests for runtime sandbox mode switching via /bypass and /safe commands."""

from __future__ import annotations

from pathlib import Path

import pytest

from mini_claw.config import AppConfig, PermissionsConfig
from mini_claw.gateway.session import SessionManager
from mini_claw.storage.db import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    db_path = tmp_path / "test.db"
    database = Database(db_path)
    database.init_tables()
    return database


@pytest.fixture
def session_mgr(db: Database) -> SessionManager:
    return SessionManager(db)


def test_session_sandbox_override_default_none(session_mgr):
    session_mgr.get_or_create("chat1", "agent1")
    mode = session_mgr.get_sandbox_mode("chat1", "agent1")
    assert mode is None


def test_session_set_bypass_mode(session_mgr):
    session_mgr.get_or_create("chat1", "agent1")
    session_mgr.set_sandbox_mode("chat1", "agent1", "bypass")
    mode = session_mgr.get_sandbox_mode("chat1", "agent1")
    assert mode == "bypass"


def test_session_set_safe_mode(session_mgr):
    session_mgr.get_or_create("chat1", "agent1")
    session_mgr.set_sandbox_mode("chat1", "agent1", "safe")
    mode = session_mgr.get_sandbox_mode("chat1", "agent1")
    assert mode == "safe"


def test_session_switch_bypass_to_safe(session_mgr):
    session_mgr.get_or_create("chat1", "agent1")
    session_mgr.set_sandbox_mode("chat1", "agent1", "bypass")
    assert session_mgr.get_sandbox_mode("chat1", "agent1") == "bypass"
    session_mgr.set_sandbox_mode("chat1", "agent1", "safe")
    assert session_mgr.get_sandbox_mode("chat1", "agent1") == "safe"


def test_session_clear_override(session_mgr):
    """Setting mode to None clears the override, falls back to config default."""
    session_mgr.get_or_create("chat1", "agent1")
    session_mgr.set_sandbox_mode("chat1", "agent1", "bypass")
    assert session_mgr.get_sandbox_mode("chat1", "agent1") == "bypass"
    session_mgr.set_sandbox_mode("chat1", "agent1", None)
    assert session_mgr.get_sandbox_mode("chat1", "agent1") is None


def test_sessions_independent(session_mgr):
    """Each chat_id has its own sandbox_mode_override."""
    session_mgr.get_or_create("chat1", "agent1")
    session_mgr.get_or_create("chat2", "agent1")
    session_mgr.set_sandbox_mode("chat1", "agent1", "bypass")
    session_mgr.set_sandbox_mode("chat2", "agent1", "safe")
    assert session_mgr.get_sandbox_mode("chat1", "agent1") == "bypass"
    assert session_mgr.get_sandbox_mode("chat2", "agent1") == "safe"
