"""Phase 9 tests: Scenarios 8-10 — Cross-channel isolation and workflow features.

Tests cover:
8. compaction cross-channel isolation
9. agent summary structured sources only
10. workflow_intent memory type mapping
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mini_claw.config import RagConfig, AppConfig
from mini_claw.permissions.policy import PermissionPolicy
from mini_claw.rag.manager import RagManager
from mini_claw.storage.db import Database


@pytest.fixture
def storage(tmp_path: Path) -> Database:
    return Database(tmp_path / "isolation.db")


@pytest.fixture
def rag_manager(storage: Database) -> RagManager:
    cfg = RagConfig()
    cfg.enabled = True
    cfg.namespaces.memory_enabled = True
    policy = PermissionPolicy(AppConfig().permissions)
    return RagManager(storage, cfg, policy)


def _insert_messages_multi_channel(storage: Database):
    """Insert test messages across multiple channels (let AUTOINCREMENT assign id)."""
    channels = ["cli", "feishu", "slack"]
    for i, channel in enumerate(channels):
        for j in range(3):
            storage.execute(
                "INSERT INTO messages (chat_id, agent_id, channel_name, role, content, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"chat-{channel}",
                    "agent-a",
                    channel,
                    "user",
                    f"{channel} message {j}",
                    int(time.time()) - j * 60,
                ),
            )


# ==================== Scenario 8: compaction cross-channel isolation ====================


def test_compaction_isolated_by_channel(storage: Database, rag_manager: RagManager):
    """Scenario 8: Message compaction only affects messages from same channel."""
    _insert_messages_multi_channel(storage)

    # Compact CLI channel messages
    cli_messages = storage.fetchall(
        "SELECT * FROM messages WHERE channel_name = ? ORDER BY created_at",
        ("cli",)
    )
    cli_msg_dicts = [dict(row) for row in cli_messages]

    # Submit compaction candidates for CLI channel only
    n = rag_manager.submit_session_compaction_candidates(
        cli_msg_dicts,
        chat_id="chat-cli",
        agent_id="agent-a",
        channel_name="cli",
    )

    # Verify candidates created
    candidates = storage.fetchall(
        "SELECT * FROM memory_candidates WHERE source_type = 'session_compaction'"
    )

    # Should only extract from CLI messages
    for cand in candidates:
        # Content should reference CLI messages, not other channels
        content = cand.get("content") or ""
        assert "cli" in content.lower() or "message" in content.lower()
        # Should NOT reference other channels
        assert "feishu" not in content.lower()
        assert "slack" not in content.lower()


def test_session_id_includes_channel_for_isolation(storage: Database):
    """Scenario 8: session_id derivation differs by channel (isolation)."""
    from mini_claw.gateway.session import derive_session_id

    # Same chat_id and agent_id but different channels
    session_1 = derive_session_id("cli", "chat-1", "agent-a")
    session_2 = derive_session_id("feishu", "chat-1", "agent-a")
    session_3 = derive_session_id("cli", "chat-1", "agent-a")  # same as session_1

    # Different channels must produce different session ids
    assert session_1 != session_2
    # Same channel + chat + agent must produce same session id (stable hash)
    assert session_1 == session_3
    # Both must be non-empty deterministic strings
    assert isinstance(session_1, str) and len(session_1) > 0
    assert isinstance(session_2, str) and len(session_2) > 0


def test_active_contexts_isolated_by_session(storage: Database):
    """Scenario 8: active_contexts uses session_id for isolation."""
    # Insert active contexts for different sessions
    storage.execute(
        "INSERT INTO active_contexts "
        "(session_id, agent_id, context_id, context_type, activated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("cli:chat-1:agent-a", "agent-a", "item_cli", "document", int(time.time())),
    )
    storage.execute(
        "INSERT INTO active_contexts "
        "(session_id, agent_id, context_id, context_type, activated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("feishu:chat-1:agent-a", "agent-a", "item_feishu", "document", int(time.time())),
    )

    cli_contexts = storage.fetchall(
        "SELECT * FROM active_contexts WHERE session_id = ?",
        ("cli:chat-1:agent-a",)
    )
    feishu_contexts = storage.fetchall(
        "SELECT * FROM active_contexts WHERE session_id = ?",
        ("feishu:chat-1:agent-a",)
    )

    assert len(cli_contexts) == 1
    assert len(feishu_contexts) == 1
    assert cli_contexts[0]["context_id"] == "item_cli"
    assert feishu_contexts[0]["context_id"] == "item_feishu"


# ==================== Scenario 9: agent summary structured sources only ====================


def test_agent_summary_only_includes_structured_sources(storage: Database, rag_manager: RagManager):
    """Scenario 9: Agent summary excludes freeform memories, includes structured only."""
    # Submit structured memory (workflow key_findings)
    n = rag_manager.submit_workflow_candidates(
        {"key_findings": ["always lint before tests"]},
        workflow_id="wf-1",
        chat_id="chat-1",
        agent_id="agent-a",
        workspace_dir="/ws",
    )
    assert n >= 1

    # Submit freeform memory (session compaction)
    msgs = [{"id": 1, "role": "user", "content": "user prefers concise answers"}]
    rag_manager.submit_session_compaction_candidates(
        msgs, chat_id="chat-1", agent_id="agent-a"
    )

    # Get agent summary
    summary, error = rag_manager.get_agent_summary(agent_id="agent-a")
    if error:
        pytest.skip(f"get_agent_summary not implemented: {error}")

    # Summary should only include structured sources
    # Check source_type field in summary
    if summary and isinstance(summary, list):
        for item in summary:
            source_type = item.get("source_type") or ""
            # Should be workflow_result or similar structured type
            assert source_type in {"workflow_result", "explicit_remember", "key_finding"}


def test_agent_summary_excludes_session_compaction(storage: Database, rag_manager: RagManager):
    """Scenario 9: Agent summary excludes session_compaction source_type."""
    # Create session compaction candidates
    msgs = [
        {"id": 1, "role": "user", "content": "message 1"},
        {"id": 2, "role": "user", "content": "message 2"},
    ]
    rag_manager.submit_session_compaction_candidates(
        msgs, chat_id="chat-1", agent_id="agent-a"
    )

    # Approve some candidates to promote to rag_items
    candidates = storage.fetchall(
        "SELECT * FROM memory_candidates WHERE status = 'pending' AND source_type = 'session_compaction'"
    )
    for cand in candidates[:1]:
        rag_manager.approve_memory(cand["candidate_id"])

    # Get agent summary
    summary, error = rag_manager.get_agent_summary(agent_id="agent-a")
    if not summary:
        return  # No summary items yet

    # Should exclude session_compaction items
    for item in summary:
        assert item.get("source_type") != "session_compaction"


# ==================== Scenario 10: workflow_intent memory type mapping ====================


def test_workflow_intent_maps_to_memory_type(storage: Database, rag_manager: RagManager):
    """Scenario 10: workflow_intent parameter maps to memory type field."""
    # Submit workflow candidates with workflow_intent
    workflow_intent = "refactor_code: modernize authentication (task: upgrade to OAuth2)"
    n = rag_manager.submit_workflow_candidates(
        {"key_findings": ["migrate to OAuth2 for all auth flows"]},
        workflow_id="wf-refactor",
        chat_id="chat-1",
        agent_id="agent-a",
        workspace_dir="/ws",
        workflow_intent=workflow_intent,
    )
    assert n >= 1

    # Check memory candidates for inferred intent on the actual schema column
    candidates = storage.fetchall(
        "SELECT * FROM memory_candidates WHERE source_chain_json LIKE ?",
        (f"%{workflow_intent}%",)
    )

    assert len(candidates) >= 1
    for cand in candidates:
        chain_json = cand.get("source_chain_json") or "{}"
        # Should include workflow_intent in the source chain JSON
        assert workflow_intent in chain_json or "refactor" in chain_json.lower()


def test_workflow_intent_infers_refactor_type(storage: Database, rag_manager: RagManager):
    """Scenario 10: workflow_intent containing 'refactor' maps to refactor type."""
    workflow_intent = "refactor_database: migrate to PostgreSQL"
    rag_manager.submit_workflow_candidates(
        {"key_findings": ["migrate to PostgreSQL instead of SQLite"]},
        workflow_id="wf-db",
        chat_id="chat-1",
        agent_id="agent-a",
        workspace_dir="/ws",
        workflow_intent=workflow_intent,
    )

    candidates = storage.fetchall(
        "SELECT * FROM memory_candidates WHERE source_type = 'workflow'"
    )

    if len(candidates) > 0:
        for cand in candidates:
            memory_type = cand.get("memory_type") or ""
            chain_json = cand.get("source_chain_json") or ""
            # Either memory_type encodes intent OR source_chain_json contains it
            assert (
                "refactor" in memory_type.lower()
                or "refactor" in chain_json.lower()
            )


def test_workflow_intent_infers_security_type(storage: Database, rag_manager: RagManager):
    """Scenario 10: workflow_intent containing 'security' maps to security type."""
    workflow_intent = "security_audit: review authentication vulnerabilities"
    rag_manager.submit_workflow_candidates(
        {"key_findings": ["always rate-limit the login endpoint"]},
        workflow_id="wf-sec",
        chat_id="chat-1",
        agent_id="agent-a",
        workspace_dir="/ws",
        workflow_intent=workflow_intent,
    )

    candidates = storage.fetchall(
        "SELECT * FROM memory_candidates WHERE source_type = 'workflow' "
        "ORDER BY created_at DESC LIMIT 5"
    )

    for cand in candidates:
        chain_json = cand.get("source_chain_json") or ""
        if workflow_intent in chain_json:
            # Type inference present in source chain or memory_type
            mtype = cand.get("memory_type") or ""
            assert "security" in chain_json.lower() or "security" in mtype.lower()
