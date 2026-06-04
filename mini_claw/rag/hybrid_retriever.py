"""Hybrid retriever (Phase 8 M4).

Combines FTS5 lexical results with vector semantic results.

Score formula (RAG.md §6.6):

    score = 0.45 * fts_score
          + 0.45 * vector_score
          + 0.05 * recency_bonus
          + 0.05 * active_context_bonus

- ``fts_score``  : normalized BM25 inverse rank (1.0 = top hit)
- ``vector_score``: cosine-style similarity in [0, 1] from VectorHit
- ``recency``    : exponential decay over ``last_accessed_at``
- ``active_context`` : 1.0 when item is in session's ``active_contexts``

If vector backend is unavailable or returns no hits, falls back to
pure FTS path with full weight (the formula degrades gracefully).
"""

from __future__ import annotations

import math
import time
from typing import Any

from mini_claw.config import RagConfig
from mini_claw.rag.embeddings import EmbeddingProvider, embed_with_cache
from mini_claw.rag.models import RagSearchResult
from mini_claw.rag.permissions import check_search_scope
from mini_claw.rag.retriever import RagRetriever
from mini_claw.rag.vector_backend import VectorBackend, VectorHit
from mini_claw.storage.db import Database

__all__ = ["HybridRetriever"]


# Weights from RAG.md §6.6
W_FTS = 0.45
W_VEC = 0.45
W_RECENCY = 0.05
W_ACTIVE = 0.05

# Recency half-life: ~30 days
RECENCY_HALF_LIFE_SECONDS = 30 * 86400


