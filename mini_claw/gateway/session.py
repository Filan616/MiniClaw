"""Session management for the gateway layer."""

from __future__ import annotations

import time
from typing import Any

from mini_claw.storage.db import Database


class SessionManager:
    """Manages chat sessions and message history."""

    def __init__(self, storage: Database) -> None:
        self._storage = storage

    def get_or_create(self, chat_id: str, agent_id: str) -> dict[str, Any]:
        """Get an existing session or create a new one.

        Returns a dict with session metadata.
        """
        row = self._storage.fetchone(
            "SELECT * FROM sessions WHERE chat_id = ? AND agent_id = ?",
            (chat_id, agent_id),
        )
        if row is not None:
            # Update last activity
            now = int(time.time())
            self._storage.execute(
                "UPDATE sessions SET updated_at = ? WHERE chat_id = ? AND agent_id = ?",
                (now, chat_id, agent_id),
            )
            row["updated_at"] = now
            return row

        now = int(time.time())
        self._storage.execute(
            "INSERT INTO sessions (chat_id, agent_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, agent_id, now, now),
        )
        return {
            "chat_id": chat_id,
            "agent_id": agent_id,
            "created_at": now,
            "updated_at": now,
        }

    def get_history(
        self, chat_id: str, agent_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Get recent messages for context window construction.

        Returns a list of message dicts with role and content keys,
        ordered chronologically (oldest first).
        """
        rows = self._storage.fetchall(
            "SELECT role, content, tool_calls, tool_call_id "
            "FROM messages "
            "WHERE chat_id = ? AND agent_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (chat_id, agent_id, limit),
        )
        # Reverse to chronological order
        rows.reverse()

        messages: list[dict[str, Any]] = []
        for row in rows:
            msg: dict[str, Any] = {"role": row["role"]}
            if row["content"]:
                msg["content"] = row["content"]
            if row["tool_calls"]:
                msg["tool_calls"] = row["tool_calls"]
            if row["tool_call_id"]:
                msg["tool_call_id"] = row["tool_call_id"]
            messages.append(msg)
        return messages

    def store_message(
        self,
        chat_id: str,
        agent_id: str,
        role: str,
        content: str | None,
        run_id: str | None = None,
    ) -> None:
        """Persist a message to the history store."""
        now = int(time.time())
        self._storage.execute(
            "INSERT INTO messages (chat_id, agent_id, run_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, agent_id, run_id, role, content, now),
        )
