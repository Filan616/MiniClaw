"""Chat search retriever: full-text search over messages with scope isolation.

Phase 9 M9.1: search messages by scope (session/agent/workspace/all_visible)
with fail-closed isolation (missing ctx fields → reject, not fallback to global).
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mini_claw.agent.context import AgentContext
    from mini_claw.storage.db import Database


def _sanitize_fts_query(query: str) -> str:
    """Escape FTS5 special characters to prevent syntax errors.

    Replaces: " with empty, - with space, other special chars with space.
    """
    return (
        query.replace('"', "")
        .replace("-", " ")
        .replace("(", " ")
        .replace(")", " ")
        .replace("*", " ")
        .strip()
    )


class ChatSearchRetriever:
    """Full-text search over messages with scope-based isolation."""

    def __init__(self, storage: "Database", config: dict[str, Any]) -> None:
        self.storage = storage
        self.config = config
        self._fts_available = self._probe_fts5()

    def _probe_fts5(self) -> bool:
        """Check if messages_fts table exists and is usable."""
        try:
            self.storage.execute("SELECT 1 FROM messages_fts LIMIT 1")
            return True
        except sqlite3.OperationalError:
            return False

    def search(
        self,
        query: str,
        *,
        scope: str,
        ctx: "AgentContext",
        top_k: int = 50,
    ) -> list[dict[str, Any]]:
        """Search messages with scope-based isolation.

        Args:
            query: Search query string
            scope: One of: current_session, current_agent, workspace, all_visible
            ctx: AgentContext with agent_id, session_id, channel_name, workspace_dir
            top_k: Maximum results to return

        Returns:
            List of message dicts with role, content, created_at, chat_id, agent_id

        Raises:
            ValueError: If scope requires a ctx field that is None (fail-closed)
        """
        # Phase 9 M9.1: fail-closed — missing scope identifiers reject, not fallback
        if ctx.channel_name is None:
            raise ValueError("ctx.channel_name is None; cannot isolate by channel")
        if ctx.agent_id is None:
            raise ValueError("ctx.agent_id is None; cannot search by agent")

        if scope in ("current_session", "session"):
            if ctx.session_id is None:
                raise ValueError("scope='current_session' requires ctx.session_id")
            return self._search_session(query, ctx, top_k)
        elif scope in ("current_agent", "agent"):
            return self._search_agent(query, ctx, top_k)
        elif scope == "workspace":
            if ctx.workspace_dir is None:
                raise ValueError("scope='workspace' requires ctx.workspace_dir")
            return self._search_workspace(query, ctx, top_k)
        elif scope in ("all_visible", "all"):
            if not self.config.get("allow_global", False):
                raise ValueError("scope='all_visible' disabled by config")
            return self._search_all_visible(query, ctx, top_k)
        else:
            raise ValueError(f"Unknown scope: {scope}")

    def _search_session(
        self, query: str, ctx: "AgentContext", top_k: int
    ) -> list[dict[str, Any]]:
        """Search within current session only."""
        if self._fts_available:
            return self._search_fts(
                query,
                where_clause="fts.session_id = ? AND m.channel_name = ?",
                params=(ctx.session_id, ctx.channel_name, top_k),
                top_k=top_k,
            )
        else:
            return self._search_like(
                query,
                where_clause="messages.chat_id = ? AND messages.agent_id = ? "
                "AND messages.channel_name = ?",
                params=(ctx.chat_id, ctx.agent_id, ctx.channel_name),
                top_k=top_k,
            )

    def _search_agent(
        self, query: str, ctx: "AgentContext", top_k: int
    ) -> list[dict[str, Any]]:
        """Search within current agent across all sessions."""
        if self._fts_available:
            return self._search_fts(
                query,
                where_clause="m.agent_id = ? AND m.channel_name = ?",
                params=(ctx.agent_id, ctx.channel_name, top_k),
                top_k=top_k,
            )
        else:
            return self._search_like(
                query,
                where_clause="messages.agent_id = ? "
                "AND messages.channel_name = ?",
                params=(ctx.agent_id, ctx.channel_name),
                top_k=top_k,
            )

    def _search_workspace(
        self, query: str, ctx: "AgentContext", top_k: int
    ) -> list[dict[str, Any]]:
        """Search within current workspace (cross chat_id, same workspace_dir).

        Phase 9 P0-2: When config.include_inferred=True, also include rows where
        workspace_dir was best-effort-inferred during migration (workspace_dir_inferred=1).
        """
        include_inferred = self.config.get("include_inferred", False)

        if self._fts_available:
            if include_inferred:
                where_clause = (
                    "(m.workspace_dir = ? OR (m.workspace_dir = ? AND m.workspace_dir_inferred = 1)) "
                    "AND m.channel_name = ?"
                )
                params = (str(ctx.workspace_dir), str(ctx.workspace_dir), ctx.channel_name, top_k)
            else:
                where_clause = "m.workspace_dir = ? AND m.channel_name = ?"
                params = (str(ctx.workspace_dir), ctx.channel_name, top_k)
            return self._search_fts(query, where_clause=where_clause, params=params, top_k=top_k)
        else:
            if include_inferred:
                where_clause = (
                    "(messages.workspace_dir = ? OR (messages.workspace_dir = ? AND messages.workspace_dir_inferred = 1)) "
                    "AND messages.channel_name = ?"
                )
                params = (str(ctx.workspace_dir), str(ctx.workspace_dir), ctx.channel_name)
            else:
                where_clause = (
                    "messages.workspace_dir = ? "
                    "AND messages.channel_name = ?"
                )
                params = (str(ctx.workspace_dir), ctx.channel_name)
            return self._search_like(query, where_clause=where_clause, params=params, top_k=top_k)

    def _search_all_visible(
        self, query: str, ctx: "AgentContext", top_k: int
    ) -> list[dict[str, Any]]:
        """Search all messages visible to user (channel-scoped only)."""
        if self._fts_available:
            return self._search_fts(
                query,
                where_clause="m.channel_name = ?",
                params=(ctx.channel_name, top_k),
                top_k=top_k,
            )
        else:
            return self._search_like(
                query,
                where_clause="messages.channel_name = ?",
                params=(ctx.channel_name,),
                top_k=top_k,
            )

    def _search_fts(
        self, query: str, where_clause: str, params: tuple, top_k: int
    ) -> list[dict[str, Any]]:
        """Search using FTS5 index."""
        sanitized = _sanitize_fts_query(query)
        if not sanitized:
            return []

        # FTS match → get message_ids → join back to messages for full row + scope filter
        rows = self.storage.fetchall(
            f"""
            SELECT m.id, m.role, m.content, m.created_at, m.chat_id, m.agent_id, m.channel_name
            FROM messages_fts fts
            JOIN messages m ON fts.message_id = m.id
            WHERE fts.content MATCH ? AND {where_clause}
            ORDER BY m.created_at DESC
            LIMIT ?
            """,
            (sanitized, *params),
        )
        return [dict(row) for row in rows]

    def _search_like(
        self, query: str, where_clause: str, params: tuple, top_k: int
    ) -> list[dict[str, Any]]:
        """Fallback LIKE search when FTS5 unavailable."""
        pattern = f"%{query}%"
        rows = self.storage.fetchall(
            f"""
            SELECT id, role, content, created_at, chat_id, agent_id, channel_name
            FROM messages
            WHERE content LIKE ? AND {where_clause}
              AND COALESCE(message_kind, 'normal') NOT IN ('prelude', 'react_update')
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (pattern, *params, top_k),
        )
        return [dict(row) for row in rows]
