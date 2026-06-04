"""Phase 9 M9.5: Memory scope filter — fail-closed scope isolation.

MemoryScopeFilter enforces that all memory operations have explicit scope context.
If a required field is missing, the filter raises ValueError rather than degrading
to a broader scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mini_claw.agent.context import AgentContext


@dataclass
class MemoryScopeFilter:
    """Scope filter for memory retrieval/storage operations.

    All fields are optional, but build_scope_filter() will fail-closed if
    the requested scope requires a field that's missing from context.
    """

    scope_type: str  # agent/workspace/user/session/all
    agent_id: str | None = None
    workspace_dir: str | None = None
    chat_id: str | None = None
    channel_name: str | None = None
    session_id: str | None = None


def build_scope_filter(
    ctx: AgentContext | dict[str, Any],
    namespace: str,
    requested_scope: str,
) -> MemoryScopeFilter:
    """Build a fail-closed scope filter from context.

    Args:
        ctx: AgentContext or dict with context fields
        namespace: memory/context (used for error messages)
        requested_scope: agent/workspace/user/session/all

    Returns:
        MemoryScopeFilter with populated fields

    Raises:
        ValueError: If requested scope requires a field that's missing from ctx
    """
    # Extract fields from context
    if isinstance(ctx, dict):
        agent_id = ctx.get("agent_id")
        workspace_dir = ctx.get("workspace_dir")
        chat_id = ctx.get("chat_id")
        channel_name = ctx.get("channel_name")
        session_id = ctx.get("session_id")
    else:
        agent_id = getattr(ctx, "agent_id", None)
        workspace_dir = getattr(ctx, "workspace_dir", None)
        chat_id = getattr(ctx, "chat_id", None)
        channel_name = getattr(ctx, "channel_name", None)
        session_id = getattr(ctx, "session_id", None)

    # Phase 9 M9.5 fail-closed: channel_name is required for ANY scope.
    # P0.2 mandates that retrieval is channel-scoped — caller bug if missing.
    if not channel_name:
        raise ValueError(
            f"{namespace} scope '{requested_scope}' requires channel_name, "
            "but ctx.channel_name is missing"
        )

    # Fail-closed: if requested scope requires a field that's missing, reject
    if requested_scope == "agent":
        if not agent_id:
            raise ValueError(f"{namespace} scope 'agent' requires agent_id, but ctx.agent_id is missing")
        return MemoryScopeFilter(
            scope_type="agent",
            agent_id=agent_id,
            channel_name=channel_name,
        )

    elif requested_scope == "workspace":
        if not workspace_dir:
            raise ValueError(f"{namespace} scope 'workspace' requires workspace_dir, but ctx.workspace_dir is missing")
        return MemoryScopeFilter(
            scope_type="workspace",
            workspace_dir=str(workspace_dir),
            channel_name=channel_name,
        )

    elif requested_scope == "session":
        if not session_id:
            raise ValueError(f"{namespace} scope 'session' requires session_id, but ctx.session_id is missing")
        if not chat_id:
            raise ValueError(f"{namespace} scope 'session' requires chat_id, but ctx.chat_id is missing")
        return MemoryScopeFilter(
            scope_type="session",
            session_id=session_id,
            chat_id=chat_id,
            channel_name=channel_name,
        )

    elif requested_scope == "user":
        # User scope still needs agent_id (per-agent user memories).
        if not agent_id:
            raise ValueError(f"{namespace} scope 'user' requires agent_id, but ctx.agent_id is missing")
        return MemoryScopeFilter(
            scope_type="user",
            agent_id=agent_id,
            channel_name=channel_name,
        )

    elif requested_scope == "all":
        # All visible: depends on what fields are available
        return MemoryScopeFilter(
            scope_type="all",
            agent_id=agent_id,
            workspace_dir=str(workspace_dir) if workspace_dir else None,
            chat_id=chat_id,
            channel_name=channel_name,
            session_id=session_id,
        )

    else:
        raise ValueError(f"Unknown scope type: {requested_scope}")
