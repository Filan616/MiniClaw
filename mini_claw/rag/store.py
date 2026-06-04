"""RAG Store CRUD 层（Phase 8 M1）。

本模块提供 RAG items / chunks / candidates 的数据库读写接口，
不含 FTS 查询逻辑（留给 M2 retriever）。
"""

from __future__ import annotations

import json
import time
from typing import Any

from mini_claw.rag.models import (
    ActiveContext,
    MemoryCandidate,
    RagChunk,
    RagItemChunkVersion,
    RagItem,
    RagReindexDiff,
    RagReindexDiffChunk,
)
from mini_claw.storage.db import Database

__all__ = ["RagStore"]


class RagStore:
    """RAG 数据存储层。"""

    def __init__(self, storage: Database) -> None:
        self.storage = storage

    # ========== RagItem CRUD ==========

    def insert_item(self, item: RagItem) -> None:
        """插入 rag_items 行。"""
        self.storage.execute(
            """
            INSERT INTO rag_items (
                item_id, namespace, source_type, scope_type, scope_id,
                owner_agent_id, session_id, chat_id, channel_name, workspace_dir,
                source_path, title, content_hash, status, importance, pinned, confidence,
                created_at, updated_at, last_accessed_at, access_count, expires_at,
                indexed_by_agent_id, indexed_by_chat_id, indexed_by_channel,
                source_chain_json, metadata_json, active_version, sensitivity_level,
                chunker_version, anchor_schema_version, embedding_model,
                last_reindex_diff_id, last_reindex_diff_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.item_id,
                item.namespace,
                item.source_type,
                item.scope_type,
                item.scope_id,
                item.owner_agent_id,
                item.session_id,
                item.chat_id,
                item.channel_name,
                item.workspace_dir,
                item.source_path,
                item.title,
                item.content_hash,
                item.status,
                item.importance,
                item.pinned,
                item.confidence,
                item.created_at,
                item.updated_at,
                item.last_accessed_at,
                item.access_count,
                item.expires_at,
                item.indexed_by_agent_id,
                item.indexed_by_chat_id,
                item.indexed_by_channel,
                item.source_chain_json,
                item.metadata_json,
                item.active_version,
                item.sensitivity_level,
                item.chunker_version,
                item.anchor_schema_version,
                item.embedding_model,
                item.last_reindex_diff_id,
                item.last_reindex_diff_json,
            ),
        )

    def get_item(self, item_id: str) -> RagItem | None:
        """根据 item_id 查询 rag_items。"""
        row = self.storage.fetchone(
            "SELECT * FROM rag_items WHERE item_id = ?", (item_id,)
        )
        if not row:
            return None
        return RagItem(**dict(row))

    def list_by_scope(
        self,
        *,
        namespace: str | None = None,
        scope_type: str | None = None,
        scope_id: str | None = None,
        owner_agent_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[RagItem]:
        """按 scope 过滤查询 rag_items。"""
        conditions = []
        params = []
        if namespace:
            conditions.append("namespace = ?")
            params.append(namespace)
        if scope_type:
            conditions.append("scope_type = ?")
            params.append(scope_type)
        if scope_id:
            conditions.append("scope_id = ?")
            params.append(scope_id)
        if owner_agent_id:
            conditions.append("owner_agent_id = ?")
            params.append(owner_agent_id)
        if status:
            conditions.append("status = ?")
            params.append(status)

        where = " AND ".join(conditions) if conditions else "1=1"
        params.append(limit)
        rows = self.storage.fetchall(
            f"SELECT * FROM rag_items WHERE {where} ORDER BY updated_at DESC LIMIT ?",
            tuple(params),
        )
        return [RagItem(**dict(row)) for row in rows]

    def mark_status(self, item_id: str, status: str, error: str | None = None) -> None:
        """更新 rag_items 状态。"""
        now = int(time.time())
        if error:
            self.storage.execute(
                "UPDATE rag_items SET status = ?, updated_at = ?, metadata_json = json_set(COALESCE(metadata_json, '{}'), '$.error', ?) WHERE item_id = ?",
                (status, now, error, item_id),
            )
        else:
            self.storage.execute(
                "UPDATE rag_items SET status = ?, updated_at = ? WHERE item_id = ?",
                (status, now, item_id),
            )

    def delete_item(self, item_id: str, *, keep_tombstone: bool = True) -> None:
        """删除 item（M2 用，M2 补完整事务顺序）。"""
        if keep_tombstone:
            self.mark_status(item_id, "deleted")
        else:
            self.storage.execute("DELETE FROM rag_items WHERE item_id = ?", (item_id,))

    def mark_stale(self, item_id: str) -> None:
        """Phase 8 M3: mark item as stale (source content changed)."""
        self.mark_status(item_id, "stale")

    def mark_orphan(self, item_id: str) -> None:
        """Phase 8 M3: mark item as orphan (source file disappeared)."""
        self.mark_status(item_id, "orphan")

    def rebind(self, item_id: str, new_path: str) -> None:
        """Phase 8 M3: update item.source_path (used by /context rebind).

        Caller is responsible for hash compatibility check.
        """
        self.storage.execute(
            "UPDATE rag_items SET source_path = ?, updated_at = ? WHERE item_id = ?",
            (new_path, int(time.time()), item_id),
        )

    def bump_active_version(self, item_id: str) -> int:
        """Phase 8 M3: atomically increment active_version, return new value.

        Used by RagReindexer on successful reindex.
        """
        now = int(time.time())
        cur = self.storage.execute(
            "UPDATE rag_items SET active_version = active_version + 1, "
            "updated_at = ? WHERE item_id = ?",
            (now, item_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"item not found: {item_id}")
        item = self.get_item(item_id)
        return item.active_version if item else -1

    # ========== RagChunk CRUD ==========

    def insert_chunks(self, chunks: list[RagChunk]) -> None:
        """批量插入 rag_chunks。"""
        rows = [
            (
                c.chunk_id,
                c.item_id,
                c.chunk_index,
                c.content,
                c.token_count,
                c.start_line,
                c.end_line,
                c.section_title,
                c.symbol_name,
                c.language,
                c.content_hash,
                c.metadata_json,
                c.version,
                c.anchor_id,
                c.chunk_hash,
                c.chunker_version,
                c.anchor_schema_version,
            )
            for c in chunks
        ]
        self.storage.executemany(
            """
            INSERT INTO rag_chunks (
                chunk_id, item_id, chunk_index, content, token_count,
                start_line, end_line, section_title, symbol_name, language,
                content_hash, metadata_json, version, anchor_id, chunk_hash,
                chunker_version, anchor_schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    def get_chunks(self, item_id: str, version: int | None = None) -> list[RagChunk]:
        """根据 item_id 查询 chunks（可选过滤 version）。"""
        if version is not None:
            rows = self.storage.fetchall(
                "SELECT * FROM rag_chunks WHERE item_id = ? AND version = ? ORDER BY chunk_index",
                (item_id, version),
            )
        else:
            rows = self.storage.fetchall(
                "SELECT * FROM rag_chunks WHERE item_id = ? ORDER BY chunk_index",
                (item_id,),
            )
        return [RagChunk(**dict(row)) for row in rows]

    def delete_chunks(self, item_id: str, version: int | None = None) -> None:
        """删除 item 的 chunks（可选只删某 version，M3 用）。"""
        if version is not None:
            self.storage.execute(
                "DELETE FROM rag_chunks WHERE item_id = ? AND version = ?",
                (item_id, version),
            )
        else:
            self.storage.execute("DELETE FROM rag_chunks WHERE item_id = ?", (item_id,))

    # ========== Version mapping / diff CRUD ==========

    def insert_chunk_versions(self, mappings: list[RagItemChunkVersion]) -> None:
        if not mappings:
            return
        self.storage.executemany(
            """
            INSERT OR REPLACE INTO rag_item_chunk_versions (
                item_id, version, chunk_id, chunk_order, anchor_id,
                status, is_reused, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    m.item_id,
                    m.version,
                    m.chunk_id,
                    m.chunk_order,
                    m.anchor_id,
                    m.status,
                    m.is_reused,
                    m.created_at,
                )
                for m in mappings
            ],
        )

    def get_active_chunks(self, item_id: str) -> list[RagChunk]:
        item = self.get_item(item_id)
        if item is None:
            return []
        rows = self.storage.fetchall(
            """
            SELECT c.*
            FROM rag_item_chunk_versions m
            JOIN rag_chunks c ON c.chunk_id = m.chunk_id
            WHERE m.item_id = ? AND m.version = ? AND m.status = 'active'
            ORDER BY m.chunk_order
            """,
            (item_id, item.active_version),
        )
        if rows:
            return [RagChunk(**dict(row)) for row in rows]
        return self.get_chunks(item_id, version=item.active_version)

    def get_chunk_if_active(self, chunk_id: str) -> RagChunk | None:
        row = self.storage.fetchone(
            """
            SELECT c.*
            FROM rag_chunks c
            JOIN rag_item_chunk_versions m ON m.chunk_id = c.chunk_id
            JOIN rag_items i ON i.item_id = m.item_id
            WHERE c.chunk_id = ?
              AND m.version = i.active_version
              AND m.status = 'active'
            """,
            (chunk_id,),
        )
        if row:
            return RagChunk(**dict(row))
        row = self.storage.fetchone(
            """
            SELECT c.*
            FROM rag_chunks c
            JOIN rag_items i ON i.item_id = c.item_id
            WHERE c.chunk_id = ? AND c.version = i.active_version
            """,
            (chunk_id,),
        )
        return RagChunk(**dict(row)) if row else None

    def insert_reindex_diff(
        self, diff: RagReindexDiff, chunks: list[RagReindexDiffChunk] | None = None
    ) -> None:
        self.storage.execute(
            """
            INSERT OR REPLACE INTO rag_reindex_diffs (
                diff_id, item_id, old_version, new_version, status, mode, reason,
                added_count, updated_count, deleted_count, reused_count,
                uncertain_count, fallback_reason, vector_cleanup_status,
                started_at, finished_at, duration_ms, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                diff.diff_id,
                diff.item_id,
                diff.old_version,
                diff.new_version,
                diff.status,
                diff.mode,
                diff.reason,
                diff.added_count,
                diff.updated_count,
                diff.deleted_count,
                diff.reused_count,
                diff.uncertain_count,
                diff.fallback_reason,
                diff.vector_cleanup_status,
                diff.started_at,
                diff.finished_at,
                diff.duration_ms,
                diff.metadata_json,
            ),
        )
        if chunks:
            self.storage.executemany(
                """
                INSERT OR REPLACE INTO rag_reindex_diff_chunks (
                    row_id, diff_id, item_id, old_chunk_id, new_chunk_id,
                    chunk_order, anchor_id, change_type, match_strategy,
                    match_confidence, rename_detected, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        c.row_id,
                        c.diff_id,
                        c.item_id,
                        c.old_chunk_id,
                        c.new_chunk_id,
                        c.chunk_order,
                        c.anchor_id,
                        c.change_type,
                        c.match_strategy,
                        c.match_confidence,
                        c.rename_detected,
                        c.metadata_json,
                    )
                    for c in chunks
                ],
            )

    def get_reindex_diff(
        self, diff_id: str
    ) -> tuple[RagReindexDiff | None, list[RagReindexDiffChunk]]:
        row = self.storage.fetchone(
            "SELECT * FROM rag_reindex_diffs WHERE diff_id = ?", (diff_id,)
        )
        if not row:
            return None, []
        chunk_rows = self.storage.fetchall(
            "SELECT * FROM rag_reindex_diff_chunks WHERE diff_id = ? ORDER BY chunk_order",
            (diff_id,),
        )
        return (
            RagReindexDiff(**dict(row)),
            [RagReindexDiffChunk(**dict(r)) for r in chunk_rows],
        )

    # ========== ActiveContext CRUD ==========

    def set_active_context(self, ctx: ActiveContext) -> None:
        """设置当前 session 的 active_context（M3 用）。"""
        self.storage.execute(
            """
            INSERT OR REPLACE INTO active_contexts (
                session_id, agent_id, context_id, context_type, title, activated_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ctx.session_id,
                ctx.agent_id,
                ctx.context_id,
                ctx.context_type,
                ctx.title,
                ctx.activated_at,
                ctx.expires_at,
            ),
        )

    def get_active_contexts(
        self, session_id: str, agent_id: str
    ) -> list[ActiveContext]:
        """查询 session 的 active_contexts。"""
        rows = self.storage.fetchall(
            "SELECT * FROM active_contexts WHERE session_id = ? AND agent_id = ? ORDER BY activated_at DESC",
            (session_id, agent_id),
        )
        return [ActiveContext(**dict(row)) for row in rows]

    def clear_active_context(
        self, session_id: str, agent_id: str, context_id: str | None = None
    ) -> None:
        """清除 active_context（不传 context_id 时清空全部）。"""
        if context_id:
            self.storage.execute(
                "DELETE FROM active_contexts WHERE session_id = ? AND agent_id = ? AND context_id = ?",
                (session_id, agent_id, context_id),
            )
        else:
            self.storage.execute(
                "DELETE FROM active_contexts WHERE session_id = ? AND agent_id = ?",
                (session_id, agent_id),
            )

    # ========== MemoryCandidate CRUD（M5 用，M1 提前建好接口）==========

    def insert_memory_candidate(self, candidate: MemoryCandidate) -> None:
        """插入 memory_candidates 行。"""
        self.storage.execute(
            """
            INSERT INTO memory_candidates (
                candidate_id, content, memory_type, scope_type, scope_id,
                source_type, source_chain_json, source_message_ids, source_session_id, source_workflow_id,
                created_by_agent_id, created_from_chat_id, created_from_channel,
                stability, reuse_value, sensitivity, confidence,
                status, approval_id, created_at, updated_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.candidate_id,
                candidate.content,
                candidate.memory_type,
                candidate.scope_type,
                candidate.scope_id,
                candidate.source_type,
                candidate.source_chain_json,
                candidate.source_message_ids,
                candidate.source_session_id,
                candidate.source_workflow_id,
                candidate.created_by_agent_id,
                candidate.created_from_chat_id,
                candidate.created_from_channel,
                candidate.stability,
                candidate.reuse_value,
                candidate.sensitivity,
                candidate.confidence,
                candidate.status,
                candidate.approval_id,
                candidate.created_at,
                candidate.updated_at,
                candidate.metadata_json,
            ),
        )

    def get_memory_candidate(self, candidate_id: str) -> MemoryCandidate | None:
        """根据 candidate_id 查询 memory_candidates。"""
        row = self.storage.fetchone(
            "SELECT * FROM memory_candidates WHERE candidate_id = ?", (candidate_id,)
        )
        if not row:
            return None
        return MemoryCandidate(**dict(row))

    def list_memory_candidates(
        self, *, status: str | None = None, limit: int = 100
    ) -> list[MemoryCandidate]:
        """查询 memory_candidates（可选过滤 status）。"""
        if status:
            rows = self.storage.fetchall(
                "SELECT * FROM memory_candidates WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            rows = self.storage.fetchall(
                "SELECT * FROM memory_candidates ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        return [MemoryCandidate(**dict(row)) for row in rows]

    def update_candidate_status(
        self, candidate_id: str, status: str, approval_id: str | None = None
    ) -> None:
        """更新 candidate 状态（M5 审批流程用）。"""
        now = int(time.time())
        self.storage.execute(
            "UPDATE memory_candidates SET status = ?, approval_id = ?, updated_at = ? WHERE candidate_id = ?",
            (status, approval_id, now, candidate_id),
        )
