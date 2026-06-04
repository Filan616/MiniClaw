"""RAG manager facade (Phase 8 M2).

RagManager is the main entry point for RAG operations, wiring together
indexer, retriever, store, and config. Tool handlers call RagManager methods.
"""

from __future__ import annotations

import time
from typing import Any

from mini_claw.config import RagConfig
from mini_claw.permissions.policy import PermissionPolicy
from mini_claw.rag.embeddings import get_embedding_provider
from mini_claw.rag.health import RagHealthManager
from mini_claw.rag.hybrid_retriever import HybridRetriever
from mini_claw.rag.indexer import RagIndexer
from mini_claw.rag.lifecycle import RagLifecycle
from mini_claw.rag.models import ActiveContext, RagItem, RagSearchResult, RagStatus
from mini_claw.rag.reindex import RagReindexer
from mini_claw.rag.retriever import RagRetriever
from mini_claw.permissions.approval_store import ApprovalStore
from mini_claw.rag.memory import MemoryStore, consolidate
from mini_claw.rag.redaction import redact_for_rag
from mini_claw.rag.store import RagStore
from mini_claw.rag.vector_backend import build_vector_backend
from mini_claw.storage.db import Database

__all__ = ["RagManager"]


class RagManager:
    """RAG subsystem facade."""

    def __init__(
        self,
        storage: Database,
        config: RagConfig,
        policy: PermissionPolicy,
    ):
        self.config = config
        self.storage = storage
        self.store = RagStore(storage)
        # Phase 8 M4: vector backend + embedding provider
        self.vector_backend = build_vector_backend(config)
        try:
            self.embedder = get_embedding_provider(config)
        except Exception:
            # Defer model load failures to first use
            self.embedder = None
        self.indexer = RagIndexer(
            self.store,
            config,
            policy,
            vector_backend=self.vector_backend,
            embedder=self.embedder,
        )
        self.retriever = RagRetriever(storage, config)
        self.hybrid = HybridRetriever(
            storage, config, self.vector_backend, self.embedder
        )
        # Phase 8 M3
        self.reindexer = RagReindexer(
            self.store,
            storage,
            config,
            policy,
            vector_backend=self.vector_backend,
            embedder=self.embedder,
        )
        self.lifecycle = RagLifecycle(storage, config)
        # Phase 8 M4.5: health observability
        self.health = RagHealthManager(
            storage, config, self.vector_backend, self.embedder
        )
        # Phase 8 M5: memory subsystem (lazy: only constructed when memory enabled)
        self._memory_store: MemoryStore | None = None
        if config.namespaces.memory_enabled:
            self._memory_store = MemoryStore(
                self.store, ApprovalStore(storage)
            )

    # ========== Index operations ==========

    def index_context(
        self,
        path: str,
        *,
        ctx: dict[str, Any],
        title: str | None = None,
    ) -> tuple[str | None, str]:
        """Index a file as context.

        Returns ``(item_id | None, error_message)``.
        """
        if not self.config.enabled or not self.config.namespaces.context_enabled:
            return None, "context RAG is disabled"

        return self.indexer.index_path(
            path,
            ctx=ctx,
            namespace="context",
            source_type=None,  # auto-detect
            scope_type="workspace",
            scope_id=str(ctx.get("workspace_dir", "unknown")),
            title=title,
        )

    # ========== Search operations ==========

    def search_context(
        self,
        query: str,
        *,
        ctx: dict[str, Any],
        top_k: int | None = None,
        include_archived: bool = False,
    ) -> tuple[list[RagSearchResult], str]:
        """Search indexed context.

        Returns ``(results: list[RagSearchResult], error_message: str)``.

        Phase 8 M4: when ``rag.backend.hybrid_enabled=True`` and a real
        vector backend is configured, route through HybridRetriever for
        FTS + vector merge. Otherwise stay on the M2 FTS-only path.
        """
        if not self.config.enabled or not self.config.namespaces.context_enabled:
            return [], "context RAG is disabled"

        if (
            self.config.backend.hybrid_enabled
            and self.config.backend.vector_backend not in ("none", None)
        ):
            return self.hybrid.search(
                query,
                ctx=ctx,
                namespace="context",
                scope_filter=None,
                top_k=top_k,
                include_archived=include_archived,
            )

        return self.retriever.search_context(
            query,
            ctx=ctx,
            namespace="context",
            scope_filter=None,
            top_k=top_k,
            include_archived=include_archived,
        )

    # ========== List / inspect operations ==========

    def list_contexts(
        self,
        *,
        ctx: dict[str, Any],
        status: str | None = None,
        limit: int = 100,
    ) -> list[RagItem]:
        """List indexed contexts owned by current agent."""
        if not self.config.enabled or not self.config.namespaces.context_enabled:
            return []

        return self.store.list_by_scope(
            namespace="context",
            owner_agent_id=ctx.get("agent_id"),
            status=status,
            limit=limit,
        )

    def inspect_context(
        self,
        context_id: str,
        *,
        ctx: dict[str, Any],
    ) -> tuple[RagItem | None, str]:
        """Get metadata for a context item."""
        if not self.config.enabled or not self.config.namespaces.context_enabled:
            return None, "context RAG is disabled"

        item = self.store.get_item(context_id)
        if item is None:
            return None, "context not found"

        # Owner check
        if item.owner_agent_id != ctx.get("agent_id"):
            return None, "cannot inspect context owned by another agent"

        return item, ""

    # ========== Archive / delete operations ==========

    def archive_context(
        self,
        context_id: str,
        *,
        ctx: dict[str, Any],
    ) -> tuple[bool, str]:
        """Archive a context item (mark status='archived')."""
        if not self.config.enabled or not self.config.namespaces.context_enabled:
            return False, "context RAG is disabled"

        item = self.store.get_item(context_id)
        if item is None:
            return False, "context not found"

        # Owner check
        if item.owner_agent_id != ctx.get("agent_id"):
            return False, "cannot archive context owned by another agent"

        self.store.mark_status(context_id, "archived")
        return True, ""

    def delete_context(
        self,
        context_id: str,
        *,
        ctx: dict[str, Any],
    ) -> tuple[bool, str]:
        """Delete a context item (M2 plan: 7-step transaction, user feedback 6).

        Steps:
        1. L3 approval check (done by PermissionGate before calling this)
        2. Mark status='deleted_pending'
        3. Delete vector backend (M4, M2 noop)
        4. Delete rag_chunks_fts
        5. Delete rag_chunks
        6. Mark status='deleted' or delete row (depends on config.lifecycle.keep_tombstone)
        7. Audit (done by caller)
        """
        if not self.config.enabled or not self.config.namespaces.context_enabled:
            return False, "context RAG is disabled"

        item = self.store.get_item(context_id)
        if item is None:
            return False, "context not found"

        # Owner check
        if item.owner_agent_id != ctx.get("agent_id"):
            return False, "cannot delete context owned by another agent"

        try:
            # Step 2: Mark deleted_pending
            self.store.mark_status(context_id, "deleted_pending")

            # Step 3: Vector backend (Phase 8 M4)
            try:
                if self.vector_backend is not None and getattr(
                    self.vector_backend, "name", "none"
                ) != "none":
                    self.vector_backend.delete_item(context_id)
            except Exception:
                # Vector deletes are best-effort; FTS+chunk delete still proceeds.
                pass
            # Also clear rag_embeddings metadata rows (always done; cheap)
            try:
                self.storage.execute(
                    "DELETE FROM rag_embeddings WHERE item_id = ?", (context_id,)
                )
                self.storage.execute(
                    "DELETE FROM rag_item_chunk_versions WHERE item_id = ?", (context_id,)
                )
                self.storage.execute(
                    "DELETE FROM rag_reindex_diff_chunks WHERE item_id = ?", (context_id,)
                )
                self.storage.execute(
                    "DELETE FROM rag_reindex_diffs WHERE item_id = ?", (context_id,)
                )
            except Exception:
                pass

            # Step 4: Delete FTS
            try:
                self.store.storage.execute(
                    "DELETE FROM rag_chunks_fts WHERE item_id = ?", (context_id,)
                )
            except Exception:
                # FTS5 may not exist
                pass

            # Step 5: Delete chunks
            self.store.delete_chunks(context_id)

            # Step 6: Tombstone or delete
            self.store.delete_item(
                context_id, keep_tombstone=self.config.lifecycle.keep_tombstone
            )

            return True, ""

        except Exception as exc:
            # Step failed, mark as delete_failed
            self.store.mark_status(context_id, "delete_failed", error=str(exc))
            return False, f"delete failed: {exc}"

    def clear_context(
        self,
        *,
        ctx: dict[str, Any],
    ) -> tuple[int, str]:
        """Clear all active contexts for current agent in current session.

        Returns ``(deleted_count, error_message)``.
        """
        if not self.config.enabled or not self.config.namespaces.context_enabled:
            return 0, "context RAG is disabled"

        session_id = ctx.get("session_id")
        agent_id = ctx.get("agent_id")
        if not session_id or not agent_id:
            return 0, "missing session_id or agent_id"

        # Find all active contexts for this session
        active = self.store.storage.fetchall(
            "SELECT context_id FROM active_contexts WHERE session_id = ? AND agent_id = ?",
            (session_id, agent_id),
        )

        count = 0
        for row in active:
            context_id = row["context_id"]
            # Archive, not delete (clear is L2, delete is L3)
            self.store.mark_status(context_id, "warm")
            self.store.clear_active_context(session_id, agent_id, context_id)
            count += 1

        return count, ""

    # ========== Read sensitive content (M2 plan: user feedback 4) ==========

    def read_sensitive_context(
        self,
        context_id: str,
        chunk_id: str,
        *,
        ctx: dict[str, Any],
    ) -> tuple[str | None, str]:
        """Read full content of a high-sensitivity chunk (L3 approval required).

        Returns ``(content | None, error_message)``.
        """
        if not self.config.enabled or not self.config.namespaces.context_enabled:
            return None, "context RAG is disabled"

        # Check ownership
        item = self.store.get_item(context_id)
        if item is None:
            return None, "context not found"
        if item.owner_agent_id != ctx.get("agent_id"):
            return None, "cannot read context owned by another agent"

        # Get chunk
        chunks = self.store.get_active_chunks(context_id)
        chunk = next((c for c in chunks if c.chunk_id == chunk_id), None)
        if chunk is None:
            return None, "chunk not found"

        # Phase 9 M9.6: Track memory_usage_event if reading memory item
        if item.namespace == "memory":
            try:
                import json
                import time
                import uuid
                now = int(time.time())
                event_id = f"mue-{uuid.uuid4().hex[:12]}"
                context_json = json.dumps({
                    "chat_id": ctx.get("chat_id"),
                    "agent_id": ctx.get("agent_id"),
                    "channel_name": ctx.get("channel_name"),
                    "retrieval_type": "read_sensitive_context",
                    "chunk_id": chunk_id,
                })
                self.storage.execute(
                    "INSERT INTO memory_usage_events "
                    "(event_id, item_id, accessed_at, context_json) "
                    "VALUES (?, ?, ?, ?)",
                    (event_id, context_id, now, context_json),
                )
            except Exception:
                pass

        return chunk.content, ""

    # ========== Phase 8 M3: reindex / rebind / use / cleanup ==========

    def reindex_context(
        self,
        context_id: str,
        *,
        ctx: dict[str, Any],
        dry_run: bool = False,
    ) -> tuple[bool, str]:
        """Re-chunk and re-index an item's source file with atomic version swap.

        Returns ``(success, error_message)``.
        """
        if not self.config.enabled or not self.config.namespaces.context_enabled:
            return False, "context RAG is disabled"
        return self.reindexer.reindex(context_id, ctx=ctx, dry_run=dry_run)

    def diff_context(
        self,
        context_id: str,
        *,
        ctx: dict[str, Any],
        last: bool = False,
    ) -> tuple[bool, str]:
        if not self.config.enabled or not self.config.namespaces.context_enabled:
            return False, "context RAG is disabled"
        item = self.store.get_item(context_id)
        if item is None:
            return False, "context not found"
        if item.owner_agent_id != ctx.get("agent_id"):
            return False, "cannot diff context owned by another agent"
        if last:
            diff, chunks = self.reindexer.last_diff(context_id)
            if diff is None:
                return False, "no previous reindex diff"
            return True, (
                f"last_diff={diff.diff_id}; mode={diff.mode}; status={diff.status}; "
                f"added={diff.added_count}; updated={diff.updated_count}; "
                f"deleted={diff.deleted_count}; reused={diff.reused_count}; "
                f"uncertain={diff.uncertain_count}; rows={len(chunks)}"
            )
        return self.reindexer.reindex(context_id, ctx=ctx, dry_run=True)

    def reembed_context(
        self,
        context_id: str,
        *,
        ctx: dict[str, Any],
    ) -> tuple[bool, str]:
        if not self.config.enabled or not self.config.namespaces.context_enabled:
            return False, "context RAG is disabled"
        item = self.store.get_item(context_id)
        if item is None:
            return False, "context not found"
        if item.owner_agent_id != ctx.get("agent_id"):
            return False, "cannot reembed context owned by another agent"
        if (
            self.embedder is None
            or self.vector_backend is None
            or getattr(self.vector_backend, "name", "none") == "none"
            or not self.config.embedding.enabled
        ):
            return False, "vector embedding is disabled"
        chunks = self.store.get_active_chunks(context_id)
        if not chunks:
            return False, "no active chunks"
        vectors = self.embedder.embed_texts([c.content for c in chunks])
        if not vectors or len(vectors) != len(chunks):
            return False, "embedding provider returned invalid vectors"
        self.vector_backend.upsert_chunks(
            chunks,
            vectors,
            namespace=item.namespace,
            source_type=item.source_type,
        )
        now = int(time.time())
        self.storage.execute(
            "UPDATE rag_items SET embedding_model = ?, updated_at = ? WHERE item_id = ?",
            (getattr(self.embedder, "model", None), now, context_id),
        )
        return True, f"reembedded {len(chunks)} active chunks"

    def rebind_context(
        self,
        context_id: str,
        new_path: str,
        *,
        ctx: dict[str, Any],
    ) -> tuple[bool, str]:
        """Update an item's source_path. If hash matches, simple rebind; else
        return error suggesting reindex.
        """
        if not self.config.enabled or not self.config.namespaces.context_enabled:
            return False, "context RAG is disabled"
        return self.reindexer.rebind(context_id, new_path, ctx=ctx)

    def use_context(
        self,
        context_id: str,
        *,
        ctx: dict[str, Any],
    ) -> tuple[bool, str]:
        """Set *context_id* as active for the current session.

        Active contexts boost retrieval scores and feed query routing in M5.
        """
        if not self.config.enabled or not self.config.namespaces.context_enabled:
            return False, "context RAG is disabled"

        item = self.store.get_item(context_id)
        if item is None:
            return False, "context not found"
        if item.owner_agent_id != ctx.get("agent_id"):
            return False, "cannot use context owned by another agent"

        session_id = ctx.get("session_id")
        agent_id = ctx.get("agent_id")
        if not session_id or not agent_id:
            return False, "missing session_id or agent_id"

        active = ActiveContext(
            session_id=session_id,
            agent_id=agent_id,
            context_id=context_id,
            context_type=item.source_type or "document",
            title=item.title,
            activated_at=int(time.time()),
        )
        self.store.set_active_context(active)
        return True, ""

    def cleanup_lifecycle(self) -> dict[str, int]:
        """Run a single lifecycle pass (active→warm→archived→cold→deleted).

        Pinned items are excluded. Returns transition counts.
        """
        if not self.config.enabled:
            return {}
        return self.lifecycle.cleanup_expired()

    # ========== Phase 8 M4.5: health observability ==========

    def status(self) -> RagStatus:
        """Return a fresh ``RagStatus`` snapshot."""
        return self.health.summarize()

    def status_text(self) -> str:
        """Render the human-readable status table (used by ``/rag status``)."""
        return self.health.render_text()

    def status_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable status (used by CLI ``--json``)."""
        return self.health.to_dict()

    # ========== Phase 8 M5: Memory RAG ==========

    @property
    def memory(self) -> MemoryStore | None:
        """Memory store handle (None when memory namespace is disabled)."""
        return self._memory_store

    def remember(
        self,
        content: str,
        *,
        ctx: dict[str, Any],
        memory_type: str = "user_preference",
        scope_type: str = "agent",
        scope_id: str | None = None,
    ) -> tuple[str | None, str | None, str]:
        """Submit an explicit user memory candidate (``/memory remember``).

        Returns ``(candidate_id_or_None, approval_id_or_None, status_word)``.

        ``status_word`` matches :meth:`MemoryStore.submit_explicit`:
        ``"submitted"`` / ``"rejected:<category>"`` / ``"stored_direct"``.
        """
        if (
            not self.config.enabled
            or not self.config.namespaces.memory_enabled
            or self._memory_store is None
        ):
            return None, None, "rejected:disabled"
        cand, approval_id, status = self._memory_store.submit_explicit(
            content,
            memory_type=memory_type,
            agent_id=ctx.get("agent_id", "unknown"),
            chat_id=ctx.get("chat_id", "unknown"),
            channel=ctx.get("channel_name"),
            scope_type=scope_type,
            scope_id=scope_id,
        )
        return (cand.candidate_id if cand else None), approval_id, status

    def approve_memory(self, candidate_id: str) -> tuple[str | None, str]:
        """Promote a pending candidate to ``rag_items(namespace='memory')``.

        Returns ``(item_id_or_None, error_message)``.
        """
        if self._memory_store is None:
            return None, "memory namespace disabled"
        return self._memory_store.commit_candidate(candidate_id)

    def reject_memory(self, candidate_id: str) -> bool:
        if self._memory_store is None:
            return False
        return self._memory_store.reject_candidate(candidate_id)

    def list_memories(
        self,
        *,
        ctx: dict[str, Any],
        status: str = "active",
        limit: int = 100,
    ) -> list:
        """List long-term memories owned by the current agent."""
        if self._memory_store is None:
            return []
        return self._memory_store.list_memories(
            owner_agent_id=ctx.get("agent_id", "unknown"),
            status=status,
            limit=limit,
        )

    def list_pending_memories(self, limit: int = 50) -> list:
        if self._memory_store is None:
            return []
        return self._memory_store.list_pending(limit=limit)

    def inspect_memory(
        self, memory_id: str, *, ctx: dict[str, Any]
    ) -> tuple[Any, str]:
        if self._memory_store is None:
            return None, "memory namespace disabled"
        item = self.store.get_item(memory_id)
        if item is None or item.namespace != "memory":
            return None, "memory not found"
        if item.owner_agent_id != ctx.get("agent_id"):
            return None, "cannot inspect memory owned by another agent"

        # Phase 9 M9.6: Track memory_usage_event for inspect
        try:
            import json
            import time
            import uuid
            now = int(time.time())
            event_id = f"mue-{uuid.uuid4().hex[:12]}"
            context_json = json.dumps({
                "chat_id": ctx.get("chat_id"),
                "agent_id": ctx.get("agent_id"),
                "channel_name": ctx.get("channel_name"),
                "retrieval_type": "inspect_memory",
            })
            self.storage.execute(
                "INSERT INTO memory_usage_events "
                "(event_id, item_id, accessed_at, context_json) "
                "VALUES (?, ?, ?, ?)",
                (event_id, memory_id, now, context_json),
            )
        except Exception:
            pass

        return item, ""

    def delete_memory(self, memory_id: str, *, ctx: dict[str, Any]) -> tuple[bool, str]:
        if self._memory_store is None:
            return False, "memory namespace disabled"
        item = self.store.get_item(memory_id)
        if item is None or item.namespace != "memory":
            return False, "memory not found"
        if item.owner_agent_id != ctx.get("agent_id"):
            return False, "cannot delete memory owned by another agent"
        self.store.delete_chunks(memory_id)
        try:
            self.storage.execute(
                "DELETE FROM rag_chunks_fts WHERE item_id = ?", (memory_id,)
            )
        except Exception:
            pass
        self.store.delete_item(
            memory_id, keep_tombstone=self.config.lifecycle.keep_tombstone
        )
        return True, ""

    def pin_memory(self, memory_id: str, *, ctx: dict[str, Any]) -> tuple[bool, str]:
        if self._memory_store is None:
            return False, "memory namespace disabled"
        item = self.store.get_item(memory_id)
        if item is None or item.namespace != "memory":
            return False, "memory not found"
        if item.owner_agent_id != ctx.get("agent_id"):
            return False, "cannot pin memory owned by another agent"
        import time as _t
        self.storage.execute(
            "UPDATE rag_items SET pinned = 1, updated_at = ? WHERE item_id = ?",
            (int(_t.time()), memory_id),
        )
        return True, ""

    def unpin_memory(self, memory_id: str, *, ctx: dict[str, Any]) -> tuple[bool, str]:
        if self._memory_store is None:
            return False, "memory namespace disabled"
        item = self.store.get_item(memory_id)
        if item is None or item.namespace != "memory":
            return False, "memory not found"
        if item.owner_agent_id != ctx.get("agent_id"):
            return False, "cannot unpin memory owned by another agent"
        import time as _t
        self.storage.execute(
            "UPDATE rag_items SET pinned = 0, updated_at = ? WHERE item_id = ?",
            (int(_t.time()), memory_id),
        )
        return True, ""

    def archive_memory(self, memory_id: str, *, ctx: dict[str, Any]) -> tuple[bool, str]:
        """Phase 9 M9.2: Archive a memory (set status='archived').

        Archived memories are excluded from search but remain in DB for recovery.
        """
        if self._memory_store is None:
            return False, "memory namespace disabled"
        item = self.store.get_item(memory_id)
        if item is None or item.namespace != "memory":
            return False, "memory not found"
        if item.owner_agent_id != ctx.get("agent_id"):
            return False, "cannot archive memory owned by another agent"
        import time as _t
        self.storage.execute(
            "UPDATE rag_items SET status = 'archived', updated_at = ? WHERE item_id = ?",
            (int(_t.time()), memory_id),
        )
        return True, ""

    def clear_memory_scope(
        self,
        scope_type: str,
        scope_id: str | None,
        *,
        ctx: dict[str, Any],
        dry_run: bool = True,
        hard_delete: bool = False,
    ) -> tuple[list[dict[str, Any]], str]:
        """Phase 9 M9.2: Clear memories by scope (default: archive, not delete).

        Args:
            scope_type: 'user' | 'workspace' | 'session' | 'agent'
            scope_id: identifier for the scope (agent_id / workspace_dir / etc)
            ctx: agent context
            dry_run: if True, return preview without executing
            hard_delete: if True, DELETE rows; else SET status='archived'

        Returns:
            (preview_list, error_message)
            preview_list: [{"memory_id", "type", "scope", "content_preview"}, ...]
        """
        if self._memory_store is None:
            return [], "memory namespace disabled"

        # Phase 9 M9.2: Hard-delete config guard
        if hard_delete and not self.config.memory_control.allow_hard_delete:
            return [], "Hard delete disabled in config"

        # Build WHERE clause based on scope
        where_parts = ["namespace = 'memory'", "status = 'active'"]
        params: list[Any] = []

        if scope_type == "agent":
            where_parts.append("owner_agent_id = ?")
            params.append(scope_id or ctx.get("agent_id"))
        elif scope_type == "workspace":
            where_parts.append("workspace_dir = ?")
            params.append(scope_id or ctx.get("workspace_dir"))
        elif scope_type == "session":
            where_parts.append("session_id = ?")
            params.append(scope_id or ctx.get("session_id"))
        elif scope_type == "user":
            # User scope: all memories owned by this agent (cross-workspace)
            where_parts.append("owner_agent_id = ?")
            params.append(ctx.get("agent_id"))
        else:
            return [], f"Unknown scope_type: {scope_type}"

        where_clause = " AND ".join(where_parts)
        rows = self.storage.fetchall(
            f"SELECT item_id, source_type, scope_type, scope_id, title FROM rag_items WHERE {where_clause}",
            tuple(params),
        )

        preview = []
        for row in rows:
            preview.append(
                {
                    "memory_id": row["item_id"],
                    "type": row["source_type"],
                    "scope": f"{row['scope_type']}:{row['scope_id']}",
                    "content_preview": (row["title"] or "")[:80],
                }
            )

        if dry_run:
            return preview, ""

        # Execute: archive or hard_delete
        item_ids = [r["memory_id"] for r in preview]
        if not item_ids:
            return preview, ""

        if hard_delete:
            # Physical delete
            placeholders = ",".join("?" for _ in item_ids)
            self.storage.execute(
                f"DELETE FROM rag_chunks WHERE item_id IN ({placeholders})",
                tuple(item_ids),
            )
            try:
                self.storage.execute(
                    f"DELETE FROM rag_chunks_fts WHERE item_id IN ({placeholders})",
                    tuple(item_ids),
                )
            except Exception:
                pass
            self.storage.execute(
                f"DELETE FROM rag_items WHERE item_id IN ({placeholders})",
                tuple(item_ids),
            )
        else:
            # Archive
            import time as _t
            placeholders = ",".join("?" for _ in item_ids)
            self.storage.execute(
                f"UPDATE rag_items SET status = 'archived', updated_at = ? WHERE item_id IN ({placeholders})",
                (_t.time(), *item_ids),
            )

        return preview, ""

    def export_memories(
        self,
        scope_type: str | None = None,
        scope_id: str | None = None,
        *,
        ctx: dict[str, Any],
        full_content: bool = False,
        scope: str | None = None,
        format: str | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        """Phase 9 M9.2: Export memories (redacted by default, full requires approval).

        Accepts two calling conventions:
        - Positional: ``export_memories("agent", scope_id, ctx=...)`` (router path)
        - Keyword:    ``export_memories(scope="agent", format="redacted", ctx=...)`` (test/programmatic path)
        """
        # Normalize scope/scope_type
        if scope_type is None and scope is not None:
            scope_type = scope
        if scope_type is None:
            return [], "scope is required"
        # Normalize format → full_content
        if format is not None:
            if format in ("full", "full_content"):
                full_content = True
            elif format in ("redacted", "json"):
                full_content = False
        if self._memory_store is None:
            return [], "memory namespace disabled"

        where_parts = ["namespace = 'memory'", "status = 'active'"]
        params: list[Any] = []

        if scope_type == "agent":
            where_parts.append("owner_agent_id = ?")
            params.append(scope_id or ctx.get("agent_id"))
        elif scope_type == "workspace":
            where_parts.append("workspace_dir = ?")
            params.append(scope_id or ctx.get("workspace_dir"))
        elif scope_type == "user":
            where_parts.append("owner_agent_id = ?")
            params.append(ctx.get("agent_id"))
        elif scope_type == "all":
            # All visible to user (same channel)
            where_parts.append("channel_name = ?")
            params.append(ctx.get("channel_name"))
        else:
            return [], f"Unknown scope: {scope_type}"

        where_clause = " AND ".join(where_parts)
        rows = self.storage.fetchall(
            f"SELECT item_id, source_type, scope_type, scope_id, title, created_at, confidence, pinned FROM rag_items WHERE {where_clause}",
            tuple(params),
        )

        export_data = []
        for row in rows:
            entry = {
                "memory_id": row["item_id"],
                "type": row["source_type"],
                "scope": f"{row['scope_type']}:{row['scope_id']}",
                "title": row["title"],
                "created_at": row["created_at"],
                "confidence": row["confidence"],
                "pinned": bool(row["pinned"]),
            }
            if full_content:
                # Fetch actual content (full_content requires L3 approval)
                chunks = self.storage.fetchall(
                    "SELECT content FROM rag_chunks WHERE item_id = ? ORDER BY chunk_index",
                    (row["item_id"],),
                )
                entry["content"] = "\n".join(c["content"] for c in chunks)
            else:
                # Redacted export: use redaction.redact_for_rag instead of placeholder
                chunks = self.storage.fetchall(
                    "SELECT content FROM rag_chunks WHERE item_id = ? ORDER BY chunk_index",
                    (row["item_id"],),
                )
                full_text = "\n".join(c["content"] for c in chunks)
                redacted_text, was_redacted = redact_for_rag(full_text)
                entry["content"] = redacted_text
                entry["was_redacted"] = was_redacted
            export_data.append(entry)

        return export_data, ""

    def list_memory_candidates(
        self,
        *,
        memory_type: str | None = None,
        older_than_days: int | None = None,
    ) -> list[dict[str, Any]]:
        """Phase 9 M9.2: List pending memory candidates."""
        if self._memory_store is None:
            return []

        where_parts = ["status = 'pending'"]
        params: list[Any] = []

        if memory_type:
            where_parts.append("memory_type = ?")
            params.append(memory_type)

        if older_than_days:
            import time as _t
            cutoff = int(_t.time()) - (older_than_days * 86400)
            where_parts.append("created_at < ?")
            params.append(cutoff)

        where_clause = " AND ".join(where_parts)
        rows = self.storage.fetchall(
            f"SELECT candidate_id, memory_type, content, created_at, sensitivity FROM memory_candidates WHERE {where_clause} ORDER BY created_at DESC",
            tuple(params),
        )

        return [
            {
                "candidate_id": r["candidate_id"],
                "type": r["memory_type"],
                "content_preview": (r["content"] or "")[:100],
                "created_at": r["created_at"],
                "sensitivity": r["sensitivity"],
            }
            for r in rows
        ]

    def approve_all_candidates(
        self,
        *,
        memory_type: str | None = None,
        dry_run: bool = True,
        ctx: dict[str, Any],
    ) -> tuple[list[str] | list[dict[str, Any]], str]:
        """Phase 9 M9.2: Batch approve candidates (with risk checks).

        Returns:
            - If dry_run=True: (list[dict], "") where dict has {candidate_id, memory_type, sensitivity, created_at, approval_id}
            - If dry_run=False: (list[str], "") where str is the candidate_id
            - On error: ([], error_msg)
        """
        if self._memory_store is None:
            return [], "memory namespace disabled"

        where_parts = ["status = 'pending'"]
        params: list[Any] = []

        if memory_type:
            where_parts.append("memory_type = ?")
            params.append(memory_type)

        where_clause = " AND ".join(where_parts)
        # Phase 9 mc-8 / ct-1: Use config.memory_control.batch_approve_max (accessible as config.memory.control.batch_approve_max)
        batch_limit = self.config.memory_control.batch_approve_max
        candidates = self.storage.fetchall(
            f"SELECT candidate_id, memory_type, sensitivity, source_type, created_at, approval_id FROM memory_candidates WHERE {where_clause} LIMIT ?",
            tuple(params) + (batch_limit,),
        )

        # Risk check: block if any high-risk candidate
        for cand in candidates:
            if cand["sensitivity"] >= 2:
                return [], f"Batch approval blocked: candidate {cand['candidate_id']} has sensitivity >= 2"
            if cand["memory_type"] in {"security_rule", "project_constraint", "architecture_decision"}:
                return [], f"Batch approval blocked: candidate {cand['candidate_id']} is high-risk type"
            if cand["source_type"] == "explicit":
                return [], f"Batch approval blocked: candidate {cand['candidate_id']} is explicit (requires manual review)"

        if dry_run:
            # Return full candidate details for enhanced preview
            return [
                {
                    "candidate_id": c["candidate_id"],
                    "memory_type": c["memory_type"],
                    "sensitivity": c["sensitivity"],
                    "created_at": c["created_at"],
                    "approval_id": c.get("approval_id"),
                }
                for c in candidates
            ], ""

        # Execute approval
        approved = []
        for cand in candidates:
            cid = cand["candidate_id"]
            item_id, error = self._memory_store.commit_candidate(cid)
            if item_id:  # Success - item_id returned
                approved.append(cid)

        return approved, ""

    def approve_batch(
        self,
        candidate_ids: list[str],
    ) -> tuple[list[str], list[str]]:
        """Phase 9 M9.2: programmatic batch approval helper.

        Returns ``(approved_ids, errors)`` parallel to ``candidate_ids``. Honors
        the same ``memory_control.batch_approve_max`` ceiling that the CLI uses.
        """
        if self._memory_store is None:
            return [], ["memory namespace disabled"] * len(candidate_ids)
        max_batch = getattr(self.config.memory_control, "batch_approve_max", 20) or 20
        if len(candidate_ids) > max_batch:
            return [], [f"batch size {len(candidate_ids)} exceeds max {max_batch}"]
        approved: list[str] = []
        errors: list[str] = []
        for cid in candidate_ids:
            item_id, err = self._memory_store.commit_candidate(cid)
            if item_id:
                approved.append(cid)
            else:
                errors.append(err or f"failed to approve {cid}")
        return approved, errors

    def reject_batch(self, candidate_ids: list[str]) -> list[str]:
        """Phase 9 M9.2: programmatic batch rejection helper. Returns list of
        rejected candidate ids."""
        if self._memory_store is None:
            return []
        rejected: list[str] = []
        for cid in candidate_ids:
            try:
                if self._memory_store.reject_candidate(cid):
                    rejected.append(cid)
            except Exception:
                continue
        return rejected

    def get_agent_summary(
        self,
        *,
        agent_id: str,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], str]:
        """Phase 9 M9.4: structured-only agent memory summary.

        Returns memory items whose ``source_type`` is one of the structured
        sources (workflow / explicit_remember / key_finding / agent_summary)
        — explicitly excludes ``session_compaction``.
        """
        if self._memory_store is None:
            return [], "memory namespace disabled"

        structured_sources = (
            "workflow_result",
            "explicit_remember",
            "explicit",
            "key_finding",
            "agent_summary",
            "task_state",
            "workflow",
        )
        placeholders = ",".join("?" for _ in structured_sources)
        rows = self.storage.fetchall(
            f"SELECT item_id, source_type, scope_type, scope_id, title, content_hash, "
            f"created_at, confidence, pinned FROM rag_items "
            f"WHERE namespace='memory' AND status='active' "
            f"AND owner_agent_id = ? AND source_type IN ({placeholders}) "
            f"ORDER BY created_at DESC LIMIT ?",
            (agent_id, *structured_sources, limit),
        )
        return [dict(r) for r in rows], ""

    def run_maintenance(
        self,
        *,
        agent_id: str | None = None,
        workspace_dir: str | None = None,
        scope: str = "agent",
        auto_apply: bool = False,
        ctx: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Phase 9 M9.6 alias of :meth:`run_memory_maintenance` with a simpler
        signature for direct programmatic / test invocation.

        Args:
            agent_id: agent scope identifier (when scope='agent').
            workspace_dir: workspace dir (when scope='workspace').
            scope: agent / workspace / all.
            auto_apply: if True, immediately apply suggestions (default False).
            ctx: optional pre-built ctx dict; overrides agent_id/workspace_dir.
        """
        if ctx is None:
            ctx = {
                "agent_id": agent_id,
                "workspace_dir": workspace_dir,
                "channel_name": "legacy",
            }
        result = self.run_memory_maintenance(ctx=ctx, scope=scope, persist=True)
        if auto_apply and result.get("suggestion_ids"):
            from mini_claw.rag.memory.maintenance import apply_suggestion
            applied: list[str] = []
            for sid in result["suggestion_ids"]:
                ok, _err = apply_suggestion(sid, self.storage, self)
                if ok:
                    applied.append(sid)
            result["applied_suggestions"] = applied
        return result

    def run_memory_maintenance(
        self,
        *,
        ctx: dict[str, Any],
        scope: str = "agent",
        persist: bool = True,
    ) -> dict[str, Any]:
        """Phase 9 M9.6: Run memory maintenance scan (suggestions only, no mutations).

        Returns dict with:
          - duplicates: list of {representative_id, duplicate_ids, similarity}
          - conflicts: list of {item_id_a, item_id_b, similarity}
          - stale: list of {item_id, age_days, access_count}
          - scanned_count: int
          - suggestion_ids: list of new suggestion_ids written to DB

        Args:
            ctx: agent context
            scope: agent/workspace/all
            persist: if True (default), write findings to memory_maintenance_suggestions
        """
        if self._memory_store is None:
            return {
                "duplicates": [],
                "conflicts": [],
                "stale": [],
                "scanned_count": 0,
                "suggestion_ids": [],
                "error": "memory namespace disabled",
            }

        from mini_claw.rag.memory.maintenance import MemoryMaintenance
        # Phase 9 M9.6: pass maintenance config including hybrid dedupe mode
        # Config drives the actual maintenance algorithm via these keys:
        # - dedupe_text_threshold: Jaccard threshold for text-only or hybrid text phase
        # - dedupe_embedding_threshold: cosine threshold for hybrid embedding phase
        # - mode: "auto" (hybrid if embedder available) | "text_only" | "hybrid"
        # - conflict_threshold: Jaccard threshold for conflict detection
        # - stale_age_days / stale_max_access: stale memory thresholds
        maintenance = MemoryMaintenance(
            self.storage,
            config={
                "dedupe_text_threshold": self.config.memory_maintenance.dedupe_text_threshold,
                "dedupe_embedding_threshold": self.config.memory_maintenance.dedupe_embedding_threshold,
                "mode": self.config.memory_maintenance.mode,
                "conflict_threshold": self.config.memory_maintenance.conflict_threshold,
                "stale_age_days": self.config.memory_maintenance.stale_age_days,
                "stale_max_access": self.config.memory_maintenance.stale_max_access,
                "embedder": self.embedder,  # Pass embedder for hybrid mode
            }
        )
        result = maintenance.run(ctx=ctx, scope=scope)

        # Phase 9 M9.6: Persist findings to memory_maintenance_suggestions table
        suggestion_ids: list[str] = []
        if persist:
            import time as _t
            import uuid as _uuid
            now = int(_t.time())

            for d in result.duplicates:
                # Each duplicate group → one suggestion per (representative, duplicate) pair
                for dup_id in d.duplicate_ids:
                    sid = f"sug-{_uuid.uuid4().hex[:12]}"
                    try:
                        self.storage.execute(
                            "INSERT INTO memory_maintenance_suggestions "
                            "(suggestion_id, suggestion_type, item_id_a, item_id_b, reason, confidence, status, created_at) "
                            "VALUES (?, 'dedupe', ?, ?, ?, ?, 'pending', ?)",
                            (sid, d.representative_id, dup_id, d.reason, d.similarity, now),
                        )
                        suggestion_ids.append(sid)
                    except Exception:
                        pass

            for c in result.conflicts:
                sid = f"sug-{_uuid.uuid4().hex[:12]}"
                try:
                    self.storage.execute(
                        "INSERT INTO memory_maintenance_suggestions "
                        "(suggestion_id, suggestion_type, item_id_a, item_id_b, reason, confidence, status, created_at) "
                        "VALUES (?, 'conflict', ?, ?, ?, ?, 'pending', ?)",
                        (sid, c.item_id_a, c.item_id_b, c.reason, c.similarity, now),
                    )
                    suggestion_ids.append(sid)
                except Exception:
                    pass

            for s in result.stale:
                sid = f"sug-{_uuid.uuid4().hex[:12]}"
                try:
                    self.storage.execute(
                        "INSERT INTO memory_maintenance_suggestions "
                        "(suggestion_id, suggestion_type, item_id_a, item_id_b, reason, confidence, status, created_at) "
                        "VALUES (?, 'stale', ?, NULL, ?, ?, 'pending', ?)",
                        (sid, s.item_id, s.reason, 0.0, now),
                    )
                    suggestion_ids.append(sid)
                except Exception:
                    pass

            try:
                self.storage._conn.commit()
            except Exception:
                pass

        return {
            "duplicates": [
                {
                    "representative_id": d.representative_id,
                    "duplicate_ids": d.duplicate_ids,
                    "similarity": d.similarity,
                    "reason": d.reason,
                }
                for d in result.duplicates
            ],
            "conflicts": [
                {
                    "item_id_a": c.item_id_a,
                    "item_id_b": c.item_id_b,
                    "similarity": c.similarity,
                    "reason": c.reason,
                }
                for c in result.conflicts
            ],
            "stale": [
                {
                    "item_id": s.item_id,
                    "age_days": s.age_days,
                    "access_count": s.access_count,
                    "last_accessed_at": s.last_accessed_at,
                    "reason": s.reason,
                }
                for s in result.stale
            ],
            "scanned_count": result.scanned_count,
            "suggestion_ids": suggestion_ids,
        }

    def search_memory(
        self,
        query: str,
        *,
        ctx: dict[str, Any],
        top_k: int | None = None,
        scope: str = "agent",
    ) -> tuple[list, str]:
        """Search the memory namespace.

        Reuses the FTS retriever (M2) but forces ``namespace='memory'`` and
        applies the memory-specific ``min_memory_confidence`` filter on
        the parent items (chunks inherit item confidence at runtime).

        Phase 9 M9.5: Added ``scope`` parameter with fail-closed semantics:
        - scope='agent'      : agent-scoped memories (owner_agent_id == ctx.agent_id)
        - scope='workspace'  : workspace-scoped memories (workspace_dir == ctx.workspace_dir)
        - scope='user'       : cross-agent user-scoped memories (user_id-based, future)
        - scope='all'        : combined agent + workspace + user (current behavior)

        Fail-closed: if scope requires a ctx field that is missing/None, return
        empty result + error rather than fall back to global search.
        """
        if (
            not self.config.enabled
            or not self.config.namespaces.memory_enabled
            or self._memory_store is None
        ):
            return [], "memory RAG is disabled"

        top_k = top_k or self.config.retrieval.memory_top_k

        # Phase 9 M9.5: Build scope filter with fail-closed checks
        # Phase 9 M9.5: route through MemoryScopeFilter (fail-closed channel/agent/ws).
        try:
            from mini_claw.rag.memory.scope_filter import build_scope_filter
            _filter = build_scope_filter(ctx, "memory", scope)
        except ValueError as exc:
            return [], f"fail-closed: {exc}"

        scope_filter: dict[str, Any] = {"namespace": "memory"}
        if _filter.channel_name:
            scope_filter["channel_name"] = _filter.channel_name

        if scope == "agent":
            scope_filter["owner_agent_id"] = _filter.agent_id
            scope_filter["scope_type"] = "agent"
        elif scope == "workspace":
            scope_filter["workspace_dir"] = _filter.workspace_dir
            scope_filter["scope_type"] = "workspace"
        elif scope == "user":
            scope_filter["owner_agent_id"] = _filter.agent_id
            scope_filter["scope_type"] = "user"
        elif scope == "all":
            scope_filter["owner_agent_id"] = _filter.agent_id
        else:
            return [], f"unknown scope: {scope}"

        results, error = self.retriever.search_context(
            query,
            ctx=ctx,
            namespace="memory",
            scope_filter=scope_filter,
            top_k=top_k,
            include_archived=False,
        )
        if error:
            return [], error
        # Filter by confidence threshold (RAG.md §7.8)
        min_conf = self.config.retrieval.min_memory_confidence
        filtered = []
        item_ids_accessed = []
        for r in results:
            item = self.store.get_item(r.item_id)
            if item is None:
                continue
            if item.confidence < min_conf and not item.pinned:
                continue
            filtered.append(r)
            item_ids_accessed.append(r.item_id)

        # Phase 9 M9.2.4: update last_accessed_at + access_count
        if item_ids_accessed:
            import time as _t
            import uuid as _uuid
            import json as _json
            now = int(_t.time())
            placeholders = ",".join("?" * len(item_ids_accessed))
            self.storage.execute(
                f"UPDATE rag_items SET last_accessed_at = ?, access_count = access_count + 1 "
                f"WHERE item_id IN ({placeholders})",
                (now, *item_ids_accessed),
            )

            # Phase 9 M9.6: write usage events (best-effort, swallow errors)
            ctx_blob = _json.dumps(
                {
                    "chat_id": ctx.get("chat_id"),
                    "agent_id": ctx.get("agent_id"),
                    "channel_name": ctx.get("channel_name"),
                    "retrieval_type": "search_memory",
                    "scope": scope,
                }
            )
            for item_id in item_ids_accessed:
                try:
                    self.storage.execute(
                        "INSERT INTO memory_usage_events "
                        "(event_id, item_id, accessed_at, context_json) "
                        "VALUES (?, ?, ?, ?)",
                        (f"mue-{_uuid.uuid4().hex[:12]}", item_id, now, ctx_blob),
                    )
                except Exception:
                    pass

        return filtered, ""

    async def consolidate_candidate(self, candidate_id: str) -> bool:
        """Optionally rewrite a pending candidate into a self-contained fact.

        Returns True if the candidate was updated. Provider is fetched lazily
        from the manager-bound provider hook (set by Gateway during init).
        """
        if self._memory_store is None:
            return False
        cand = self.store.get_memory_candidate(candidate_id)
        if cand is None or cand.status != "pending":
            return False
        provider = getattr(self, "_consolidator_provider", None)
        if provider is None:
            return False
        rewritten = await consolidate(cand, provider=provider)
        if rewritten is cand or rewritten.content == cand.content:
            return False
        # Persist new content (re-run validator inside commit at approval time)
        self.storage.execute(
            "UPDATE memory_candidates SET content = ?, updated_at = ? WHERE candidate_id = ?",
            (rewritten.content, int(__import__('time').time()), candidate_id),
        )
        return True

    def set_consolidator_provider(self, provider: Any) -> None:
        """Inject the LLM provider used by :meth:`consolidate_candidate`."""
        self._consolidator_provider = provider

    # Auto-source intake — these are called by SessionManager / TaskState /
    # WorkflowMerger. They're noops when memory_enabled=False so existing
    # call sites can fire unconditionally.

    def submit_session_compaction_candidates(
        self,
        messages: list[dict[str, Any]],
        *,
        chat_id: str,
        agent_id: str,
        session_id: str | None = None,
        channel: str | None = None,
        channel_name: str | None = None,
        workspace_dir: str | None = None,
    ) -> int:
        # Accept both ``channel`` and ``channel_name`` kwarg names.
        if channel is None and channel_name is not None:
            channel = channel_name
        if (
            not self.config.enabled
            or not self.config.namespaces.memory_enabled
            or self._memory_store is None
        ):
            return 0
        from mini_claw.rag.memory import extract_from_session_compaction
        cands = extract_from_session_compaction(
            messages,
            chat_id=chat_id,
            agent_id=agent_id,
            session_id=session_id,
            channel=channel,
            workspace_dir=workspace_dir,
        )
        if not cands:
            return 0
        results = self._memory_store.submit_candidates(cands, require_approval=True)
        return sum(1 for _, _, status in results if status == "submitted")

    def submit_task_state_candidates(
        self,
        task_state: Any,
        *,
        chat_id: str,
        agent_id: str,
        channel: str | None = None,
    ) -> int:
        if (
            not self.config.enabled
            or not self.config.namespaces.memory_enabled
            or self._memory_store is None
        ):
            return 0
        from mini_claw.rag.memory import extract_from_task_state
        cands = extract_from_task_state(
            task_state, chat_id=chat_id, agent_id=agent_id, channel=channel
        )
        if not cands:
            return 0
        results = self._memory_store.submit_candidates(cands, require_approval=True)
        return sum(1 for _, _, status in results if status == "submitted")

    def submit_agent_summary_candidates(
        self,
        summary_text: str,
        *,
        chat_id: str,
        agent_id: str,
        channel: str | None = None,
        workspace_dir: str | None = None,
    ) -> int:
        """Phase 9 M9.4: ingest agent self-summary into memory candidates.

        Gated by ``memory_control.auto_candidate_from_agent`` — caller is
        responsible for the gate check; this method only enforces the
        memory-namespace + RAG enable flags.
        """
        if (
            not self.config.enabled
            or not self.config.namespaces.memory_enabled
            or self._memory_store is None
        ):
            return 0
        from mini_claw.rag.memory import extract_from_agent_summary
        cands = extract_from_agent_summary(
            summary_text,
            agent_id=agent_id,
            chat_id=chat_id,
            channel_name=channel or "legacy",
            workspace_dir=workspace_dir,
        )
        if not cands:
            return 0
        results = self._memory_store.submit_candidates(cands, require_approval=True)
        return sum(1 for _, _, status in results if status == "submitted")

    def submit_workflow_candidates(
        self,
        merged_result: dict[str, Any],
        *,
        workflow_id: str,
        chat_id: str,
        agent_id: str,
        channel: str | None = None,
        workspace_dir: str | None = None,
        workflow_intent: str | None = None,
    ) -> int:
        if (
            not self.config.enabled
            or not self.config.namespaces.memory_enabled
            or self._memory_store is None
        ):
            return 0
        from mini_claw.rag.memory import extract_from_workflow_merger
        cands = extract_from_workflow_merger(
            merged_result,
            workflow_id=workflow_id,
            chat_id=chat_id,
            agent_id=agent_id,
            channel=channel,
            workspace_dir=workspace_dir,
            workflow_intent=workflow_intent,
        )
        if not cands:
            return 0
        results = self._memory_store.submit_candidates(cands, require_approval=True)
        return sum(1 for _, _, status in results if status == "submitted")
