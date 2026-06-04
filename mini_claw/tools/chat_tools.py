"""Phase 9 M9.1: Chat search tool — full-text search over messages.

Permission level: L1 (default allow), but PermissionGate evaluates query for
sensitive keywords and records to ChainDetector for link E enforcement.
"""

from __future__ import annotations

from mini_claw.tools.registry import Tool


async def _search_chat_handler(
    query: str,
    scope: str = "current_session",
    top_k: int = 10,
    **kwargs,
) -> str:
    """Async wrapper for execute_search_chat to match Tool handler signature.

    Phase 9 M9.1: Reads ctx and chat_search_manager from ToolContext.
    """
    ctx = kwargs.get("ctx")
    # Get chat_search_manager from ToolContext (preferred) or kwargs (fallback)
    chat_search_manager = (
        getattr(ctx, "chat_search_manager", None) if ctx else None
    ) or kwargs.get("chat_search_manager")
    return execute_search_chat(
        query=query,
        scope=scope,
        top_k=top_k,
        ctx=ctx,
        chat_search_manager=chat_search_manager,
    )


TOOL_SEARCH_CHAT = Tool(
    name="search_chat",
    description=(
        "Search conversation history using full-text search. "
        "Useful for finding past discussions, decisions, or references. "
        "Returns matching messages with role, content, and timestamp."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query to match in message content",
            },
            "scope": {
                "type": "string",
                "enum": ["current_session", "current_agent", "workspace", "all_visible"],
                "description": (
                    "Search scope: current_session (this conversation), "
                    "current_agent (all sessions of this agent), "
                    "workspace (all messages in this project), "
                    "all_visible (all messages on this channel)"
                ),
                "default": "current_session",
            },
            "top_k": {
                "type": "integer",
                "description": "Maximum number of results to return",
                "default": 10,
            },
        },
        "required": ["query"],
    },
    handler=_search_chat_handler,
    permission_level="L1",
)


def execute_search_chat(
    query: str,
    scope: str = "current_session",
    top_k: int = 10,
    *,
    ctx: any,
    chat_search_manager: any,
) -> str:
    """Execute chat search with scope isolation.

    Returns formatted results or error message if scope requirements not met.
    """
    try:
        results = chat_search_manager.search(query, scope=scope, ctx=ctx, top_k=top_k)
        if not results:
            return f"No messages found matching '{query}' in scope={scope}"

        lines = [f"Found {len(results)} message(s) matching '{query}' (scope={scope}):\n"]
        for i, msg in enumerate(results, 1):
            role = msg.get("role", "unknown")
            content = (msg.get("content") or "")[:200]
            created_at = msg.get("created_at", 0)
            lines.append(f"{i}. [{role}] {content}... (ts={created_at})")

        return "\n".join(lines)
    except ValueError as e:
        return f"[ERROR] Chat search failed: {e}"
    except Exception as e:
        return f"[ERROR] Unexpected error during chat search: {e}"
