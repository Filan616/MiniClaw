"""Session management for the gateway layer."""

from __future__ import annotations

import time
from typing import Any

from mini_claw.storage.db import Database


# Configuration constants
MAX_HISTORY_MESSAGES = 50  # Keep last N messages
SYSTEM_SUMMARY_THRESHOLD = 40  # Trigger compression when exceeding this


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
            "sandbox_mode_override": None,
        }

    def set_sandbox_mode(self, chat_id: str, agent_id: str, mode: str) -> None:
        """Set sandbox_mode_override for a session ("safe", "bypass", or None)."""
        self._storage.execute(
            "UPDATE sessions SET sandbox_mode_override = ?, updated_at = ? "
            "WHERE chat_id = ? AND agent_id = ?",
            (mode, int(time.time()), chat_id, agent_id),
        )

    def get_sandbox_mode(self, chat_id: str, agent_id: str) -> str | None:
        """Get sandbox_mode_override for a session, or None if not set."""
        row = self._storage.fetchone(
            "SELECT sandbox_mode_override FROM sessions WHERE chat_id = ? AND agent_id = ?",
            (chat_id, agent_id),
        )
        return row["sandbox_mode_override"] if row else None

    def get_history(
        self, chat_id: str, agent_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Get recent messages for context window construction.

        Returns a list of message dicts with role and content keys,
        ordered chronologically (oldest first).

        If the history exceeds SYSTEM_SUMMARY_THRESHOLD, applies compression:
        - Keeps the first message (usually system prompt)
        - Summarizes middle messages
        - Keeps the last N messages intact
        """
        # Get total count
        count_row = self._storage.fetchone(
            "SELECT COUNT(*) as cnt FROM messages "
            "WHERE chat_id = ? AND agent_id = ?",
            (chat_id, agent_id),
        )
        total_count = count_row["cnt"] if count_row else 0

        # If under threshold, return all messages
        if total_count <= SYSTEM_SUMMARY_THRESHOLD:
            rows = self._storage.fetchall(
                "SELECT role, content, tool_calls, tool_call_id "
                "FROM messages "
                "WHERE chat_id = ? AND agent_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (chat_id, agent_id, limit),
            )
            rows.reverse()
            return self._rows_to_messages(rows)

        # Compression: keep first + last N messages
        keep_recent = 20
        keep_first = 1

        first_msgs = self._storage.fetchall(
            "SELECT role, content, tool_calls, tool_call_id "
            "FROM messages "
            "WHERE chat_id = ? AND agent_id = ? "
            "ORDER BY created_at ASC LIMIT ?",
            (chat_id, agent_id, keep_first),
        )

        last_msgs = self._storage.fetchall(
            "SELECT role, content, tool_calls, tool_call_id "
            "FROM messages "
            "WHERE chat_id = ? AND agent_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (chat_id, agent_id, keep_recent),
        )
        last_msgs.reverse()

        # Build compressed history
        messages = self._rows_to_messages(first_msgs)

        # Add compression notice
        compressed_count = total_count - keep_first - len(last_msgs)
        if compressed_count > 0:
            messages.append({
                "role": "system",
                "content": f"[Earlier {compressed_count} messages omitted for context length]"
            })

        messages.extend(self._rows_to_messages(last_msgs))
        return messages

    def _rows_to_messages(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert database rows to message dicts."""
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
