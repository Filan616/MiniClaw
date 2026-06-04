"""Memory store (Phase 8 M5).

The candidate → approval → item lifecycle:

    auto extractor / explicit /memory remember
        ↓
    insert_memory_candidate(status='pending')
        ↓
    create approval (approval_type='memory_write')
        ↓
    user approve / reject
        ↓
    on approve: validate → store as rag_items(namespace='memory')
                update_candidate_status('approved')
    on reject: update_candidate_status('rejected')

Critical invariant (RAG.md §1.4 / user feedback 6): **no path from any
auto-source ever writes directly to rag_items**. Auto extraction always
hits ``insert_memory_candidate(status='pending')`` first.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from mini_claw.permissions.approval_store import ApprovalStore
from mini_claw.rag.memory.candidate import MemoryCandidate
from mini_claw.rag.memory.policy import MemoryDecision, evaluate_candidate
from mini_claw.rag.memory.validator import MemoryValidator
from mini_claw.rag.models import RagItem
from mini_claw.rag.store import RagStore

__all__ = ["MemoryStore"]


class MemoryStore:
    """Manages memory candidate lifecycle and final storage as rag_items."""

    def __init__(
        self,
        rag_store: RagStore,
        approval_store: ApprovalStore,
        validator: MemoryValidator | None = None,
    ):
        self.store = rag_store
        self.approval = approval_store
        self.validator = validator or MemoryValidator()

    # ------------------------------------------------------------------
    # Candidate intake (called by auto extractors AND explicit user path)
    # ------------------------------------------------------------------

    def submit_candidates(
        self,
        candidates: list[MemoryCandidate],
        *,
        require_approval: bool = True,
    ) -> list[tuple[MemoryCandidate, str | None, str]]:
        """Persist *candidates* as ``status='pending'`` and create approvals.

        Returns one ``(candidate, approval_id_or_None, status_word)`` tuple
        per input. status_word is one of:
        - ``"submitted"``   — pending approval created
        - ``"rejected:<category>"`` — validator killed it before queueing

        The candidates are written to ``memory_candidates`` table either
        way (rejected ones get ``status='rejected'``) so audit can show the
        rejected attempt.
        """
        results: list[tuple[MemoryCandidate, str | None, str]] = []
        for cand in candidates:
            decision = evaluate_candidate(cand, validator=self.validator, explicit=False)
            if not decision.should_store:
                # Persist as rejected with reason in metadata so audit can see it
                rejected = self._with_status(cand, "rejected")
                self.store.insert_memory_candidate(rejected)
                results.append((rejected, None, f"rejected:{decision.category}"))
                continue

            self.store.insert_memory_candidate(cand)
            if not require_approval:
                # Trusted path (rare). Promote directly.
                self.commit_candidate(cand.candidate_id)
                results.append((cand, None, "stored_direct"))
                continue

            # Create pending approval (approval_type='memory_write')
            approval_id = self._create_approval(cand)
            self.store.update_candidate_status(
                cand.candidate_id, "pending", approval_id=approval_id
            )
            results.append((cand, approval_id, "submitted"))
        return results

    def submit_explicit(
        self,
        content: str,
        *,
        memory_type: str,
        agent_id: str,
        chat_id: str,
        channel: str | None = None,
        scope_type: str = "agent",
        scope_id: str | None = None,
    ) -> tuple[MemoryCandidate | None, str | None, str]:
        """Build and submit a single candidate from ``/memory remember``.

        Explicit path relaxes scoring (stability / reuse) but validator
        still runs. Always requires user approval.
        """
        if not content or not content.strip():
            return None, None, "rejected:empty"

        cand = MemoryCandidate(
            candidate_id=f"cand-{uuid.uuid4().hex[:12]}",
            content=content.strip(),
            memory_type=memory_type,
            scope_type=scope_type,
            scope_id=scope_id or agent_id,
            source_type="explicit",
            status="pending",
            created_at=int(time.time()),
            updated_at=int(time.time()),
            stability=2,  # explicit: relaxed; validator + L3 approval cover safety
            reuse_value=2,
            sensitivity=1,
            confidence=0.85,
            source_chain_json='{"source": "explicit"}',
            created_by_agent_id=agent_id,
            created_from_chat_id=chat_id,
            created_from_channel=channel,
        )

        decision = evaluate_candidate(cand, validator=self.validator, explicit=True)
        if not decision.should_store:
            self.store.insert_memory_candidate(self._with_status(cand, "rejected"))
            return cand, None, f"rejected:{decision.category}"

        self.store.insert_memory_candidate(cand)
        approval_id = self._create_approval(cand)
        self.store.update_candidate_status(
            cand.candidate_id, "pending", approval_id=approval_id
        )
        return cand, approval_id, "submitted"

    # ------------------------------------------------------------------
    # Approval resolution
    # ------------------------------------------------------------------

    def commit_candidate(
        self, candidate_id: str
    ) -> tuple[str | None, str]:
        """Promote a pending candidate to ``rag_items(namespace='memory')``.

        Re-runs validation as a final defense (the validator never trusts
        that nothing has tampered with the row in flight).
        Returns ``(item_id_or_None, error_message)``.
        """
        cand = self.store.get_memory_candidate(candidate_id)
        if cand is None:
            return None, "candidate not found"
        if cand.status not in ("pending", "approved"):
            return None, f"candidate not approvable (status={cand.status})"

        # Final validator pass
        result = self.validator.validate(cand)
        if not result.ok:
            self.store.update_candidate_status(candidate_id, "rejected")
            return None, f"validator rejected: {result.reason}"

        item_id = f"mem-{uuid.uuid4().hex[:12]}"
        now = int(time.time())
        # Phase 9 P0.1: extract workspace_dir from candidate metadata/source_chain
        workspace_dir_val = None
        if cand.source_chain_json:
            try:
                import json
                chain = json.loads(cand.source_chain_json)
                workspace_dir_val = chain.get("workspace_dir")
            except Exception:
                pass
        if not workspace_dir_val and cand.metadata_json:
            try:
                import json
                meta = json.loads(cand.metadata_json)
                workspace_dir_val = meta.get("workspace_dir")
            except Exception:
                pass

        item = RagItem(
            item_id=item_id,
            namespace="memory",
            source_type=cand.memory_type,
            scope_type=cand.scope_type,
            scope_id=cand.scope_id,
            owner_agent_id=cand.created_by_agent_id or "unknown",
            session_id=cand.source_session_id,
            chat_id=cand.created_from_chat_id,
            channel_name=cand.created_from_channel,
            workspace_dir=workspace_dir_val,
            source_path=None,
            title=cand.content[:80],
            content_hash=None,
            status="active",
            importance=3,
            pinned=0,
            confidence=cand.confidence,
            created_at=now,
            updated_at=now,
            indexed_by_agent_id=cand.created_by_agent_id,
            indexed_by_chat_id=cand.created_from_chat_id,
            indexed_by_channel=cand.created_from_channel,
            source_chain_json=cand.source_chain_json,
            metadata_json=None,
            active_version=1,
            sensitivity_level="low",
        )
        self.store.insert_item(item)

        # One chunk per memory: the content itself
        from mini_claw.rag.models import RagChunk
        chunk = RagChunk(
            chunk_id=f"{item_id}-0",
            item_id=item_id,
            chunk_index=0,
            content=cand.content,
            token_count=max(1, len(cand.content) // 4),
            version=1,
        )
        self.store.insert_chunks([chunk])

        # Best-effort FTS write (M2 already handles SQLite without FTS5)
        try:
            self.store.storage.executemany(
                "INSERT INTO rag_chunks_fts(chunk_id, item_id, content, "
                "section_title, symbol_name) VALUES (?, ?, ?, ?, ?)",
                [(chunk.chunk_id, item_id, chunk.content, "", "")],
            )
        except Exception:
            pass

        self.store.update_candidate_status(candidate_id, "stored")
        return item_id, ""

    def reject_candidate(self, candidate_id: str) -> bool:
        """Mark a candidate as rejected (no rag_items write)."""
        cand = self.store.get_memory_candidate(candidate_id)
        if cand is None:
            return False
        self.store.update_candidate_status(candidate_id, "rejected")
        return True

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------

    def list_pending(self, limit: int = 50) -> list[MemoryCandidate]:
        return self.store.list_memory_candidates(status="pending", limit=limit)

    def list_memories(
        self, *, owner_agent_id: str, status: str = "active", limit: int = 100
    ) -> list[RagItem]:
        return self.store.list_by_scope(
            namespace="memory",
            owner_agent_id=owner_agent_id,
            status=status,
            limit=limit,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _with_status(self, cand: MemoryCandidate, status: str) -> MemoryCandidate:
        from dataclasses import replace
        return replace(cand, status=status, updated_at=int(time.time()))

    def _create_approval(self, cand: MemoryCandidate) -> str:
        approval_id = f"ap-{uuid.uuid4().hex[:12]}"
        ttl = 7 * 86400  # memory approvals stay open for a week
        expires_at = int(time.time()) + ttl
        self.approval.create_pending(
            approval_id=approval_id,
            run_id=cand.candidate_id,  # use candidate_id as run_id for traceability
            chat_id=cand.created_from_chat_id or "unknown",
            agent_id=cand.created_by_agent_id or "unknown",
            tool_name="memory_remember",
            tool_args={
                "candidate_id": cand.candidate_id,
                "content": cand.content[:500],
                "memory_type": cand.memory_type,
                "scope": f"{cand.scope_type}/{cand.scope_id}",
            },
            expires_at=expires_at,
            approval_type="memory_write",
            channel_name=cand.created_from_channel or "legacy",
        )
        return approval_id