class HybridRetriever:
    """FTS + vector hybrid search with re-ranking."""

    def __init__(
        self,
        storage: Database,
        config: RagConfig,
        backend: VectorBackend,
        embedder: EmbeddingProvider | None,
    ):
        self.storage = storage
        self.config = config
        self.backend = backend
        self.embedder = embedder
        self._fts = RagRetriever(storage, config)

    def search(
        self,
        query: str,
        *,
        ctx: dict[str, Any],
        namespace: str = "context",
        scope_filter: dict[str, Any] | None = None,
        top_k: int | None = None,
        include_archived: bool = False,
    ) -> tuple[list[RagSearchResult], str]:
        """Run hybrid search. Returns ``(results, error)``.

        - When ``hybrid_enabled=False`` or vector backend is ``none``,
          delegates to pure FTS retriever.
        - Vector failures are caught and the call degrades to FTS only.

        Note: memory_usage_events tracking for memory namespace is handled by
        RagManager.search_memory, which filters by confidence before tracking.
        """
        if not query.strip():
            return [], "query is empty"

        # Scope check (matches RagRetriever.search_context)
        scope_filter = scope_filter or {}
        allowed, deny_reason = check_search_scope(scope_filter, ctx, self.config)
        if not allowed:
            return [], deny_reason

        # Default scope
        if "owner_agent_id" not in scope_filter:
            scope_filter["owner_agent_id"] = ctx.get("agent_id", "")
        if "workspace_dir" not in scope_filter:
            scope_filter["workspace_dir"] = ctx.get("workspace_dir")
        if "namespace" not in scope_filter:
            scope_filter["namespace"] = namespace

        top_k_fts = (top_k or self.config.retrieval.context_top_k) * 2
        top_k_vec = (top_k or self.config.retrieval.context_top_k) * 2
        target_top_k = top_k or self.config.retrieval.context_top_k

        # 1. FTS results (always run)
        fts_results, fts_err = self._fts.search_context(
            query,
            ctx=ctx,
            namespace=namespace,
            scope_filter=scope_filter,
            top_k=top_k_fts,
            include_archived=include_archived,
        )
        if fts_err:
            return [], fts_err

        hybrid_on = (
            self.config.backend.hybrid_enabled
            and self.config.backend.vector_backend not in ("none", None)
            and self.embedder is not None
        )

        if not hybrid_on:
            # Apply recency + active boost on FTS-only path for consistency
            return (
                self._rerank(
                    query,
                    fts_results,
                    [],
                    ctx,
                    target_top_k,
                ),
                "",
            )

        # 2. Vector results (best-effort)
        vec_hits: list[VectorHit] = []
        try:
            qvec = embed_with_cache(self.embedder, query)
            if qvec:
                fetch_k = top_k_vec
                for _ in range(4):
                    candidates = self.backend.search(
                        qvec,
                        namespace=namespace,
                        top_k=fetch_k,
                        scope_filter=scope_filter,
                    )
                    vec_hits = self._filter_active_vector_hits(candidates, scope_filter)
                    if len(vec_hits) >= target_top_k or len(candidates) < fetch_k:
                        break
                    fetch_k *= 2
        except Exception:
            # Degrade to FTS only; don't bubble vector errors to caller
            vec_hits = []

        return (
            self._rerank(query, fts_results, vec_hits, ctx, target_top_k),
            "",
        )

    def _rerank(
        self,
        query: str,
        fts_results: list[RagSearchResult],
        vec_hits: list[VectorHit],
        ctx: dict[str, Any],
        top_k: int,
    ) -> list[RagSearchResult]:
        """Combine FTS + vector hits and apply weighted score."""
        # Index FTS results by chunk_id for fast merge
        by_chunk: dict[str, RagSearchResult] = {r.chunk_id: r for r in fts_results}

        # Normalize FTS rank → score in [0, 1]
        fts_scores: dict[str, float] = {}
        for i, r in enumerate(fts_results):
            fts_scores[r.chunk_id] = 1.0 / (i + 1)

        # Vector scores already normalized (1/(1+L2))
        vec_scores: dict[str, float] = {}
        for hit in vec_hits:
            vec_scores[hit.chunk_id] = hit.score

        # Active-context boost: query active_contexts table
        active_item_ids = self._active_item_ids(ctx)

        # For vector hits not in FTS (semantic-only), pull the chunk row
        # so we can return a full RagSearchResult.
        for hit in vec_hits:
            if hit.chunk_id not in by_chunk:
                row = self.storage.fetchone(
                    "SELECT c.chunk_id, c.item_id, c.content, c.start_line, c.end_line, "
                    "c.section_title, c.symbol_name, i.source_path, i.namespace, "
                    "i.source_type, i.sensitivity_level, i.last_accessed_at "
                    "FROM rag_chunks c JOIN rag_items i ON c.item_id = i.item_id "
                    "LEFT JOIN rag_item_chunk_versions m "
                    "ON m.item_id = c.item_id AND m.chunk_id = c.chunk_id "
                    "WHERE c.chunk_id = ? AND ("
                    "(m.chunk_id IS NOT NULL AND m.version = i.active_version AND m.status = 'active') "
                    "OR (m.chunk_id IS NULL AND c.version = i.active_version))",
                    (hit.chunk_id,),
                )
                if row is None:
                    continue
                by_chunk[hit.chunk_id] = RagSearchResult(
                    chunk_id=row["chunk_id"],
                    item_id=row["item_id"],
                    content=row["content"],
                    score=0.0,
                    source_path=row.get("source_path"),
                    start_line=row.get("start_line"),
                    end_line=row.get("end_line"),
                    section_title=row.get("section_title"),
                    symbol_name=row.get("symbol_name"),
                    namespace=row.get("namespace"),
                    source_type=row.get("source_type"),
                    sensitivity_level=row.get("sensitivity_level", "low"),
                )

        now = int(time.time())
        merged: list[RagSearchResult] = []
        for chunk_id, result in by_chunk.items():
            fts_s = fts_scores.get(chunk_id, 0.0)
            vec_s = vec_scores.get(chunk_id, 0.0)
            recency_s = self._recency_score(result.item_id, now)
            active_s = 1.0 if result.item_id in active_item_ids else 0.0

            combined = (
                W_FTS * fts_s
                + W_VEC * vec_s
                + W_RECENCY * recency_s
                + W_ACTIVE * active_s
            )
            merged.append(
                RagSearchResult(
                    chunk_id=result.chunk_id,
                    item_id=result.item_id,
                    content=result.content,
                    score=combined,
                    source_path=result.source_path,
                    start_line=result.start_line,
                    end_line=result.end_line,
                    section_title=result.section_title,
                    symbol_name=result.symbol_name,
                    namespace=result.namespace,
                    source_type=result.source_type,
                    sensitivity_level=result.sensitivity_level,
                )
            )

        merged.sort(key=lambda r: r.score, reverse=True)
        # Re-apply sensitivity redaction (same logic as RagRetriever)
        return self._fts._apply_sensitivity_redaction(merged[:top_k])

    def _filter_active_vector_hits(
        self, hits: list[VectorHit], scope_filter: dict[str, Any]
    ) -> list[VectorHit]:
        """Post-filter vector candidates against SQLite active mapping."""
        active: list[VectorHit] = []
        for hit in hits:
            where = [
                "c.chunk_id = ?",
                "((m.chunk_id IS NOT NULL AND m.version = i.active_version AND m.status = 'active') "
                "OR (m.chunk_id IS NULL AND c.version = i.active_version))",
            ]
            params: list[Any] = [hit.chunk_id]
            if scope_filter.get("namespace"):
                where.append("i.namespace = ?")
                params.append(scope_filter["namespace"])
            if scope_filter.get("owner_agent_id"):
                where.append("i.owner_agent_id = ?")
                params.append(scope_filter["owner_agent_id"])
            if scope_filter.get("workspace_dir"):
                where.append("i.workspace_dir = ?")
                params.append(str(scope_filter["workspace_dir"]))
            row = self.storage.fetchone(
                "SELECT c.chunk_id FROM rag_chunks c "
                "JOIN rag_items i ON c.item_id = i.item_id "
                "LEFT JOIN rag_item_chunk_versions m "
                "ON m.item_id = c.item_id AND m.chunk_id = c.chunk_id "
                f"WHERE {' AND '.join(where)}",
                tuple(params),
            )
            if row:
                active.append(hit)
        return active

    def _recency_score(self, item_id: str, now: int) -> float:
        """Exponential decay on ``last_accessed_at`` (defaults to 0 if missing)."""
        row = self.storage.fetchone(
            "SELECT COALESCE(last_accessed_at, updated_at, 0) AS ts "
            "FROM rag_items WHERE item_id = ?",
            (item_id,),
        )
        if not row:
            return 0.0
        ts = int(row["ts"] or 0)
        if ts == 0:
            return 0.0
        age = max(0, now - ts)
        # Half-life decay: score = 0.5 ** (age / half_life)
        return math.pow(0.5, age / RECENCY_HALF_LIFE_SECONDS)

    def _active_item_ids(self, ctx: dict[str, Any]) -> set[str]:
        """Return item_ids currently active in this session."""
        session_id = ctx.get("session_id")
        agent_id = ctx.get("agent_id")
        if not session_id or not agent_id:
            return set()
        rows = self.storage.fetchall(
            "SELECT context_id FROM active_contexts "
            "WHERE session_id = ? AND agent_id = ?",
            (session_id, agent_id),
        )
        return {r["context_id"] for r in rows}
