"""Tests for the permissions system."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from mini_claw.permissions.levels import L0, L1, L2, L3, L4
from mini_claw.permissions.policy import PermissionPolicy
from mini_claw.permissions.gate import Decision, PermissionGate
from mini_claw.config import PermissionsConfig, PermissionsHighRiskConfig


@pytest.fixture
def policy():
    cfg = PermissionsConfig()
    return PermissionPolicy(cfg)


@pytest.fixture
def gate(policy):
    storage = MagicMock()
    storage.has_session_grant = MagicMock(return_value=False)
    return PermissionGate(policy, storage)


class TestPolicy:
    def test_blacklist_rm_rf(self, policy):
        assert policy.is_blacklisted("rm -rf /")

    def test_blacklist_safe_command(self, policy):
        assert not policy.is_blacklisted("ls -la")

    def test_path_in_workspace(self, policy):
        workspace = Path("/home/user/project")
        assert policy.path_in_workspace("/home/user/project/src/main.py", workspace)

    def test_path_escape_detected(self, policy):
        workspace = Path("/home/user/project")
        assert not policy.path_in_workspace("/etc/passwd", workspace)

    def test_blacklist_fork_bomb(self, policy):
        assert policy.is_blacklisted(":(){ :|:& };:")

    def test_mkfs_blocked(self, policy):
        assert policy.is_blacklisted("mkfs.ext4 /dev/sda1")


class TestPermissionGate:
    def test_allow_l0_tool(self, gate):
        tool = MagicMock()
        tool.name = "read_file"
        tool.permission_level = L0
        ctx = MagicMock()
        ctx.workspace_dir = Path("/home/user/project")
        decision = gate.evaluate(tool, {"path": "/home/user/project/f.txt"}, ctx)
        assert decision.action == "allow"

    def test_deny_l4_tool(self, gate):
        tool = MagicMock()
        tool.name = "dangerous_op"
        tool.permission_level = L4
        ctx = MagicMock()
        decision = gate.evaluate(tool, {}, ctx)
        assert decision.action == "deny"

    def test_l3_needs_approval(self, gate):
        tool = MagicMock()
        tool.name = "send_message"
        tool.permission_level = L3
        ctx = MagicMock()
        ctx.chat_id = "chat_001"
        ctx.agent_id = "default"
        decision = gate.evaluate(tool, {}, ctx)
        assert decision.action == "need_approval"

    def test_shell_blacklist_deny(self, gate):
        tool = MagicMock()
        tool.name = "run_shell"
        tool.permission_level = L2
        ctx = MagicMock()
        ctx.workspace_dir = Path("/home/user/project")
        decision = gate.evaluate(tool, {"cmd": "rm -rf /"}, ctx)
        assert decision.action == "deny"
