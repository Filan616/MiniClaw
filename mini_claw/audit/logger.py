"""Security audit logger for MiniClaw.

This module provides a centralized way to log security-related events
(blacklist hits, sensitive file access attempts, chain attacks, etc.) to the database.
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from mini_claw.storage.db import Database


class SecurityAuditLogger:
    """Logs security events to the security_audit table with debug IDs."""

    def __init__(self, storage: Database) -> None:
        self._storage = storage

    def log_security_event(
        self,
        event_type: str,
        details: dict[str, Any],
        chat_id: str | None = None,
        agent_id: str | None = None,
    ) -> str:
        """Record a security event to the security_audit table.

        Args:
            event_type: Type of event (e.g., "blacklist_hit", "sensitive_path")
            details: Event-specific details as a dict
            chat_id: Optional chat/session ID
            agent_id: Optional agent ID

        Returns:
            debug_id: A unique debug ID for this event (format: sec_YYYYMMDD_XXXX)
        """
        debug_id = self._generate_debug_id()
        self._storage.execute(
            "INSERT INTO security_audit "
            "(debug_id, event_type, details, chat_id, agent_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (debug_id, event_type, json.dumps(details), chat_id, agent_id, int(time.time())),
        )
        return debug_id

    def _generate_debug_id(self) -> str:
        """Generate a unique debug ID with timestamp and random suffix."""
        timestamp = datetime.now().strftime("%Y%m%d")
        suffix = secrets.token_hex(4)
        return f"sec_{timestamp}_{suffix}"
