"""RAG 数据模型（Phase 8 M1）。

本模块定义 RAG 系统的核心数据类，所有数据类使用 `dataclass(slots=True)` 与
现有 WorkflowSpec / AgentConfig 保持一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "RagItem",
    "RagChunk",
    "RagSearchResult",
    "RagItemChunkVersion",
    "RagReindexDiff",
    "RagReindexDiffChunk",
    "ActiveContext",
    "MemoryCandidate",
    "RagStatus",
    "RagComponentStatus",
    "AUDIT_EVENT_TYPES",
    "WORKSPACE_MEMORY_TYPES",
]

# Phase 9 M9.3: Workspace-scoped memory types
WORKSPACE_MEMORY_TYPES = {
    "test_command",
    "build_command",
    "debug_pattern",
    "bug_root_cause",
    "project_constraint",
    "project_preference",
    "architecture_decision",
    "tech_stack_choice",
    "module_boundary",
    "security_rule",
    "workflow_finding",
    "implementation_note",
    "deployment_note",
}



@dataclass(slots=True)
class RagItem:
    """RAG item 元数据（context 或 memory）。"""

    item_id: str
    namespace: str  # context | memory
    source_type: str  # document | code | log | user_preference | project_rule | ...
    scope_type: str  # user | agent | workspace | session | document | codebase
    scope_id: str
    owner_agent_id: str
    status: str  # active | warm | archived | cold | stale | orphan | deleted
    created_at: int
    updated_at: int

    # Optional fields
    session_id: str | None = None
    chat_id: str | None = None
    channel_name: str | None = None
    workspace_dir: str | None = None
    source_path: str | None = None
    title: str | None = None
    content_hash: str | None = None
    importance: int = 3
    pinned: int = 0
    confidence: float = 1.0
    last_accessed_at: int | None = None
    access_count: int = 0
    expires_at: int | None = None
    indexed_by_agent_id: str | None = None
    indexed_by_chat_id: str | None = None
    indexed_by_channel: str | None = None
    source_chain_json: str | None = None
    metadata_json: str | None = None

    # M1 schema 增强字段
    active_version: int = 1  # M3 原子 reindex 用
    sensitivity_level: str = "low"  # low | medium | high
    chunker_version: str | None = None
    anchor_schema_version: str | None = None
    embedding_model: str | None = None
    last_reindex_diff_id: str | None = None
    last_reindex_diff_json: str | None = None


@dataclass(slots=True)
class RagChunk:
    """RAG chunk（原文切片）。"""

    chunk_id: str
    item_id: str
    chunk_index: int
    content: str

    # Optional fields
    token_count: int | None = None
    start_line: int | None = None
    end_line: int | None = None
    section_title: str | None = None
    symbol_name: str | None = None
    language: str | None = None
    content_hash: str | None = None
    metadata_json: str | None = None

    # M1 schema 增强字段
    version: int = 1  # M3 原子 reindex 用
    anchor_id: str | None = None
    chunk_hash: str | None = None
    chunker_version: str | None = None
    anchor_schema_version: str | None = None


@dataclass(slots=True)
class RagItemChunkVersion:
    """Mapping of chunks visible in a specific item version."""

    item_id: str
    version: int
    chunk_id: str
    chunk_order: int
    created_at: int
    anchor_id: str | None = None
    status: str = "active"
    is_reused: int = 0


@dataclass(slots=True)
class RagReindexDiff:
    """Structured summary for one reindex attempt."""

    diff_id: str
    item_id: str
    old_version: int
    new_version: int
    status: str
    mode: str
    started_at: int
    reason: str | None = None
    added_count: int = 0
    updated_count: int = 0
    deleted_count: int = 0
    reused_count: int = 0
    uncertain_count: int = 0
    fallback_reason: str | None = None
    vector_cleanup_status: str | None = None
    finished_at: int | None = None
    duration_ms: int | None = None
    metadata_json: str | None = None


@dataclass(slots=True)
class RagReindexDiffChunk:
    """Per-chunk diff row."""

    row_id: str
    diff_id: str
    item_id: str
    change_type: str
    old_chunk_id: str | None = None
    new_chunk_id: str | None = None
    chunk_order: int | None = None
    anchor_id: str | None = None
    match_strategy: str | None = None
    match_confidence: float | None = None
    rename_detected: int = 0
    metadata_json: str | None = None


@dataclass(slots=True)
class RagSearchResult:
    """检索结果。"""

    chunk_id: str
    item_id: str
    content: str
    score: float

    # Metadata
    source_path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    section_title: str | None = None
    symbol_name: str | None = None
    namespace: str | None = None
    source_type: str | None = None
    sensitivity_level: str = "low"


@dataclass(slots=True)
class ActiveContext:
    """当前 session 的 active context。"""

    session_id: str
    agent_id: str
    context_id: str
    context_type: str  # document | code | log | codebase
    activated_at: int
    title: str | None = None
    expires_at: int | None = None


@dataclass(slots=True)
class MemoryCandidate:
    """待审批的长期记忆候选（M5 用）。"""

    candidate_id: str
    content: str
    memory_type: str
    scope_type: str
    scope_id: str
    source_type: str  # explicit | compaction | task_state | workflow
    status: str  # pending | approved | rejected | stored
    created_at: int
    updated_at: int

    # Scoring fields
    stability: int = 3
    reuse_value: int = 3
    sensitivity: int = 1
    confidence: float = 1.0

    # M1 schema 增强：完整 source chain（用户反馈 6）
    source_chain_json: str = "{}"
    source_message_ids: str | None = None  # 逗号分隔
    source_session_id: str | None = None
    source_workflow_id: str | None = None
    created_by_agent_id: str | None = None
    created_from_chat_id: str | None = None
    created_from_channel: str | None = None

    approval_id: str | None = None
    metadata_json: str | None = None


@dataclass(slots=True)
class RagComponentStatus:
    """RAG 组件健康状态（M4.5 用）。"""

    component: str  # fts | chroma | embedding
    status: str  # ok | degraded | failed
    last_ok_at: int | None = None
    last_error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RagStatus:
    """RAG 系统整体状态（M4.5 用）。"""

    enabled: bool
    fts: RagComponentStatus
    chroma: RagComponentStatus
    embedding: RagComponentStatus
    active_fallback: str  # none | fts-only | chroma-only
    stale_items: int
    orphan_items: int
    pending_candidates: int
    abandoned_reindex_versions: int
    timestamp: int


# M1: 审计事件类型常量（供后续 milestone 引用，不影响 audit/logger.py）
AUDIT_EVENT_TYPES = frozenset(
    [
        # M2 索引事件
        "rag_index_attempt",
        "rag_index_completed",
        "rag_index_failed",
        "rag_index_sensitive_attempt",
        # M2 检索事件
        "rag_search_performed",
        "rag_search_exfil_query",
        "rag_search_sensitive_query",
        # M2.5 链检测事件
        "rag_external_send_after_search",
        "rag_write_retrieved_content",
        "rag_chain_attack_blocked",
        # M3 生命周期事件
        "rag_context_activated",
        "rag_context_archived",
        "rag_context_deleted",
        "rag_context_stale",
        "rag_context_orphan",
        "rag_lifecycle_cleanup",
        # M4 向量后端事件
        "vector_backend_error",
        # M5 Memory 事件
        "memory_candidate_created",
        "memory_write_approval_required",
        "memory_write_completed",
        "memory_write_rejected",
        "memory_write_policy_like_content",
        "memory_search_performed",
        "memory_delete_completed",
        # Phase 9 M9.1 Chat Search 事件
        "chat_search_performed",
        "chat_search_rebuild_started",
        "chat_search_rebuild_completed",
        "chat_search_rebuild_failed",
        "chat_search_sensitive_query",
        # Phase 9 M9.2 Memory Control 事件
        "memory_candidate_listed",
        "memory_approved_batch",
        "memory_rejected_batch",
        "memory_cleared_scope",
        "memory_clear_approval_required",
        "memory_exported",
        "memory_export_approval_required",
        "memory_reject_approval_required",
        # Phase 9 M9.3 Workspace Memory 事件
        "workspace_memory_created",
        # Phase 9 M9.5 Context Isolation 事件
        "memory_scope_violation_blocked",
        # Phase 9 M9.6 Memory Maintenance 事件
        "memory_maintenance_run",
        "memory_dedupe_suggested",
        "memory_conflict_detected",
        "memory_cleanup_suggested",
    ]
)
