"""Security audit logger for MiniClaw.

This module provides a centralized way to log security-related events
(blacklist hits, sensitive file access attempts, etc.) to the database.

Architecture:
- PermissionGate returns Decision objects with `audit_event` field
- Gateway/tools receive these and call SecurityAuditLogger.log_security_event()
- This keeps PermissionGate independent of storage layer
"""

from __future__ import annotations

from mini_claw.audit.logger import SecurityAuditLogger

__all__ = ["SecurityAuditLogger"]
