"""Tests for ApprovalStore persistence (Phase 0.2)."""

from pathlib import Path

import pytest

from mini_claw.permissions.approval_store import ApprovalStore
from mini_claw.permissions.gate import PermissionGate
from mini_claw.permissions.policy import PermissionPolicy
from mini_claw.config import PermissionsConfig
from mini_claw.storage.db import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    db_path = tmp_path / "test_approval.db"
    database = Database(db_path)
    database.init_tables()
    return database


@pytest.fixture
def approval_store(db: Database) -> ApprovalStore:
    return ApprovalStore(db)


def test_approval_store_create_and_resolve(approval_store):
    """ApprovalStore can create and resolve pending approvals."""
    approval_store.create_pending(
        approval_id="test_approval_1",
        run_id="run_1",
        chat_id="chat_1",
        agent_id="agent_1",
        tool_name="run_shell",
        tool_args={"command": "ls"},
        expires_at=9999999999,  # Far future
    )

    result = approval_store.resolve_pending("test_approval_1", "approved")
    assert result is not None
    assert result["status"] == "approved"
    assert result["run_id"] == "run_1"
    assert result["tool_call"]["tool"] == "run_shell"


def test_approval_store_resolve_expired(approval_store):
    """ApprovalStore auto-expires approvals past their TTL."""
    approval_store.create_pending(
        approval_id="test_approval_2",
        run_id="run_2",
        chat_id="chat_2",
        agent_id="agent_2",
        tool_name="write_file",
        tool_args={"path": "test.txt"},
        expires_at=1,  # 1970-01-01, definitely expired
    )

    result = approval_store.resolve_pending("test_approval_2", "approved")
    assert result is not None
    assert result["status"] == "expired"


def test_approval_store_session_grant(approval_store):
    """ApprovalStore can grant and check session permissions."""
    approval_store.grant_session("chat_3", "agent_3", "run_shell", expires_at=9999999999)

    has_grant = approval_store.has_session_grant("chat_3", "agent_3", "run_shell")
    assert has_grant is True

    # Different tool should not have grant
    has_grant = approval_store.has_session_grant("chat_3", "agent_3", "write_file")
    assert has_grant is False


def test_approval_store_session_grant_expires(approval_store):
    """ApprovalStore respects grant expiry."""
    approval_store.grant_session("chat_4", "agent_4", "run_shell", expires_at=1)

    has_grant = approval_store.has_session_grant("chat_4", "agent_4", "run_shell")
    assert has_grant is False


def test_approval_persistence_across_instances(tmp_path):
    """Phase 0.2: pending approvals and grants survive PermissionGate destruction.

    This is the key test — create a Gate, create an approval, destroy Gate,
    rebuild Gate with same DB, resolve should still work.
    """
    db_path = tmp_path / "persist.db"
    db = Database(db_path)
    db.init_tables()

    # First instance: create pending approval
    approval_store_1 = ApprovalStore(db)
    policy_1 = PermissionPolicy(PermissionsConfig())
    gate_1 = PermissionGate(policy_1, approval_store_1)

    approval_id = gate_1.create_pending(
        run_id="run_persist",
        chat_id="chat_persist",
        agent_id="agent_persist",
        tool_call={"tool": "run_shell", "args": {"command": "echo hi"}},
        ttl=3600,
    )

    # Grant session
    gate_1.grant_session(
        {"chat_id": "chat_persist", "agent_id": "agent_persist"},
        "write_file",
        ttl=3600,
    )

    # Destroy first instance
    del gate_1, approval_store_1

    # Second instance: should be able to resolve
    db2 = Database(db_path)
    approval_store_2 = ApprovalStore(db2)
    policy_2 = PermissionPolicy(PermissionsConfig())
    gate_2 = PermissionGate(policy_2, approval_store_2)

    result = gate_2.resolve(approval_id, "approved")
    assert result is not None
    assert result["status"] == "approved"
    assert result["chat_id"] == "chat_persist"

    # Session grant should also persist
    has_grant = approval_store_2.has_session_grant("chat_persist", "agent_persist", "write_file")
    assert has_grant is True


def test_gate_evaluate_with_session_grant(tmp_path):
    """PermissionGate.evaluate respects persistent session grants."""
    db_path = tmp_path / "eval_grant.db"
    db = Database(db_path)
    db.init_tables()

    approval_store = ApprovalStore(db)
    policy = PermissionPolicy(PermissionsConfig())
    gate = PermissionGate(policy, approval_store)

    # Grant session for a tool
    gate.grant_session(
        {"chat_id": "chat_eval", "agent_id": "agent_eval"},
        "run_shell",
        ttl=3600,
    )

    # Evaluate: should return allow due to session grant
    decision = gate.evaluate(
        "run_shell",
        {"command": "echo test"},
        {"level": "L3", "chat_id": "chat_eval", "agent_id": "agent_eval"},
    )
    assert decision.action == "allow"
    assert "session grant" in decision.reason


def test_expire_pending_cleanup(approval_store):
    """expire_pending removes old approvals."""
    import time

    # Create one old approval
    approval_store.create_pending(
        approval_id="old_approval",
        run_id="run_old",
        chat_id="chat_old",
        agent_id="agent_old",
        tool_name="run_shell",
        tool_args={"command": "ls"},
        expires_at=int(time.time()) - 100000,  # Old timestamp
    )

    count = approval_store.expire_pending(86400)
    assert count == 1

    # Try to resolve, should return None or expired
    result = approval_store.get_pending("old_approval")
    assert result is None or result["status"] == "expired"
