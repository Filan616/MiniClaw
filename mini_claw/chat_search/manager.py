"""Chat search manager: facade for chat_search retriever + rebuild commands.

Phase 9 M9.1: entry point for /chat search, /chat reindex, /chat status.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mini_claw.agent.context import AgentContext
    from mini_claw.storage.db import Database

from mini_claw.chat_search.retriever import ChatSearchRetriever


class ChatSearchManager:
    """Facade for chat search operations."""

    def __init__(self, storage: "Database", config: dict[str, Any]) -> None:
        self.storage = storage
        self.config = config
        self.retriever = ChatSearchRetriever(storage, config)

    def search(
        self,
        query: str,
        *,
        scope: str,
        ctx: "AgentContext",
        top_k: int | None = None,
    ) -> list[dict[str, Any]]:
        """Search messages with scope isolation.

        Returns list of message dicts. Raises ValueError if scope requirements not met.
        """
        k = top_k or self.config.get("fts_max_results", 50)
        return self.retriever.search(query, scope=scope, ctx=ctx, top_k=k)

    def rebuild_index(self, *, scope: str | None = None) -> dict[str, Any]:
        """Rebuild messages_fts from messages table.

        Returns: {total: int, indexed: int, skipped: int, duration_ms: int}
        """
        import time
        from mini_claw.chat_search.indexer import index_message_row
        from mini_claw.gateway.session import derive_session_id

        start = time.time()
        self.storage.execute("DELETE FROM messages_fts")

        rows = self.storage.fetchall(
            "SELECT id, chat_id, agent_id, channel_name, workspace_dir, role, content, created_at "
            "FROM messages WHERE content IS NOT NULL"
        )
        total = len(rows)
        indexed = 0
        skipped = 0

        for row in rows:
            session_id = derive_session_id(
                row["channel_name"] or "legacy",
                row["chat_id"],
                row["agent_id"],
            )
            success = index_message_row(
                self.storage,
                row["id"],
                session_id=session_id,
                agent_id=row["agent_id"],
                chat_id=row["chat_id"],
                channel_name=row["channel_name"] or "legacy",
                workspace_dir=row["workspace_dir"],
                role=row["role"],
                content=row["content"],
                created_at=row["created_at"],
            )
            if success:
                indexed += 1
            else:
                skipped += 1

        duration_ms = int((time.time() - start) * 1000)
        return {
            "total": total,
            "indexed": indexed,
            "skipped": skipped,
            "duration_ms": duration_ms,
        }

    def get_status(self) -> dict[str, Any]:
        """Return FTS availability + last rebuild info.

        Returns: {fts_available: bool, total_messages: int, fts_count: int, last_rebuild_time: int | None}
        """
        fts_available = self.retriever._fts_available

        total_row = self.storage.fetchone(
            "SELECT COUNT(*) as cnt FROM messages WHERE content IS NOT NULL"
        )
        total = int(total_row["cnt"]) if total_row else 0

        fts_count = 0
        if fts_available:
            fts_row = self.storage.fetchone("SELECT COUNT(*) as cnt FROM messages_fts")
            fts_count = int(fts_row["cnt"]) if fts_row else 0

        # Query last rebuild time from security_audit
        last_rebuild_time = None
        try:
            rebuild_row = self.storage.fetchone(
                "SELECT created_at FROM security_audit "
                "WHERE event_type = 'chat_search_rebuild_completed' "
                "ORDER BY created_at DESC LIMIT 1"
            )
            if rebuild_row and rebuild_row["created_at"]:
                last_rebuild_time = int(rebuild_row["created_at"])
        except Exception:
            # Table may not exist in older schemas, or query may fail
            pass

        return {
            "fts_available": fts_available,
            "total_messages": total,
            "fts_count": fts_count,
            "last_rebuild_time": last_rebuild_time,
        }
