"""Tests for workspace_dir path normalization in RAG scope checks.

These tests verify that workspace_dir comparison handles unresolved paths
correctly. Without normalization, a workspace stored as "X/workspaces/../.."
would not match the same path passed in as "X" via ctx.workspace_dir,
causing false cross-workspace denials.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mini_claw.rag.permissions import check_search_scope


@pytest.fixture
def mock_config():
    """Mock RagConfig with cross-workspace sharing disabled (default)."""
    config = MagicMock()
    config.sharing.allow_cross_agent_context = False
    config.sharing.allow_workspace_context_sharing = False
    return config


def test_unresolved_workspace_path_matches_resolved(tmp_path, mock_config):
    """When stored path has '..' segments, it should still match its resolved form."""
    # Stored workspace_dir contains unresolved relative segments
    target = tmp_path / "real_workspace"
    target.mkdir()
    unresolved = tmp_path / "decoy" / ".." / "real_workspace"

    scope_filter = {"workspace_dir": str(unresolved)}
    ctx = {
        "agent_id": "default",
        "workspace_dir": str(target),
    }

    allowed, reason = check_search_scope(scope_filter, ctx, mock_config)
    assert allowed, f"Expected allow, got deny: {reason}"


def test_truly_different_workspaces_still_denied(tmp_path, mock_config):
    """Genuinely different workspaces should still be denied."""
    workspace_a = tmp_path / "workspace_a"
    workspace_a.mkdir()
    workspace_b = tmp_path / "workspace_b"
    workspace_b.mkdir()

    scope_filter = {"workspace_dir": str(workspace_a)}
    ctx = {
        "agent_id": "default",
        "workspace_dir": str(workspace_b),
    }

    allowed, reason = check_search_scope(scope_filter, ctx, mock_config)
    assert not allowed
    assert "cross-workspace" in reason


def test_path_object_vs_string_comparison(tmp_path, mock_config):
    """Path objects and strings of the same path should be treated as equal."""
    target = tmp_path / "ws"
    target.mkdir()

    scope_filter = {"workspace_dir": Path(target)}
    ctx = {
        "agent_id": "default",
        "workspace_dir": str(target),
    }

    allowed, reason = check_search_scope(scope_filter, ctx, mock_config)
    assert allowed, f"Expected allow, got deny: {reason}"


def test_missing_workspace_dir_skips_check(mock_config):
    """If scope_filter has no workspace_dir, the check should pass."""
    scope_filter = {}
    ctx = {
        "agent_id": "default",
        "workspace_dir": "/some/path",
    }

    allowed, reason = check_search_scope(scope_filter, ctx, mock_config)
    assert allowed, f"Expected allow, got deny: {reason}"


def test_workspace_normalize_handles_invalid_path(mock_config):
    """Malformed paths should fall back to string comparison gracefully."""
    # Use a string that resolve() would handle but might have edge cases
    scope_filter = {"workspace_dir": "not-a-real-path"}
    ctx = {
        "agent_id": "default",
        "workspace_dir": "not-a-real-path",
    }

    # Same string -> should pass even if resolve gives weird result
    allowed, reason = check_search_scope(scope_filter, ctx, mock_config)
    assert allowed, f"Expected allow for identical strings, got: {reason}"


def test_workspace_manager_resolves_relative_paths(tmp_path):
    """WorkspaceManager.load_workspaces should resolve '../..' style paths."""
    from mini_claw.agent.workspace import WorkspaceManager
    from mini_claw.config import AgentConfig

    base = tmp_path / "data" / "workspaces"
    base.mkdir(parents=True)

    agent_cfg = AgentConfig(
        id="default",
        workspace="../..",  # User's config style
    )

    wm = WorkspaceManager(base_dir=base)
    wm.load_workspaces([agent_cfg])

    ws_dir = wm.get_workspace("any_chat", "default")

    # Should be resolved (no '..' segments)
    assert ".." not in str(ws_dir)
    # Should match the actual resolved path
    expected = (base / "../..").resolve()
    assert ws_dir == expected
