"""Tests for the sandbox_mode safe/bypass switch."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from mini_claw.config import PermissionsConfig
from mini_claw.permissions.gate import PermissionGate
from mini_claw.permissions.policy import PermissionPolicy
from mini_claw.tools.builtin import (
    TOOL_LIST_DIRECTORY,
    TOOL_READ_FILE,
    TOOL_WRITE_FILE,
)
from mini_claw.tools.registry import ToolContext


# ---------------------------------------------------------------------------
# Tool-layer behavior in safe vs bypass
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def outside_file() -> Path:
    """A file *outside* the workspace tmp_path (in system temp root)."""
    fd, path_str = tempfile.mkstemp(prefix="mc_outside_", suffix=".txt")
    os.write(fd, b"outside content")
    os.close(fd)
    yield Path(path_str)
    try:
        Path(path_str).unlink()
    except OSError:
        pass


def test_safe_mode_blocks_outside_read(workspace, outside_file):
    ctx = ToolContext(workspace_dir=workspace, sandbox_mode="safe")
    result = asyncio.run(TOOL_READ_FILE.handler(path=str(outside_file), ctx=ctx))
    # Sprint 1.3: tier-2 obfuscation rewrites "escapes workspace" -> "Path outside workspace"
    assert "Path outside workspace" in result


def test_bypass_mode_allows_outside_read(workspace, outside_file):
    ctx = ToolContext(workspace_dir=workspace, sandbox_mode="bypass")
    result = asyncio.run(TOOL_READ_FILE.handler(path=str(outside_file), ctx=ctx))
    assert result == "outside content"


def test_safe_mode_blocks_sensitive_inside_workspace(workspace):
    (workspace / ".env").write_text("SECRET=1")
    ctx = ToolContext(workspace_dir=workspace, sandbox_mode="safe")
    result = asyncio.run(TOOL_READ_FILE.handler(path=".env", ctx=ctx))
    # Sprint 1.3: tier-3 obfuscation rewrites sensitive errors -> "Access denied"
    assert "Access denied" in result or "denied" in result.lower()


def test_bypass_mode_reads_sensitive_files(workspace):
    (workspace / ".env").write_text("SECRET=1")
    ctx = ToolContext(workspace_dir=workspace, sandbox_mode="bypass")
    result = asyncio.run(TOOL_READ_FILE.handler(path=".env", ctx=ctx))
    assert result == "SECRET=1"


def test_safe_mode_blocks_sensitive_directory_listing(workspace):
    """Phase 0.5: list_directory must check assert_not_sensitive, same as read/write."""
    ssh_dir = workspace / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "known_hosts").write_text("example.com")
    ctx = ToolContext(workspace_dir=workspace, sandbox_mode="safe")
    result = asyncio.run(TOOL_LIST_DIRECTORY.handler(path=".ssh", ctx=ctx))
    # Sprint 1.3: tier-3 obfuscation rewrites sensitive errors -> "Access denied"
    assert "Access denied" in result or "denied" in result.lower()


def test_bypass_mode_lists_sensitive_directory(workspace):
    """Phase 0.5: bypass mode allows listing .ssh, same as reading .env."""
    ssh_dir = workspace / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "known_hosts").write_text("example.com")
    ctx = ToolContext(workspace_dir=workspace, sandbox_mode="bypass")
    result = asyncio.run(TOOL_LIST_DIRECTORY.handler(path=".ssh", ctx=ctx))
    assert "known_hosts" in result
    assert "[ERROR]" not in result


def test_bypass_mode_writes_outside_workspace(workspace, tmp_path):
    target = tmp_path.parent / "mc_bypass_write.txt"
    ctx = ToolContext(workspace_dir=workspace, sandbox_mode="bypass")
    try:
        result = asyncio.run(
            TOOL_WRITE_FILE.handler(path=str(target), content="ok", ctx=ctx)
        )
        assert "Written" in result
        assert target.read_text() == "ok"
    finally:
        if target.exists():
            target.unlink()


def test_safe_mode_blocks_outside_write(workspace, tmp_path):
    target = tmp_path.parent / "mc_safe_write_blocked.txt"
    ctx = ToolContext(workspace_dir=workspace, sandbox_mode="safe")
    result = asyncio.run(
        TOOL_WRITE_FILE.handler(path=str(target), content="bad", ctx=ctx)
    )
    # Sprint 1.3: tier-2 obfuscation rewrites "escapes workspace" -> "Path outside workspace"
    assert "Path outside workspace" in result
    assert not target.exists()


def test_bypass_mode_lists_outside_dir(workspace, tmp_path):
    ctx = ToolContext(workspace_dir=workspace, sandbox_mode="bypass")
    # Listing the parent of workspace is outside workspace.
    result = asyncio.run(TOOL_LIST_DIRECTORY.handler(path=str(tmp_path.parent), ctx=ctx))
    assert "[ERROR]" not in result or "escapes" not in result


# ---------------------------------------------------------------------------
# PermissionGate behavior in safe vs bypass
# ---------------------------------------------------------------------------


@pytest.fixture
def gate(tmp_path: Path) -> PermissionGate:
    from mini_claw.permissions.approval_store import ApprovalStore
    from mini_claw.storage.db import Database

    db = Database(tmp_path / "gate_test.db")
    db.init_tables()
    approval_store = ApprovalStore(db)
    return PermissionGate(PermissionPolicy(PermissionsConfig()), approval_store)


def test_gate_safe_mode_blocks_sensitive(gate):
    decision = gate.evaluate(
        "read_file", {"path": ".env"}, {"sandbox_mode": "safe", "level": "L0"}
    )
    assert decision.action == "deny"
    # Sprint 1.3: gate now returns obfuscated reason; check internal_reason for "sensitive"
    assert "sensitive" in decision.internal_reason.lower()


def test_gate_bypass_mode_allows_sensitive(gate):
    decision = gate.evaluate(
        "read_file", {"path": ".env"}, {"sandbox_mode": "bypass", "level": "L0"}
    )
    assert decision.action == "allow"


def test_gate_blacklist_still_active_in_bypass(gate):
    """Bash blacklist must always fire as a final safety net."""
    decision = gate.evaluate(
        "run_shell",
        {"command": "rm -rf /"},
        {"sandbox_mode": "bypass", "level": "L2"},
    )
    assert decision.action == "deny"
    # Sprint 1.3: gate now returns obfuscated reason; check internal_reason for "blacklist"
    assert "blacklist" in decision.internal_reason.lower()
