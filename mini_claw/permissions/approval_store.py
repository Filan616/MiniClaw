"""Approval persistence layer — owns pending_approvals and session_grants tables."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Optional


class ApprovalStore:
    """Persistence layer for approval lifecycle and session grants.

    Architecture: Separates storage concerns from decision logic (PermissionGate).
    PermissionGate calls ApprovalStore for create/resolve/grant operations.
    This keeps PermissionGate as a pure decision function.
    """

    def __init__(self, storage: Any) -> None:
        self._storage = storage
        # In-memory cache for hot-path reads (session grants)
        self._grants_cache: dict[tuple[str, str, str], int] = {}  # (chat_id, agent_id, tool) -> expires_at

    # ------------------------------------------------------------------
    # Pending approvals
    # ------------------------------------------------------------------

    def create_pending(
        self,
        approval_id: str,
        run_id: str,
        chat_id: str,
        agent_id: str,
        tool_name: str,
        tool_args: dict,
        expires_at: int,
    ) -> None:
        """Create a pending approval record.

        Args:
            approval_id: unique approval ID
            run_id: current agent run ID
            chat_id: originating chat/conversation ID
            agent_id: agent that issued the tool call
            tool_name: name of the tool requiring approval
            tool_args: tool arguments as dict
            expires_at: unix timestamp when this approval expires
        """
        now = int(time.time())
        self._storage.execute(
            "INSERT INTO pending_approvals "
            "(id, run_id, chat_id, agent_id, tool_name, tool_args, status, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (approval_id, run_id, chat_id, agent_id, tool_name, json.dumps(tool_args), now, expires_at),
        )

    def resolve_pending(self, approval_id: str, decision: str) -> Optional[dict[str, Any]]:
        """Resolve a pending approval.

        Args:
            approval_id: ID of the approval to resolve
            decision: one of "approved", "rejected", "expired"

        Returns:
            The resolved record as dict, or None if not found / already resolved.
        """
        record = self._storage.fetchone(
            "SELECT id, run_id, chat_id, agent_id, tool_name, tool_args, status, expires_at "
            "FROM pending_approvals WHERE id = ?",
            (approval_id,),
        )
        if record is None or record["status"] != "pending":
            return None

        now = int(time.time())
        if now >= record["expires_at"]:
            final_status = "expired"
        else:
            final_status = decision

        self._storage.execute(
            "UPDATE pending_approvals SET status = ? WHERE id = ?",
            (final_status, approval_id),
        )

        return {
            "approval_id": record["id"],
            "status": final_status,
            "tool_call": {
                "tool": record["tool_name"],
                "args": json.loads(record["tool_args"]),
            },
            "run_id": record["run_id"],
            "chat_id": record["chat_id"],
            "agent_id": record["agent_id"],
        }

    def get_pending(self, approval_id: str) -> Optional[dict[str, Any]]:
        """Get a pending approval record by ID.

        Returns:
            The record as dict, or None if not found.
        """
        record = self._storage.fetchone(
            "SELECT id, run_id, chat_id, agent_id, tool_name, tool_args, status, created_at, expires_at "
            "FROM pending_approvals WHERE id = ?",
            (approval_id,),
        )
        if record is None:
            return None

        return {
            "approval_id": record["id"],
            "run_id": record["run_id"],
            "chat_id": record["chat_id"],
            "agent_id": record["agent_id"],
            "tool_name": record["tool_name"],
            "tool_args": json.loads(record["tool_args"]),
            "status": record["status"],
            "created_at": record["created_at"],
            "expires_at": record["expires_at"],
        }

    def expire_pending(self, older_than_seconds: int = 86400) -> int:
        """Expire old pending approvals.

        Args:
            older_than_seconds: expire approvals created more than this many seconds ago (default 24h)

        Returns:
            Number of records expired.
        """
        now = int(time.time())
        cutoff = now - older_than_seconds
        cursor = self._storage.execute(
            "UPDATE pending_approvals SET status = 'expired' "
            "WHERE status = 'pending' AND (expires_at < ? OR created_at < ?)",
            (now, cutoff),
        )
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Session grants
    # ------------------------------------------------------------------

    def grant_session(
        self,
        chat_id: str,
        agent_id: str,
        tool_name: str,
        expires_at: Optional[int] = None,
    ) -> None:
        """Grant a tool permission for the current session.

        Args:
            chat_id: chat/conversation ID
            agent_id: agent ID
            tool_name: tool to grant
            expires_at: unix timestamp when grant expires (None = no expiry)
        """
        self._storage.execute(
            "INSERT OR REPLACE INTO session_grants (chat_id, agent_id, tool_name, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, agent_id, tool_name, expires_at),
        )
        # Update cache
        cache_key = (chat_id, agent_id, tool_name)
        self._grants_cache[cache_key] = expires_at or 0

    def has_session_grant(self, chat_id: str, agent_id: str, tool_name: str) -> bool:
        """Check if an active session grant exists for a tool.

        Args:
            chat_id: chat/conversation ID
            agent_id: agent ID
            tool_name: tool to check

        Returns:
            True if grant exists and has not expired.
        """
        cache_key = (chat_id, agent_id, tool_name)

        # Check cache first
        if cache_key in self._grants_cache:
            expires_at = self._grants_cache[cache_key]
            if expires_at == 0 or expires_at > int(time.time()):
                return True
            else:
                # Expired, remove from cache
                del self._grants_cache[cache_key]
                return False

        # Cache miss, query database
        record = self._storage.fetchone(
            "SELECT expires_at FROM session_grants "
            "WHERE chat_id = ? AND agent_id = ? AND tool_name = ?",
            (chat_id, agent_id, tool_name),
        )
        if record is None:
            return False

        expires_at = record["expires_at"]
        # Populate cache
        self._grants_cache[cache_key] = expires_at or 0

        # Check expiry
        if expires_at is None or expires_at > int(time.time()):
            return True
        else:
            return False

    def cleanup_expired_grants(self) -> int:
        """Remove expired session grants from database.

        Returns:
            Number of grants removed.
        """
        now = int(time.time())
        cursor = self._storage.execute(
            "DELETE FROM session_grants WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        )
        # Clear cache to force refresh
        self._grants_cache.clear()
        return cursor.rowcount
