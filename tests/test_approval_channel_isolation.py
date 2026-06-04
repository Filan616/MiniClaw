"""Test Phase 9 P0.4: pending_approvals strict channel isolation.

Verifies that resolve_pending and get_pending enforce strict channel_name
matching without legacy NULL/cross-channel fallback.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mini_claw.permissions.approval_store import ApprovalStore
from mini_claw.storage.db import Database


@pytest.fixture
def storage(tmp_path: Path) -> Database:
    return Database(tmp_path / "approval_isolation.db")


@pytest.fixture
def approval_store(storage: Database) -> ApprovalStore:
    return ApprovalStore(storage)


def test_resolve_pending_strict_channel_isolation(
    approval_store: ApprovalStore,
) -> None:
    """resolve_pending with channel_name must NOT match approvals from other channels."""
    # Create approval in channel A
    approval_store.create_pending(
        approval_id="approval-123",
        run_id="run-1",
        chat_id="chat-x",
        agent_id="agent-a",
        tool_name="write_file",
        tool_args={"path": "/tmp/test"},
        expires_at=9999999999,
        channel_name="cli",
    )

    # Try to resolve from channel B
    result = approval_store.resolve_pending(
        "approval-123", "approved", channel_name="feishu"
    )

    # Should NOT find it (strict isolation)
    assert result is None, "Approval from channel 'cli' leaked into channel 'feishu'"


def test_resolve_pending_same_channel_succeeds(
    approval_store: ApprovalStore,
) -> None:
    """resolve_pending with matching channel_name should succeed."""
    approval_store.create_pending(
        approval_id="approval-456",
        run_id="run-2",
        chat_id="chat-y",
        agent_id="agent-b",
        tool_name="delete_file",
        tool_args={"path": "/tmp/test2"},
        expires_at=9999999999,
        channel_name="feishu",
    )

    # Resolve from same channel
    result = approval_store.resolve_pending(
        "approval-456", "approved", channel_name="feishu"
    )

    assert result is not None
    assert result["status"] == "approved"
    assert result["tool_call"]["tool"] == "delete_file"


def test_get_pending_strict_channel_isolation(
    approval_store: ApprovalStore,
) -> None:
    """get_pending with channel_name must NOT match approvals from other channels."""
    approval_store.create_pending(
        approval_id="approval-789",
        run_id="run-3",
        chat_id="chat-z",
        agent_id="agent-c",
        tool_name="execute_bash",
        tool_args={"command": "ls"},
        expires_at=9999999999,
        channel_name="cli",
    )

    # Try to get from different channel
    result = approval_store.get_pending("approval-789", channel_name="web")

    # Should NOT find it
    assert result is None, "Approval from channel 'cli' leaked into channel 'web'"


def test_get_pending_same_channel_succeeds(
    approval_store: ApprovalStore,
) -> None:
    """get_pending with matching channel_name should succeed."""
    approval_store.create_pending(
        approval_id="approval-999",
        run_id="run-4",
        chat_id="chat-w",
        agent_id="agent-d",
        tool_name="network_request",
        tool_args={"url": "https://example.com"},
        expires_at=9999999999,
        channel_name="slack",
    )

    # Get from same channel
    result = approval_store.get_pending("approval-999", channel_name="slack")

    assert result is not None
    assert result["approval_id"] == "approval-999"
    assert result["tool_name"] == "network_request"
    assert result["channel_name"] == "slack"


def test_resolve_pending_without_channel_finds_any(
    approval_store: ApprovalStore,
) -> None:
    """resolve_pending without channel_name parameter should find any approval (legacy compat)."""
    approval_store.create_pending(
        approval_id="approval-compat",
        run_id="run-5",
        chat_id="chat-v",
        agent_id="agent-e",
        tool_name="tool_x",
        tool_args={},
        expires_at=9999999999,
        channel_name="discord",
    )

    # Resolve without channel filter (internal/admin use case)
    result = approval_store.resolve_pending("approval-compat", "approved")

    assert result is not None
    assert result["status"] == "approved"


def test_get_pending_without_channel_finds_any(
    approval_store: ApprovalStore,
) -> None:
    """get_pending without channel_name parameter should find any approval (legacy compat)."""
    approval_store.create_pending(
        approval_id="approval-compat2",
        run_id="run-6",
        chat_id="chat-u",
        agent_id="agent-f",
        tool_name="tool_y",
        tool_args={},
        expires_at=9999999999,
        channel_name="telegram",
    )

    # Get without channel filter
    result = approval_store.get_pending("approval-compat2")

    assert result is not None
    assert result["approval_id"] == "approval-compat2"
