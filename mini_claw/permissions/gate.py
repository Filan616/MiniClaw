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

        # Phase 8 M2: RAG tool explicit dispatch (M2 plan: user feedback 5)
        # Every RAG tool gets a dedicated branch; do NOT rely on generic fallback.
        RAG_TOOLS = {
            "index_context", "search_context", "list_contexts", "inspect_context",
            "clear_context", "archive_context", "delete_context", "read_sensitive_context",
            "reindex_context", "diff_context", "reembed_context", "rebind_context",
            "memory_remember", "memory_search", "memory_list", "memory_inspect",
            "memory_pin", "memory_unpin", "memory_delete", "memory_compact_to_rag",
        }
        if tool in RAG_TOOLS:
            return self._evaluate_rag_tool(tool, args, ctx, level, sandbox_mode)

        # Phase 9 M9.1: Chat search tool explicit dispatch (cs-5)
        # Sensitive query detection moved from ChainDetector to PermissionGate
        if tool == "search_chat":
            return self._evaluate_chat_tool(tool, args, ctx, level, sandbox_mode)

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
        approval_type: str = "tool",
        channel_name: str = "legacy",
    ) -> str:
        """Create a pending approval record and return its ID.

        Args:
            run_id: current execution run ID
            chat_id: originating chat/conversation ID
            agent_id: agent that issued the tool call
            tool_call: dict describing the tool call (tool, args, etc.)
            ttl: time-to-live in seconds before auto-expiry
            approval_type: classification used by callers (tool / workflow_plan /
                memory_export_full / etc.)
            channel_name: Phase 9 P0.2 — channel for multi-channel isolation.
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
            approval_type=approval_type,
            channel_name=channel_name or "legacy",
        )
        return approval_id

    def resolve(
        self,
        approval_id: str,
        decision: str,
        channel_name: str | None = None,
    ) -> Optional[dict]:
        """Resolve a pending approval.

        Args:
            approval_id: ID returned by create_pending
            decision: one of "approved", "rejected", "expired"
            channel_name: Phase 9 P0.2 — if provided, verifies that the
                approval belongs to the same channel before resolving.

        Returns:
            The resolved record as a dict, or None if not found / already resolved.
        """
        return self._approval_store.resolve_pending(
            approval_id, decision, channel_name=channel_name
        )

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

    def _evaluate_rag_tool(
        self, tool: str, args: dict, ctx: dict, level: str, sandbox_mode: str
    ) -> Decision:
        """Phase 8 M2: RAG tool explicit evaluation branch.

        Every RAG tool gets a dedicated check; do NOT rely on generic fallback.
        M2 plan (user feedback 5): explicit rules for each tool.
        """
        # index_context: L2, workspace + non-sensitive + non-bypass + size + binary
        if tool == "index_context":
            path = args.get("path", "")
            # Bypass mode check
            if sandbox_mode == "bypass":
                return Decision(
                    action="deny",
                    reason="index_context not allowed in bypass mode",
                    internal_reason="index_context + bypass = deny per M2 plan",
                )
            # Sensitive path check (higher severity than generic path check)
            if self._policy.is_sensitive_path(path):
                return Decision(
                    action="deny",
                    reason="cannot index sensitive path",
                    internal_reason=f"index_context denied: sensitive path {path!r}",
                    audit_event={
                        "event_type": "rag_index_sensitive_attempt",
                        "path": path,
                        "tool": tool,
                    },
                )
            # L2 require confirmation (unless session grant)
            if level in self._policy.config.require_confirm or level == "L2":
                chat_id = ctx.get("chat_id", "")
                agent_id = ctx.get("agent_id", "")
                if self._has_session_grant(chat_id, agent_id, tool):
                    return Decision(action="allow", reason="session grant active")
                return Decision(action="need_approval", reason=f"index_context requires L2 confirmation")
            return Decision(action="allow", reason="index_context permitted")

        # search_context: L1 (low risk, read-only)
        # CI-4: Add audit logging with query hash (never raw query) to limit PII exposure
        if tool == "search_context":
            import hashlib
            from mini_claw.permissions.policy import looks_like_exfil_query, get_exfil_query_keywords

            query = args.get("query", "")
            # Hash query to avoid logging PII
            query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16] if query else ""
            matched_keywords = get_exfil_query_keywords(query)
            keyword_class = "sensitive" if matched_keywords else "normal"

            # Generate audit event for sensitive queries (exfil detection)
            if query and looks_like_exfil_query(query):
                return Decision(
                    action="allow",
                    reason="search_context permitted (sensitive query logged for audit)",
                    audit_event={
                        "event_type": "rag_search_sensitive_query",
                        "tool": tool,
                        "query_hash": query_hash,
                        "query_length": len(query),
                        "keyword_class": keyword_class,
                        "matched_keywords": matched_keywords[:5],  # Limit to first 5 for audit
                    },
                )

            return Decision(action="allow", reason="search_context permitted (L1)")

        # list_contexts, inspect_context, diff_context: L1
        if tool in {"list_contexts", "inspect_context", "diff_context"}:
            return Decision(action="allow", reason=f"{tool} permitted (L1)")

        # clear_context, archive_context, reindex/reembed/rebind: L2
        if tool in {"clear_context", "archive_context", "reindex_context", "reembed_context", "rebind_context"}:
            if level in self._policy.config.require_confirm or level in {"L2", "L3"}:
                chat_id = ctx.get("chat_id", "")
                agent_id = ctx.get("agent_id", "")
                if self._has_session_grant(chat_id, agent_id, tool):
                    return Decision(action="allow", reason="session grant active")
                return Decision(action="need_approval", reason=f"{tool} requires confirmation")
            return Decision(action="allow", reason=f"{tool} permitted")

        # delete_context, read_sensitive_context: L3 (always require approval)
        if tool in {"delete_context", "read_sensitive_context"}:
            chat_id = ctx.get("chat_id", "")
            agent_id = ctx.get("agent_id", "")
            if self._has_session_grant(chat_id, agent_id, tool):
                return Decision(action="allow", reason="session grant active")
            return Decision(action="need_approval", reason=f"{tool} requires L3 approval")

        # memory_*: all L3 (M5, but defined here for completeness)
        if tool in {
            "memory_remember", "memory_delete", "memory_compact_to_rag",
            "memory_search", "memory_list", "memory_inspect",
            "memory_pin", "memory_unpin",
        }:
            # memory_search/list/inspect: L1
            if tool in {"memory_search", "memory_list", "memory_inspect"}:
                return Decision(action="allow", reason=f"{tool} permitted (L1)")
            # memory_pin/unpin: L2
            if tool in {"memory_pin", "memory_unpin"}:
                if level in self._policy.config.require_confirm or level in {"L2", "L3"}:
                    chat_id = ctx.get("chat_id", "")
                    agent_id = ctx.get("agent_id", "")
                    if self._has_session_grant(chat_id, agent_id, tool):
                        return Decision(action="allow", reason="session grant active")
                    return Decision(action="need_approval", reason=f"{tool} requires confirmation")
                return Decision(action="allow", reason=f"{tool} permitted")
            # memory_remember/delete/compact_to_rag: L3 (always approval)
            chat_id = ctx.get("chat_id", "")
            agent_id = ctx.get("agent_id", "")
            if self._has_session_grant(chat_id, agent_id, tool):
                return Decision(action="allow", reason="session grant active")
            return Decision(action="need_approval", reason=f"{tool} requires L3 approval")

        # Unknown RAG tool (should not happen if registry is consistent)
        return Decision(action="deny", reason=f"unknown RAG tool: {tool}")

    def _evaluate_chat_tool(
        self, tool: str, args: dict, ctx: dict, level: str, sandbox_mode: str
    ) -> Decision:
        """Phase 9 M9.1 (cs-5): Chat search tool evaluation branch.

        Moved from ChainDetector to PermissionGate for consistent architecture.
        Handles two risk scenarios:
        1. Bulk export detection: scope=all_visible + top_k>50
        2. Sensitive query detection: query contains exfil keywords

        Both scenarios generate audit events. The first is a warning (allow with audit),
        the second is also a warning (logged for audit review by ChainDetector link E).

        Audit contract: all events match Phase 9 spec with query_hash (never raw query),
        query_length, scope, and tool fields.
        """
        import hashlib
        from mini_claw.permissions.policy import looks_like_exfil_query, EXFIL_QUERY_KEYWORDS

        query = args.get("query", "")
        scope = args.get("scope", "current_session")
        top_k = int(args.get("top_k", 10))

        # Hash query once for all audit events (never log raw query)
        query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16] if query else ""

        # Determine keyword_class based on query content
        query_lower = query.lower() if query else ""
        matched_keywords = [kw for kw in EXFIL_QUERY_KEYWORDS if kw in query_lower]
        keyword_class = "sensitive" if matched_keywords else "normal"

        chat_id = ctx.get("chat_id", "")
        agent_id = ctx.get("agent_id", "")

        # Scenario 1: Bulk export detection (scope=all_visible + top_k>50)
        # This is a warning-level check — allow but audit for review
        if scope == "all_visible" and top_k > 50:
            return Decision(
                action="allow",
                reason="search_chat permitted (bulk export flagged for audit)",
                audit_event={
                    "event_type": "chat_search_bulk_export_attempt",
                    "tool": tool,
                    "scope": scope,
                    "top_k": top_k,
                    "query_hash": query_hash,
                    "query_length": len(query),
                    "keyword_class": keyword_class,
                    "agent_id": agent_id,
                    "chat_id": chat_id,
                },
            )

        # Scenario 2: Sensitive query detection (exfil keywords)
        # Also warning-level — allow but flag for ChainDetector link E
        if query and looks_like_exfil_query(query):
            return Decision(
                action="allow",
                reason="search_chat permitted (sensitive query logged for audit)",
                audit_event={
                    "event_type": "chat_search_sensitive_query",
                    "tool": tool,
                    "scope": scope,
                    "query_hash": query_hash,
                    "query_length": len(query),
                    "keyword_class": keyword_class,
                    "agent_id": agent_id,
                    "chat_id": chat_id,
                },
            )

        # Default: allow (L1 read-only operation)
        return Decision(action="allow", reason="search_chat permitted (L1)")
