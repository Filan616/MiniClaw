"""Tests for Phase 8 M3: active context (use/clear), command dispatch, role profiles."""

from __future__ import annotations

from pathlib import Path

import pytest

from mini_claw.config import AppConfig, RagConfig
from mini_claw.permissions.policy import PermissionPolicy
from mini_claw.rag.manager import RagManager
from mini_claw.storage.db import Database
from mini_claw.workflow.role_profiles import ROLE_PROFILES


@pytest.fixture
def config() -> RagConfig:
    cfg = RagConfig()
    cfg.enabled = True
    cfg.namespaces.context_enabled = True
    return cfg


@pytest.fixture
def storage(tmp_path: Path) -> Database:
    return Database(tmp_path / "active.db")


@pytest.fixture
def manager(storage, config) -> RagManager:
    return RagManager(storage, config, PermissionPolicy(AppConfig().permissions))


def _ctx(workspace_dir: Path, agent_id: str = "agent-a", session_id: str = "sess-1") -> dict:
    return {
        "agent_id": agent_id,
        "workspace_dir": str(workspace_dir),
        "sandbox_mode": "safe",
        "chat_id": "chat-1",
        "session_id": session_id,
        "channel_name": "cli",
    }


def _index(manager: RagManager, tmp_path: Path) -> str:
    p = tmp_path / "doc.md"
    p.write_text("# title\nbody\n", encoding="utf-8")
    item_id, _ = manager.index_context(str(p), ctx=_ctx(tmp_path), title="Doc")
    return item_id


# ===================== use_context =====================


def test_use_context_sets_active(manager: RagManager, tmp_path: Path):
    item_id = _index(manager, tmp_path)
    success, error = manager.use_context(item_id, ctx=_ctx(tmp_path))
    assert success, error
    actives = manager.store.get_active_contexts("sess-1", "agent-a")
    assert len(actives) == 1
    assert actives[0].context_id == item_id
    assert actives[0].title == "Doc"


def test_use_context_blocks_cross_agent(manager: RagManager, tmp_path: Path):
    item_id = _index(manager, tmp_path)
    success, error = manager.use_context(
        item_id, ctx=_ctx(tmp_path, agent_id="agent-b")
    )
    assert not success
    assert "another agent" in error.lower()


def test_use_context_isolated_per_session(manager: RagManager, tmp_path: Path):
    item_id = _index(manager, tmp_path)
    manager.use_context(item_id, ctx=_ctx(tmp_path, session_id="sess-1"))
    # Different session → no active
    actives_other = manager.store.get_active_contexts("sess-2", "agent-a")
    assert actives_other == []


def test_clear_context_removes_active(manager: RagManager, tmp_path: Path):
    item_id = _index(manager, tmp_path)
    manager.use_context(item_id, ctx=_ctx(tmp_path))
    count, error = manager.clear_context(ctx=_ctx(tmp_path))
    assert error == ""
    assert count == 1
    assert manager.store.get_active_contexts("sess-1", "agent-a") == []


# ===================== cleanup_lifecycle =====================


def test_cleanup_lifecycle_returns_counts(manager: RagManager, tmp_path: Path):
    counts = manager.cleanup_lifecycle()
    # Should return a dict (possibly all zeros for fresh DB)
    assert isinstance(counts, dict)
    assert "warm" in counts
    assert "deleted" in counts


def test_cleanup_lifecycle_disabled_returns_empty(tmp_path: Path):
    """When RAG is disabled, cleanup_lifecycle returns {}."""
    cfg = RagConfig()  # enabled=False default
    db = Database(tmp_path / "off.db")
    mgr = RagManager(db, cfg, PermissionPolicy(AppConfig().permissions))
    assert mgr.cleanup_lifecycle() == {}


# ===================== role profiles include search_context =====================


def test_researcher_role_has_search_context():
    """M3: researcher role gets search_context / list_contexts in default tools."""
    profile = ROLE_PROFILES["researcher"]
    assert "search_context" in profile.default_tools
    assert "list_contexts" in profile.default_tools


def test_security_reviewer_role_has_search_context():
    profile = ROLE_PROFILES["security_reviewer"]
    assert "search_context" in profile.default_tools


def test_tester_role_has_search_context():
    profile = ROLE_PROFILES["tester"]
    assert "search_context" in profile.default_tools


def test_implementer_role_has_search_context():
    profile = ROLE_PROFILES["implementer"]
    assert "search_context" in profile.default_tools


def test_summarizer_role_unchanged():
    """Summarizer must remain tool-free."""
    profile = ROLE_PROFILES["summarizer"]
    assert profile.default_tools == []


def test_prompt_reviewer_role_unchanged():
    """prompt_reviewer must remain tool-free."""
    profile = ROLE_PROFILES["prompt_reviewer"]
    assert profile.default_tools == []
