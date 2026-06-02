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


# ---------------------------------------------------------------------------
# Phase 0.4: TTL semantics — get_effective_sandbox_mode should be the
# single source of truth for both handle_message and handle_approval. An
# expired bypass must auto-revert to "safe".
# ---------------------------------------------------------------------------


def test_effective_sandbox_mode_expired_ttl_reverts_to_safe(session_mgr):
    """A bypass whose expires_at is in the past must resolve to 'safe'.

    Direct SQL is used because set_bypass_mode lets us write the expiry, but
    we want to verify that the read path itself sweeps the row back to safe
    rather than letting handle_approval consume a stale grant.
    """
    session_mgr.get_or_create("chat_expired", "agent1")
    # 1-second-ago expiry — must be treated as expired.
    session_mgr.set_bypass_mode(
        "chat_expired", "agent1", "bypass", expires_at=1
    )
    effective = session_mgr.get_effective_sandbox_mode(
        "chat_expired", "agent1"
    )
    assert effective == "safe"
    # Sweep is persistent: the row should now be reset.
    assert session_mgr.get_sandbox_mode("chat_expired", "agent1") == "safe"


def test_effective_sandbox_mode_future_ttl_stays_bypass(session_mgr):
    """A future-dated TTL bypass remains active until consumed/expired."""
    import time as _time

    session_mgr.get_or_create("chat_active", "agent1")
    session_mgr.set_bypass_mode(
        "chat_active", "agent1", "bypass", expires_at=int(_time.time()) + 600
    )
    effective = session_mgr.get_effective_sandbox_mode(
        "chat_active", "agent1"
    )
    assert effective == "bypass"


def test_resolve_sandbox_mode_helper_matches_effective(tmp_path):
    """Gateway._resolve_sandbox_mode must delegate to get_effective_sandbox_mode.

    Phase 0.4 introduced the helper specifically so handle_approval cannot
    drift from handle_message. This test pins the contract.
    """
    from mini_claw.gateway.router import Gateway

    db_path = tmp_path / "resolve.db"
    db = Database(db_path)
    db.init_tables()
    session = SessionManager(db)
    session.get_or_create("c", "a")
    session.set_bypass_mode("c", "a", "bypass", expires_at=1)  # expired

    gw = Gateway.__new__(Gateway)  # bypass __init__
    gw._session_mgr = session
    assert gw._resolve_sandbox_mode("c", "a") == "safe"

    # And for an active bypass:
    import time as _time

    session.set_bypass_mode(
        "c", "a", "bypass", expires_at=int(_time.time()) + 60
    )
    assert gw._resolve_sandbox_mode("c", "a") == "bypass"
