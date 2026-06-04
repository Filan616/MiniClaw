"""Chat search indexer: mirrors messages to messages_fts for full-text search.

Phase 9 M9.1: messages → messages_fts mirror index with FTS5 → LIKE fallback.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mini_claw.storage.db import Database


def index_message_row(
    storage: "Database",
    message_id: int,
    *,
    session_id: str | None = None,
    agent_id: str,
    chat_id: str,
    channel_name: str,
    workspace_dir: str | None = None,
    role: str,
    content: str | None,
    created_at: int,
) -> bool:
    """Mirror a single message row into messages_fts.

    Returns True if indexed successfully, False if FTS5 unavailable (graceful degradation).
    Failures are silent — chat_search falls back to LIKE search on messages table.
    """
    if not content:
        return False

    try:
        storage.execute(
            "INSERT INTO messages_fts "
            "(message_id, session_id, agent_id, chat_id, channel_name, workspace_dir, "
            "role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message_id,
                session_id,
                agent_id,
                chat_id,
                channel_name,
                workspace_dir,
                role,
                content,
                created_at,
            ),
        )
        return True
    except sqlite3.OperationalError:
        # FTS5 table doesn't exist or is broken; retriever will use LIKE fallback
        return False
