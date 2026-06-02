"""Permission gate: pure decision function for tool-call authorization."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from mini_claw.permissions.approval_store import ApprovalStore
from mini_claw.permissions.levels import L3, L4
from mini_claw.permissions.policy import PermissionPolicy


@dataclass(frozen=True)
class Decision:
    """Result of a permission evaluation.

    Architecture: PermissionGate returns Decision objects with audit_event field;
    Gateway/tools receive these and call SecurityAuditLogger.log_security_event()
    to keep PermissionGate independent of storage layer.
    """

    action: str  # "allow" | "deny" | "need_approval"
    reason: str = ""  # Message shown to LLM (may contain {debug_id} placeholder)
    internal_reason: str = ""  # Detailed reason for logs (not shown to LLM)
    audit_event: Optional[Dict[str, Any]] = None  # {"event_type": ..., ...}, written by Gateway


class PermissionGate:
    """Pure decision gate — evaluates tool calls, never blocks."""

    def __init__(
        self,
        policy: PermissionPolicy,
        approval_store: ApprovalStore,
    ) -> None:
        self._policy = policy
        self._approval_store = approval_store

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
        sandbox_mode = ctx.get("sandbox_mode", "safe")

        # 1. Blacklist check (always-on safety net, even in bypass mode).
        if cmd:
            matched_pattern = self._policy.first_blacklist_match(cmd)
            if matched_pattern:
                return Decision(
                    action="deny",
                    reason="command blocked by security policy. debug_id={debug_id}",
                    internal_reason=f"matched blacklist pattern: {matched_pattern!r}",
                    audit_event={
                        "event_type": "blacklist_hit",
                        "cmd": cmd,
                        "matched_pattern": matched_pattern,
                        "tool": tool,
                    },
                )

        # In bypass mode, skip the sensitive-file and workspace-escape checks.
        # The user has explicitly opted to give the agent full filesystem access.
        if sandbox_mode != "bypass":
            # 2. Sensitive-file check (any level, before workspace check so the
            # error reason is more specific than "path escapes workspace").
            candidate_paths = [p for p in (args.get("path"), args.get("file")) if p]
            for cp in candidate_paths:
                if self._policy.is_sensitive_path(cp):
                    if self._policy.is_sensitive_path_allowlisted(cp):
                        continue
                    return Decision(
                        action="deny",
                        reason="access denied. debug_id={debug_id}",
                        internal_reason=f"sensitive path: {cp!r}",
                        audit_event={
                            "event_type": "sensitive_path",
                            "path": cp,
                            "tool": tool,
                        },
                    )

            # 3. Path escape check (semi-obfuscated, no audit)
            path = args.get("path", args.get("file", ""))
            workspace_dir = ctx.get("workspace_dir")
            if path and workspace_dir:
                from pathlib import Path as _Path
                if not self._policy.path_in_workspace(path, _Path(workspace_dir)):
                    return Decision(
                        action="deny",
                        reason="path outside workspace",
                        internal_reason=f"path escapes workspace: {path!r}",
                    )

        # 4. L4 deny-by-default (unless template match)
        if level in self._policy.config.deny_by_default:
            if self._policy.matches_high_risk_template(tool, args):
                return Decision(action="allow", reason="matches allowed high-risk template")
            return Decision(action="deny", reason=f"level {level} is denied by default")

        # 5. L3 require confirmation (unless session grant)
        if level in self._policy.config.require_confirm:
            chat_id = ctx.get("chat_id", "")
            agent_id = ctx.get("agent_id", "")
            if self._has_session_grant(chat_id, agent_id, tool):
                return Decision(action="allow", reason="session grant active")
            return Decision(action="need_approval", reason=f"level {level} requires confirmation")

        # 6. Default allow
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
        expires_at = int((now + timedelta(seconds=ttl)).timestamp())

        self._approval_store.create_pending(
            approval_id=approval_id,
            run_id=run_id,
            chat_id=chat_id,
            agent_id=agent_id,
            tool_name=tool_call.get("tool", ""),
            tool_args=tool_call.get("args", {}),
            expires_at=expires_at,
        )
        return approval_id

    def resolve(self, approval_id: str, decision: str) -> Optional[dict]:
        """Resolve a pending approval.

        Args:
            approval_id: ID returned by create_pending
            decision: one of "approved", "rejected", "expired"

        Returns:
            The resolved record as a dict, or None if not found / already resolved.
        """
        return self._approval_store.resolve_pending(approval_id, decision)

    # ------------------------------------------------------------------
    # Session grants
    # ------------------------------------------------------------------

    def grant_session(self, ctx: dict, tool_name: str, ttl: int = 600) -> None:
        """Add a temporary session grant for a tool.

        Args:
            ctx: context dict (must contain chat_id and agent_id)
            tool_name: the tool to grant
            ttl: grant duration in seconds (default 10 minutes, 0 = no expiry)
        """
        chat_id = ctx.get("chat_id", "")
        agent_id = ctx.get("agent_id", "")
        expires_at = int(datetime.now(timezone.utc).timestamp()) + ttl if ttl > 0 else None
        self._approval_store.grant_session(chat_id, agent_id, tool_name, expires_at)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _has_session_grant(self, chat_id: str, agent_id: str, tool_name: str) -> bool:
        """Check if an active (non-expired) session grant exists for *tool_name*."""
        return self._approval_store.has_session_grant(chat_id, agent_id, tool_name)
