"""RAG retriever (Phase 8 M2).

RagRetriever searches indexed content using FTS5 full-text search (with fallback
to LIKE if FTS5 is unavailable). Returns RagSearchResult objects ranked by score.

M4 will extend this with vector similarity search + hybrid ranking.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from mini_claw.config import RagConfig
from mini_claw.rag.models import RagSearchResult
from mini_claw.rag.permissions import check_search_scope
from mini_claw.storage.db import Database

__all__ = ["RagRetriever"]


def _sanitize_fts_query(query: str) -> str:
    """Sanitize FTS5 query to prevent syntax errors (M2 plan: user feedback 5).

    FTS5 special characters (:, *, (, ), ") can cause MATCH parse errors.
    Strategy: wrap query in double quotes for phrase search, escape quotes inside.
    Multi-token: split and OR-join quoted terms.
    """
    if not query:
        return '""'

    # Escape existing double quotes
    query = query.replace('"', '""')

    # Simple phrase query: wrap in quotes
    # FTS5 phrase mode handles most special chars gracefully
    return f'"{query}"'


class RagRetriever:
    """Retrieve indexed content via FTS5 or LIKE fallback."""

    def __init__(
        self,
        storage: Database,
        config: RagConfig,
    ):
        self.storage = storage
        self.config = config

    def search_context(
        self,
        query: str,
        *,
        ctx: dict[str, Any],
        namespace: str = "context",
        scope_filter: dict[str, Any] | None = None,
        top_k: int | None = None,
        include_archived: bool = False,
    ) -> tuple[list[RagSearchResult], str]:
        """Search indexed context.

        Returns ``(results: list[RagSearchResult], error_message: str)``.

        Steps:
        1. Scope check (agent/workspace/session isolation)
        2. Build scope WHERE clause
        3. Try FTS5 MATCH query
        4. If FTS5 fails, fallback to LIKE
        5. For sensitivity_level='high' items, redact content to metadata only (M2 plan: user feedback 4)
        6. Return top_k ranked by score

        Note: memory_usage_events tracking for memory namespace is handled by
        RagManager.search_memory, which filters by confidence before tracking.
        """
        if not query.strip():
            return [], "query is empty"

        # 1. Scope check
        scope_filter = scope_filter or {}
        allowed, deny_reason = check_search_scope(scope_filter, ctx, self.config)
        if not allowed:
            return [], deny_reason

        # 2. Default scope: current agent + workspace + namespace
        if "owner_agent_id" not in scope_filter:
            scope_filter["owner_agent_id"] = ctx.get("agent_id", "")
        if "workspace_dir" not in scope_filter:
            scope_filter["workspace_dir"] = ctx.get("workspace_dir")
        if "namespace" not in scope_filter:
            scope_filter["namespace"] = namespace

        # 3. Try FTS5
        results = self._search_fts5(query, scope_filter, top_k, include_archived)
        if results is not None:
            return self._apply_sensitivity_redaction(results), ""

        # 4. Fallback to LIKE
        results = self._search_like(query, scope_filter, top_k, include_archived)
        return self._apply_sensitivity_redaction(results), ""

    def _search_fts5(
        self,
        query: str,
        scope_filter: dict[str, Any],
        top_k: int | None,
        include_archived: bool,
    ) -> list[RagSearchResult] | None:
        """FTS5 search with bm25 ranking. Returns None if FTS5 unavailable."""
        top_k = top_k or self.config.retrieval.context_top_k
        sanitized_query = _sanitize_fts_query(query)

        # Build WHERE clause for scope
        where_parts = ["i.namespace = ?"]
        params: list[Any] = [scope_filter.get("namespace", "context")]

        if scope_filter.get("owner_agent_id"):
            where_parts.append("i.owner_agent_id = ?")
            params.append(scope_filter["owner_agent_id"])

        if scope_filter.get("workspace_dir"):
            where_parts.append("i.workspace_dir = ?")
            params.append(str(scope_filter["workspace_dir"]))

        if scope_filter.get("session_id"):
            where_parts.append("i.session_id = ?")
            params.append(scope_filter["session_id"])

        # Phase 9 M9.5: scope_type filter (agent/workspace/user/session)
        if scope_filter.get("scope_type"):
            where_parts.append("i.scope_type = ?")
            params.append(scope_filter["scope_type"])

        # Phase 9 P0.2: channel_name filter (multi-channel isolation)
        if scope_filter.get("channel_name"):
            where_parts.append("(i.channel_name = ? OR i.channel_name IS NULL)")
            params.append(scope_filter["channel_name"])

        # Status filter
        if include_archived:
            where_parts.append("i.status IN ('active', 'warm', 'archived')")
        else:
            where_parts.append("i.status IN ('active', 'warm')")

        # Only search active chunks. New indexes use rag_item_chunk_versions;
        # old indexes without mapping fall back to c.version = active_version.
        where_parts.append(
            "((m.chunk_id IS NOT NULL AND m.version = i.active_version AND m.status = 'active') "
            "OR (m.chunk_id IS NULL AND c.version = i.active_version))"
        )

        where_clause = " AND ".join(where_parts)

        sql = f"""
        SELECT
            c.chunk_id,
            c.item_id,
            c.content,
            fts.rank AS score,
            i.source_path,
            c.start_line,
            c.end_line,
            c.section_title,
            c.symbol_name,
            i.namespace,
            i.source_type,
            i.sensitivity_level
        FROM rag_chunks_fts fts
        JOIN rag_chunks c ON fts.chunk_id = c.chunk_id
        JOIN rag_items i ON c.item_id = i.item_id
        LEFT JOIN rag_item_chunk_versions m
          ON m.item_id = c.item_id AND m.chunk_id = c.chunk_id
        WHERE {where_clause}
          AND fts.content MATCH ?
        ORDER BY fts.rank
        LIMIT ?
        """

        try:
            params.append(sanitized_query)
            params.append(top_k)
            rows = self.storage.fetchall(sql, tuple(params))
            return [
                RagSearchResult(
                    chunk_id=row["chunk_id"],
                    item_id=row["item_id"],
                    content=row["content"],
                    score=float(row["score"]) if row.get("score") else 0.0,
                    source_path=row.get("source_path"),
                    start_line=row.get("start_line"),
                    end_line=row.get("end_line"),
                    section_title=row.get("section_title"),
                    symbol_name=row.get("symbol_name"),
                    namespace=row.get("namespace"),
                    source_type=row.get("source_type"),
                    sensitivity_level=row.get("sensitivity_level", "low"),
                )
                for row in rows
            ]
        except sqlite3.OperationalError:
            # FTS5 not available or query syntax error
            return None

    def _search_like(
        self,
        query: str,
        scope_filter: dict[str, Any],
        top_k: int | None,
        include_archived: bool,
    ) -> list[RagSearchResult]:
        """Fallback LIKE search (no ranking, just text match)."""
        top_k = top_k or self.config.retrieval.context_top_k

        where_parts = ["i.namespace = ?"]
        params: list[Any] = [scope_filter.get("namespace", "context")]

        if scope_filter.get("owner_agent_id"):
            where_parts.append("i.owner_agent_id = ?")
            params.append(scope_filter["owner_agent_id"])

        if scope_filter.get("workspace_dir"):
            where_parts.append("i.workspace_dir = ?")
            params.append(str(scope_filter["workspace_dir"]))

        if scope_filter.get("session_id"):
            where_parts.append("i.session_id = ?")
            params.append(scope_filter["session_id"])

        # Phase 9 M9.5: scope_type filter
        if scope_filter.get("scope_type"):
            where_parts.append("i.scope_type = ?")
            params.append(scope_filter["scope_type"])

        # Phase 9 P0.2: channel_name filter
        if scope_filter.get("channel_name"):
            where_parts.append("(i.channel_name = ? OR i.channel_name IS NULL)")
            params.append(scope_filter["channel_name"])

        if include_archived:
            where_parts.append("i.status IN ('active', 'warm', 'archived')")
        else:
            where_parts.append("i.status IN ('active', 'warm')")

        where_parts.append(
            "((m.chunk_id IS NOT NULL AND m.version = i.active_version AND m.status = 'active') "
            "OR (m.chunk_id IS NULL AND c.version = i.active_version))"
        )
        where_parts.append("c.content LIKE ?")

        where_clause = " AND ".join(where_parts)

        sql = f"""
        SELECT
            c.chunk_id,
            c.item_id,
            c.content,
            i.source_path,
            c.start_line,
            c.end_line,
            c.section_title,
            c.symbol_name,
            i.namespace,
            i.source_type,
            i.sensitivity_level
        FROM rag_chunks c
        JOIN rag_items i ON c.item_id = i.item_id
        LEFT JOIN rag_item_chunk_versions m
          ON m.item_id = c.item_id AND m.chunk_id = c.chunk_id
        WHERE {where_clause}
        LIMIT ?
        """

        params.append(f"%{query}%")
        params.append(top_k)
        rows = self.storage.fetchall(sql, tuple(params))

        return [
            RagSearchResult(
                chunk_id=row["chunk_id"],
                item_id=row["item_id"],
                content=row["content"],
                score=0.0,  # LIKE has no ranking
                source_path=row.get("source_path"),
                start_line=row.get("start_line"),
                end_line=row.get("end_line"),
                section_title=row.get("section_title"),
                symbol_name=row.get("symbol_name"),
                namespace=row.get("namespace"),
                source_type=row.get("source_type"),
                sensitivity_level=row.get("sensitivity_level", "low"),
            )
            for row in rows
        ]

    def _apply_sensitivity_redaction(
        self, results: list[RagSearchResult]
    ) -> list[RagSearchResult]:
        """Redact content for high-sensitivity items (M2 plan: user feedback 4).

        If sensitivity_level == 'high', replace content with metadata placeholder.
        LLM must call read_sensitive_context (L3) to get actual content.
        """
        redacted: list[RagSearchResult] = []
        for r in results:
            if r.sensitivity_level == "high":
                # Redact content, keep metadata
                redacted.append(
                    RagSearchResult(
                        chunk_id=r.chunk_id,
                        item_id=r.item_id,
                        content=f"[REDACTED: high-sensitivity content. Use read_sensitive_context tool to access. source={r.source_path}, lines {r.start_line}-{r.end_line}]",
                        score=r.score,
                        source_path=r.source_path,
                        start_line=r.start_line,
                        end_line=r.end_line,
                        section_title=r.section_title,
                        symbol_name=r.symbol_name,
                        namespace=r.namespace,
                        source_type=r.source_type,
                        sensitivity_level=r.sensitivity_level,
                    )
                )
            else:
                redacted.append(r)
        return redacted
