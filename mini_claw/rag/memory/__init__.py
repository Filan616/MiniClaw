"""Memory RAG (Phase 8 M5).

Long-term agent memories: user preferences, project rules, architecture
decisions, workflow findings. All writes go through ApprovalStore (L3) so
auto-extracted candidates from session compaction / TaskState pruning /
WorkflowMerger never bypass user review.

Public surface:
- :class:`mini_claw.rag.memory.candidate.MemoryCandidate` (re-export of
  models.py) and :func:`should_store_memory` scoring function
- :class:`mini_claw.rag.memory.validator.MemoryValidator`
- :func:`mini_claw.rag.memory.consolidator.consolidate`
- :func:`mini_claw.rag.memory.extractor.extract_from_*` three functions
- :class:`mini_claw.rag.memory.store.MemoryStore` (candidate → approval → item)
"""

from __future__ import annotations

__all__ = [
    "MemoryCandidate",
    "should_store_memory",
    "MemoryValidator",
    "MemoryStore",
    "consolidate",
    "extract_from_session_compaction",
    "extract_from_task_state",
    "extract_from_workflow_merger",
    "extract_from_agent_summary",
]

from mini_claw.rag.memory.candidate import MemoryCandidate, should_store_memory
from mini_claw.rag.memory.consolidator import consolidate
from mini_claw.rag.memory.extractor import (
    extract_from_agent_summary,
    extract_from_session_compaction,
    extract_from_task_state,
    extract_from_workflow_merger,
)
from mini_claw.rag.memory.store import MemoryStore
from mini_claw.rag.memory.validator import MemoryValidator
