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
        ctx = {"level": L0, "workspace_dir": Path("/home/user/project")}
        decision = gate.evaluate(
            "read_file", {"path": "/home/user/project/f.txt"}, ctx
        )
        assert decision.action == "allow"

    def test_deny_l4_tool(self, gate):
        ctx = {"level": L4}
        decision = gate.evaluate("dangerous_op", {}, ctx)
        assert decision.action == "deny"

    def test_l3_needs_approval(self, gate):
        ctx = {"level": L3, "chat_id": "chat_001", "agent_id": "default"}
        decision = gate.evaluate("send_message", {}, ctx)
        assert decision.action == "need_approval"

    def test_shell_blacklist_deny(self, gate):
        ctx = {"level": L2, "workspace_dir": Path("/home/user/project")}
        decision = gate.evaluate("run_shell", {"cmd": "rm -rf /"}, ctx)
        assert decision.action == "deny"
