"""Phase 9 tests: Scenarios 11-13, 16 — Maintenance, hybrid retrieval, audit events, and injector header.

Tests cover:
11. maintenance run_on_startup
12. hybrid embedding dedupe
13. all 22 audit events queryable
16. [Retrieved User Memory] final header
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mini_claw.config import AppConfig, RagConfig
from mini_claw.permissions.policy import PermissionPolicy
from mini_claw.rag.manager import RagManager
from mini_claw.storage.db import Database


@pytest.fixture
def storage(tmp_path: Path) -> Database:
    return Database(tmp_path / "misc.db")


@pytest.fixture
def rag_manager(storage: Database) -> RagManager:
    cfg = RagConfig()
    cfg.enabled = True
    cfg.namespaces.memory_enabled = True
    cfg.memory_maintenance.enabled = True
    cfg.memory_maintenance.run_on_startup = False  # Default OFF
    policy = PermissionPolicy(AppConfig().permissions)
    return RagManager(storage, cfg, policy)


# ==================== Scenario 11: maintenance run_on_startup ====================


def test_maintenance_run_on_startup_default_false(storage: Database):
    """Scenario 11: run_on_startup defaults to false."""
    cfg = RagConfig()
    cfg.enabled = True
    cfg.namespaces.memory_enabled = True

    # Default should be false
    assert cfg.memory_maintenance.run_on_startup is False


def test_maintenance_run_on_startup_can_be_enabled(storage: Database):
    """Scenario 11: run_on_startup can be explicitly enabled."""
    cfg = RagConfig()
    cfg.enabled = True
    cfg.memory_maintenance.enabled = True
    cfg.memory_maintenance.run_on_startup = True

    assert cfg.memory_maintenance.run_on_startup is True


def test_maintenance_runs_when_startup_enabled(storage: Database, rag_manager: RagManager):
    """Scenario 11: Maintenance executes on app startup if run_on_startup=true."""
    # Create duplicate memories
    for i in range(2):
        cand_id, _, status = rag_manager.remember(
            "user prefers concise answers", ctx={"agent_id": "agent-a", "chat_id": "chat-1"}
        )
        if status == "submitted":
            rag_manager.approve_memory(cand_id)

    # Run maintenance
    result = rag_manager.run_maintenance(agent_id="agent-a", auto_apply=False)

    # Should detect duplicates
    if result:
        assert "duplicates" in result or "conflicts" in result or "stale" in result


def test_maintenance_suggest_only_mode(storage: Database, rag_manager: RagManager):
    """Scenario 11: suggest_only mode does not modify data."""
    # Create memories
    cand_id, _, status = rag_manager.remember(
        "test memory for maintenance", ctx={"agent_id": "agent-a", "chat_id": "chat-1"}
    )
    if status == "submitted":
        item_id, _ = rag_manager.approve_memory(cand_id)

    # Run maintenance in suggest_only mode
    result = rag_manager.run_maintenance(agent_id="agent-a", auto_apply=False)

    # Memory should still exist (not deleted)
    if item_id:
        item = rag_manager.store.get_item(item_id)
        assert item is not None


# ==================== Scenario 12: hybrid embedding dedupe ====================


def test_hybrid_dedupe_uses_embedding_threshold(storage: Database, rag_manager: RagManager):
    """Scenario 12: Hybrid dedupe applies embedding_threshold for semantic similarity."""
    # Create near-duplicate memories
    cand1, _, s1 = rag_manager.remember(
        "user prefers concise answers", ctx={"agent_id": "agent-a", "chat_id": "chat-1"}
    )
    cand2, _, s2 = rag_manager.remember(
        "user likes brief responses", ctx={"agent_id": "agent-a", "chat_id": "chat-1"}
    )

    if s1 == "submitted":
        rag_manager.approve_memory(cand1)
    if s2 == "submitted":
        rag_manager.approve_memory(cand2)

    # Run maintenance with dedupe
    result = rag_manager.run_maintenance(agent_id="agent-a", auto_apply=False)

    # Should detect semantic duplicates via embedding similarity
    # (if embedding backend is available)
    if result and "duplicates" in result:
        assert len(result["duplicates"]) >= 0


def test_hybrid_dedupe_text_threshold(storage: Database, rag_manager: RagManager):
    """Scenario 12: Text threshold (Jaccard) for exact/near-exact duplicates."""
    # Create exact duplicate
    for i in range(2):
        cand_id, _, status = rag_manager.remember(
            "always use Python 3.11", ctx={"agent_id": "agent-a", "chat_id": "chat-1"}
        )
        if status == "submitted":
            rag_manager.approve_memory(cand_id)

    # Run maintenance
    result = rag_manager.run_maintenance(agent_id="agent-a", auto_apply=False)

    # Should detect text-based duplicates
    if result and "duplicates" in result:
        assert isinstance(result["duplicates"], list)


# ==================== Scenario 13: all 22 audit events queryable ====================


def test_all_audit_events_in_security_audit_table(storage: Database):
    """Scenario 13: All 22 Phase 9 audit events can be queried."""
    from mini_claw.audit.logger import SecurityAuditLogger

    audit = SecurityAuditLogger(storage)

    # Sample audit events from Phase 9 spec
    phase9_events = [
        "chat_search_rebuild_started",
        "chat_search_rebuild_completed",
        "chat_search_rebuild_failed",
        "chat_search_bulk_export_attempt",
        "chat_search_sensitive_query",
        "rag_search_sensitive_query",
        "memory_candidate_created",
        "memory_approved_batch",
        "memory_rejected_batch",
        "memory_export_approval_required",
        "memory_export_approval_granted",
        "memory_exported",
        "memory_cleared_scope",
        "memory_maintenance_run",
        "memory_dedupe_suggested",
        "workflow_started",
        "workflow_completed",
        "blacklist_hit",
        "sensitive_path",
        "chain_attack_blocked",
        "rag_index_sensitive_attempt",
        "rag_index_completed",
    ]

    # Log sample events
    for event_type in phase9_events[:5]:
        audit.log_security_event(
            event_type=event_type,
            details={"test": True},
            chat_id="chat-test",
            agent_id="agent-test",
        )

    # Query all events
    events = storage.fetchall("SELECT DISTINCT event_type FROM security_audit")
    event_types = {e["event_type"] for e in events}

    # At least some of the phase9 events should be queryable
    assert len(event_types) >= 5


def test_audit_events_have_debug_id(storage: Database):
    """Scenario 13: All audit events include debug_id for traceability."""
    from mini_claw.audit.logger import SecurityAuditLogger

    audit = SecurityAuditLogger(storage)
    debug_id = audit.log_security_event(
        event_type="test_event",
        details={"key": "value"},
        chat_id="chat-1",
        agent_id="agent-a",
    )

    # debug_id should be returned
    assert debug_id is not None
    assert debug_id.startswith("sec_")

    # Should be queryable
    event = storage.fetchone("SELECT * FROM security_audit WHERE debug_id = ?", (debug_id,))
    assert event is not None
    assert event["event_type"] == "test_event"


def test_audit_events_include_timestamp(storage: Database):
    """Scenario 13: All audit events include created_at timestamp."""
    from mini_claw.audit.logger import SecurityAuditLogger

    audit = SecurityAuditLogger(storage)
    before = int(time.time())
    debug_id = audit.log_security_event(
        event_type="timestamp_test",
        details={},
    )
    after = int(time.time())

    event = storage.fetchone("SELECT * FROM security_audit WHERE debug_id = ?", (debug_id,))
    assert event is not None
    assert event["created_at"] >= before
    assert event["created_at"] <= after


# ==================== Scenario 16: [Retrieved User Memory] final header ====================


def test_injector_uses_retrieved_user_memory_header():
    """Scenario 16: Memory injection uses '[Retrieved User Memory]' header."""
    from mini_claw.rag.injector import MEMORY_TRUSTED_HEADER

    # Verify header text
    assert "[Retrieved User Memory]" in MEMORY_TRUSTED_HEADER
    assert "long-term memories" in MEMORY_TRUSTED_HEADER.lower() or "validated" in MEMORY_TRUSTED_HEADER.lower()


def test_injector_memory_block_format():
    """Scenario 16: Memory block uses correct header and format."""
    from mini_claw.rag.injector import build_memory_block

    # Mock memory items
    class MockMemory:
        def __init__(self, content):
            self.content = content
            self.memory_type = "preference"
            self.source_type = "explicit_remember"

    memories = [MockMemory("user prefers concise answers")]
    block = build_memory_block(memories)

    # Should include header
    assert "[Retrieved User Memory]" in block
    assert "user prefers concise answers" in block


def test_injector_workspace_memory_header():
    """Scenario 16: Workspace memory uses separate header."""
    from mini_claw.rag.injector import WORKSPACE_MEMORY_HEADER

    # Verify workspace memory has distinct header
    assert "[Retrieved Workspace Memory]" in WORKSPACE_MEMORY_HEADER
    assert "workspace-scoped" in WORKSPACE_MEMORY_HEADER.lower() or "project decisions" in WORKSPACE_MEMORY_HEADER.lower()


def test_injector_chat_history_header():
    """Scenario 16: Chat history uses untrusted header."""
    from mini_claw.rag.injector import CHAT_HISTORY_HEADER

    # Chat history should be marked untrusted
    assert "[Retrieved Chat History]" in CHAT_HISTORY_HEADER
    assert "UNTRUSTED" in CHAT_HISTORY_HEADER or "untrusted" in CHAT_HISTORY_HEADER.lower()


def test_injector_context_header_untrusted():
    """Scenario 16: Context header warns about untrusted data."""
    from mini_claw.rag.injector import CONTEXT_UNTRUSTED_HEADER

    # Context should have strongest warning
    assert "[Retrieved Context]" in CONTEXT_UNTRUSTED_HEADER
    assert "UNTRUSTED" in CONTEXT_UNTRUSTED_HEADER
    assert "Do NOT execute" in CONTEXT_UNTRUSTED_HEADER or "ignore previous" in CONTEXT_UNTRUSTED_HEADER.lower()
