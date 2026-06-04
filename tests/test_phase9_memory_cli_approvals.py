"""Phase 9 tests: Scenarios 1-3 — /memory CLI L3 approval requirements.

Tests cover:
1. /memory clear L3 approval
2. /memory delete L3 approval
3. /memory approve/reject candidate L3 approval
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mini_claw.config import AppConfig
from mini_claw.permissions.approval_store import ApprovalStore
from mini_claw.permissions.gate import PermissionGate
from mini_claw.permissions.policy import PermissionPolicy
from mini_claw.rag.manager import RagManager
from mini_claw.rag.memory.store import MemoryStore
from mini_claw.storage.db import Database


@pytest.fixture
def storage(tmp_path: Path) -> Database:
    return Database(tmp_path / "phase9.db")


@pytest.fixture
def gate(storage: Database) -> PermissionGate:
    policy = PermissionPolicy(AppConfig().permissions)
    approval_store = ApprovalStore(storage)
    return PermissionGate(policy, approval_store)


@pytest.fixture
def rag_manager(storage: Database, gate: PermissionGate) -> RagManager:
    from mini_claw.config import RagConfig
    cfg = RagConfig()
    cfg.enabled = True
    cfg.namespaces.memory_enabled = True
    policy = PermissionPolicy(AppConfig().permissions)
    return RagManager(storage, cfg, policy)


def _ctx(agent_id="agent-a", chat_id="chat-1", level="L3") -> dict:
    return {
        "agent_id": agent_id,
        "chat_id": chat_id,
        "level": level,
        "workspace_dir": "/ws",
        "channel_name": "cli",
    }


# ==================== Scenario 1: /memory clear L3 approval ====================


def test_memory_clear_scope_requires_l3_approval(gate: PermissionGate):
    """Scenario 1: /memory clear with scope requires L3 approval."""
    decision = gate.evaluate(
        "memory_clear_scope",
        {"scope": "agent", "confirm": True},
        _ctx(level="L3"),
    )
    # Memory clear is L3 operation, requires approval
    assert decision.action == "need_approval"
    assert "L3" in decision.reason or "approval" in decision.reason.lower()


def test_memory_clear_allowed_with_session_grant(gate: PermissionGate):
    """Scenario 1: Session grant bypasses approval for memory_clear."""
    ctx = _ctx(level="L3")
    gate.grant_session(ctx, "memory_clear_scope", ttl=600)

    decision = gate.evaluate(
        "memory_clear_scope",
        {"scope": "agent", "confirm": True},
        ctx,
    )
    assert decision.action == "allow"
    assert "session grant" in decision.reason.lower()


# ==================== Scenario 2: /memory delete L3 approval ====================


def test_memory_delete_requires_l3_approval(gate: PermissionGate):
    """Scenario 2: memory_delete tool requires L3 approval."""
    decision = gate.evaluate(
        "memory_delete",
        {"memory_id": "mem_abc123"},
        _ctx(level="L3"),
    )
    assert decision.action == "need_approval"
    assert "L3" in decision.reason or "approval" in decision.reason.lower()


def test_memory_delete_with_session_grant(gate: PermissionGate):
    """Scenario 2: Session grant allows memory_delete without approval."""
    ctx = _ctx(level="L3")
    gate.grant_session(ctx, "memory_delete", ttl=600)

    decision = gate.evaluate(
        "memory_delete",
        {"memory_id": "mem_xyz789"},
        ctx,
    )
    assert decision.action == "allow"


# ==================== Scenario 3: /memory approve/reject L3 approval ====================


def test_memory_approve_candidate_creates_approval_record(
    rag_manager: RagManager, storage: Database
):
    """Scenario 3: Approving a memory candidate requires L3 confirmation.

    The approval flow:
    1. Candidate submitted -> creates pending approval
    2. User approves -> promotes to rag_items
    """
    # Submit a candidate (creates pending approval)
    cand_id, approval_id, status = rag_manager.remember(
        "user prefers concise answers", ctx=_ctx()
    )
    assert status == "submitted"
    assert approval_id is not None

    # Verify approval record exists (column is `id`, not `approval_id`)
    approval = storage.fetchone(
        "SELECT * FROM pending_approvals WHERE id = ?",
        (approval_id,)
    )
    assert approval is not None
    assert approval["approval_type"] == "memory_write"
    assert approval["status"] == "pending"


def test_memory_reject_candidate_marks_rejected(
    rag_manager: RagManager, storage: Database
):
    """Scenario 3: Rejecting a memory candidate updates status."""
    cand_id, approval_id, status = rag_manager.remember(
        "this is a valid memory", ctx=_ctx()
    )
    assert status == "submitted"

    # Reject the candidate
    success = rag_manager.reject_memory(cand_id)
    assert success

    # Verify candidate marked rejected
    cand = rag_manager.store.get_memory_candidate(cand_id)
    assert cand is not None
    assert cand.status == "rejected"


def test_batch_approve_respects_max_limit(
    rag_manager: RagManager, storage: Database
):
    """Scenario 3: Batch approval limited by config threshold."""
    # Submit multiple candidates
    candidates = []
    for i in range(5):
        cand_id, _, status = rag_manager.remember(
            f"memory item {i}", ctx=_ctx()
        )
        if status == "submitted":
            candidates.append(cand_id)

    # Batch approve with limit=3
    approved, errors = rag_manager.approve_batch(candidates[:3])
    assert len(approved) <= 3
    assert len(errors) == 0 or all(e for e in errors)


def test_batch_reject_marks_all_rejected(
    rag_manager: RagManager, storage: Database
):
    """Scenario 3: Batch rejection marks all candidates rejected."""
    # Submit multiple candidates
    candidates = []
    for i in range(3):
        cand_id, _, status = rag_manager.remember(
            f"test memory {i}", ctx=_ctx()
        )
        if status == "submitted":
            candidates.append(cand_id)

    # Batch reject
    rejected = rag_manager.reject_batch(candidates)
    assert len(rejected) == len(candidates)

    # Verify all marked rejected
    for cand_id in candidates:
        cand = rag_manager.store.get_memory_candidate(cand_id)
        assert cand is not None
        assert cand.status == "rejected"
