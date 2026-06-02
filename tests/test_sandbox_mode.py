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
    assert "escapes workspace" in result


def test_bypass_mode_allows_outside_read(workspace, outside_file):
    ctx = ToolContext(workspace_dir=workspace, sandbox_mode="bypass")
    result = asyncio.run(TOOL_READ_FILE.handler(path=str(outside_file), ctx=ctx))
    assert result == "outside content"


def test_safe_mode_blocks_sensitive_inside_workspace(workspace):
    (workspace / ".env").write_text("SECRET=1")
    ctx = ToolContext(workspace_dir=workspace, sandbox_mode="safe")
    result = asyncio.run(TOOL_READ_FILE.handler(path=".env", ctx=ctx))
    assert "sensitive" in result.lower()


def test_bypass_mode_reads_sensitive_files(workspace):
    (workspace / ".env").write_text("SECRET=1")
    ctx = ToolContext(workspace_dir=workspace, sandbox_mode="bypass")
    result = asyncio.run(TOOL_READ_FILE.handler(path=".env", ctx=ctx))
    assert result == "SECRET=1"


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
    assert "escapes workspace" in result
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
def gate() -> PermissionGate:
    return PermissionGate(PermissionPolicy(PermissionsConfig()))


def test_gate_safe_mode_blocks_sensitive(gate):
    decision = gate.evaluate(
        "read_file", {"path": ".env"}, {"sandbox_mode": "safe", "level": "L0"}
    )
    assert decision.action == "deny"
    assert "sensitive" in decision.reason.lower()


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
    assert "blacklist" in decision.reason.lower()
