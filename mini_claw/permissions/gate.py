"""Permission gate: pure decision function for tool-call authorization."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from mini_claw.permissions.levels import L3, L4
from mini_claw.permissions.policy import PermissionPolicy


@dataclass(frozen=True)
class Decision:
    """Result of a permission evaluation."""

    action: str  # "allow" | "deny" | "need_approval"
    reason: str = ""


@dataclass
class _SessionGrant:
    """Temporary per-session permission grant."""

    tool_name: str
    expires_at: datetime


@dataclass
class _PendingApproval:
    """A pending approval record awaiting human decision."""

    approval_id: str
    run_id: str
    chat_id: str
    agent_id: str
    tool_call: Dict[str, Any]
    created_at: datetime
    expires_at: datetime
    status: str = "pending"  # pending | approved | rejected | expired


class PermissionGate:
    """Pure decision gate — evaluates tool calls, never blocks."""

    def __init__(self, policy: PermissionPolicy, storage: Any = None) -> None:
        self._policy = policy
        self._storage = storage
        self._session_grants: list[_SessionGrant] = []
        self._pending: dict[str, _PendingApproval] = {}

    # ------------------------------------------------------------------
    # Core evaluation
    # ------------------------------------------------------------------

    def evaluate(self, tool: str, args: dict, ctx: dict) -> Decision:
        """Evaluate a tool call and return an immediate decision.

        This is a pure function — it never blocks or performs I/O.

        Args:
            tool: tool name (e.g. "run_shell", "write_file")
            args: tool call arguments
            ctx: context dict with keys like "level", "workspace_dir", "path"
        """
        level = ctx.get("level", self._policy.config.default_level)
        cmd = args.get("command", args.get("cmd", ""))

        # 1. Blacklist check (any level)
        if cmd and self._policy.is_blacklisted(cmd):
            return Decision(action="deny", reason=f"command matches blacklist: {cmd!r}")

        # 2. Path escape check
        path = args.get("path", args.get("file", ""))
        workspace_dir = ctx.get("workspace_dir")
        if path and workspace_dir:
            from pathlib import Path as _Path
            if not self._policy.path_in_workspace(path, _Path(workspace_dir)):
                return Decision(
                    action="deny",
                    reason=f"path escapes workspace: {path!r}",
                )

        # 3. L4 deny-by-default (unless template match)
        if level in self._policy.config.deny_by_default:
            if self._policy.matches_high_risk_template(tool, args):
                return Decision(action="allow", reason="matches allowed high-risk template")
            return Decision(action="deny", reason=f"level {level} is denied by default")

        # 4. L3 require confirmation (unless session grant)
        if level in self._policy.config.require_confirm:
            if self._has_session_grant(tool):
                return Decision(action="allow", reason="session grant active")
            return Decision(action="need_approval", reason=f"level {level} requires confirmation")

        # 5. Default allow
        return Decision(action="allow", reason="permitted by policy")

    # ------------------------------------------------------------------
    # Pending approval lifecycle
    # ------------------------------------------------------------------

    def create_pending(
        self,
        run_id: str,
        chat_id: str,
        agent_id: str,
        tool_call: dict,
        ttl: int = 300,
    ) -> str:
        """Create a pending approval record and return its ID.

        Args:
            run_id: current execution run ID
            chat_id: originating chat/conversation ID
            agent_id: agent that issued the tool call
            tool_call: dict describing the tool call (tool, args, etc.)
            ttl: time-to-live in seconds before auto-expiry
        """
        approval_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        record = _PendingApproval(
            approval_id=approval_id,
            run_id=run_id,
            chat_id=chat_id,
            agent_id=agent_id,
            tool_call=tool_call,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl),
        )
        self._pending[approval_id] = record
        return approval_id

    def resolve(self, approval_id: str, decision: str) -> Optional[dict]:
        """Resolve a pending approval.

        Args:
            approval_id: ID returned by create_pending
            decision: one of "approved", "rejected", "expired"

        Returns:
            The resolved record as a dict, or None if not found / already resolved.
        """
        record = self._pending.get(approval_id)
        if record is None or record.status != "pending":
            return None

        now = datetime.now(timezone.utc)
        if now >= record.expires_at:
            record.status = "expired"
        else:
            record.status = decision

        return {
            "approval_id": record.approval_id,
            "status": record.status,
            "tool_call": record.tool_call,
            "run_id": record.run_id,
            "chat_id": record.chat_id,
            "agent_id": record.agent_id,
        }

    # ------------------------------------------------------------------
    # Session grants
    # ------------------------------------------------------------------

    def grant_session(self, ctx: dict, tool_name: str, ttl: int = 600) -> None:
        """Add a temporary session grant for a tool.

        Args:
            ctx: context dict (reserved for future per-session scoping)
            tool_name: the tool to grant
            ttl: grant duration in seconds (default 10 minutes)
        """
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)
        self._session_grants.append(
            _SessionGrant(tool_name=tool_name, expires_at=expires_at)
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _has_session_grant(self, tool_name: str) -> bool:
        """Check if an active (non-expired) session grant exists for *tool_name*."""
        now = datetime.now(timezone.utc)
        self._session_grants = [
            g for g in self._session_grants if g.expires_at > now
        ]
        return any(g.tool_name == tool_name for g in self._session_grants)
