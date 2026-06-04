"""RAG tools for MiniClaw (Phase 8 M2).

8 context RAG tools + 8 memory RAG tools (M5).
All tools call RagManager methods and return formatted strings for LLM.
"""

from __future__ import annotations

from mini_claw.tools.registry import Tool

__all__ = [
    "TOOL_INDEX_CONTEXT",
    "TOOL_SEARCH_CONTEXT",
    "TOOL_LIST_CONTEXTS",
    "TOOL_INSPECT_CONTEXT",
    "TOOL_CLEAR_CONTEXT",
    "TOOL_ARCHIVE_CONTEXT",
    "TOOL_DELETE_CONTEXT",
    "TOOL_READ_SENSITIVE_CONTEXT",
    "TOOL_REINDEX_CONTEXT",
    "TOOL_DIFF_CONTEXT",
    "TOOL_REEMBED_CONTEXT",
    "TOOL_REBIND_CONTEXT",
    "TOOL_RAG_STATUS",
    # Phase 8 M5: memory
    "TOOL_MEMORY_REMEMBER",
    "TOOL_MEMORY_SEARCH",
    "TOOL_MEMORY_LIST",
    "TOOL_MEMORY_INSPECT",
    "TOOL_MEMORY_DELETE",
    "TOOL_MEMORY_PIN",
    "TOOL_MEMORY_UNPIN",
    "TOOL_MEMORY_COMPACT_TO_RAG",
]


# ========== index_context ==========

async def _index_context(path: str, title: str | None = None, *, ctx) -> str:
    """Index a file as context."""
    rag_manager = ctx.rag_manager
    if not rag_manager:
        return "[ERROR] RAG subsystem not initialized"

    item_id, error = rag_manager.index_context(
        path, ctx={"agent_id": ctx.agent_id, "workspace_dir": ctx.workspace_dir, "sandbox_mode": ctx.sandbox_mode, "chat_id": ctx.chat_id, "session_id": getattr(ctx, "session_id", None), "channel_name": getattr(ctx, "channel_name", None)}, title=title
    )
    if error:
        return f"[ERROR] {error}"
    return f"Indexed {path} as context (item_id={item_id})"


TOOL_INDEX_CONTEXT = Tool(
    name="index_context",
    description="Index a file for retrieval-augmented context (requires RAG enabled).",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to index"},
            "title": {"type": "string", "description": "Optional title for the indexed item"},
        },
        "required": ["path"],
    },
    handler=_index_context,
    permission_level="L2",
)


# ========== search_context ==========

async def _search_context(query: str, top_k: int = 6, *, ctx) -> str:
    """Search indexed context."""
    rag_manager = ctx.rag_manager
    if not rag_manager:
        return "[ERROR] RAG subsystem not initialized"

    results, error = rag_manager.search_context(
        query, ctx={"agent_id": ctx.agent_id, "workspace_dir": ctx.workspace_dir, "session_id": getattr(ctx, "session_id", None)}, top_k=top_k
    )
    if error:
        return f"[ERROR] {error}"
    if not results:
        return f"No results found for query: {query}"

    lines = [f"Found {len(results)} result(s) for query: {query}\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r.source_path}:{r.start_line}-{r.end_line}")
        if r.section_title:
            lines.append(f"    Section: {r.section_title}")
        if r.symbol_name:
            lines.append(f"    Symbol: {r.symbol_name}")
        lines.append(f"    {r.content[:200]}..." if len(r.content) > 200 else f"    {r.content}")
        lines.append("")
    return "\n".join(lines)


TOOL_SEARCH_CONTEXT = Tool(
    name="search_context",
    description="Search indexed context (requires RAG enabled).",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "top_k": {"type": "integer", "description": "Number of results to return", "default": 6},
        },
        "required": ["query"],
    },
    handler=_search_context,
    permission_level="L1",
)


# ========== list_contexts ==========

async def _list_contexts(status: str | None = None, *, ctx) -> str:
    """List indexed contexts."""
    rag_manager = ctx.rag_manager
    if not rag_manager:
        return "[ERROR] RAG subsystem not initialized"

    items = rag_manager.list_contexts(
        ctx={"agent_id": ctx.agent_id, "workspace_dir": ctx.workspace_dir}, status=status
    )
    if not items:
        return "No indexed contexts found."

    lines = [f"Found {len(items)} indexed context(s):\n"]
    for item in items:
        lines.append(f"- {item.item_id}: {item.title or item.source_path}")
        lines.append(f"  Status: {item.status}, Created: {item.created_at}, Type: {item.source_type}")
    return "\n".join(lines)


TOOL_LIST_CONTEXTS = Tool(
    name="list_contexts",
    description="List indexed contexts (requires RAG enabled).",
    input_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Filter by status (active/warm/archived)", "enum": ["active", "warm", "archived"]},
        },
    },
    handler=_list_contexts,
    permission_level="L1",
)


# ========== inspect_context ==========

async def _inspect_context(context_id: str, *, ctx) -> str:
    """Inspect context metadata."""
    rag_manager = ctx.rag_manager
    if not rag_manager:
        return "[ERROR] RAG subsystem not initialized"

    item, error = rag_manager.inspect_context(
        context_id, ctx={"agent_id": ctx.agent_id}
    )
    if error:
        return f"[ERROR] {error}"
    if not item:
        return f"Context not found: {context_id}"

    return f"""Context: {item.item_id}
Title: {item.title or "(none)"}
Source: {item.source_path}
Type: {item.source_type}
Status: {item.status}
Created: {item.created_at}
Updated: {item.updated_at}
Hash: {item.content_hash}
Sensitivity: {item.sensitivity_level}
Active version: {item.active_version}"""


TOOL_INSPECT_CONTEXT = Tool(
    name="inspect_context",
    description="Inspect context metadata (requires RAG enabled).",
    input_schema={
        "type": "object",
        "properties": {
            "context_id": {"type": "string", "description": "Context item ID"},
        },
        "required": ["context_id"],
    },
    handler=_inspect_context,
    permission_level="L1",
)


# ========== clear_context ==========

async def _clear_context(*, ctx) -> str:
    """Clear active contexts for current session."""
    rag_manager = ctx.rag_manager
    if not rag_manager:
        return "[ERROR] RAG subsystem not initialized"

    count, error = rag_manager.clear_context(
        ctx={"agent_id": ctx.agent_id, "session_id": getattr(ctx, "session_id", None)}
    )
    if error:
        return f"[ERROR] {error}"
    return f"Cleared {count} active context(s)."


TOOL_CLEAR_CONTEXT = Tool(
    name="clear_context",
    description="Clear active contexts for current session (requires RAG enabled).",
    input_schema={"type": "object", "properties": {}},
    handler=_clear_context,
    permission_level="L2",
)


# ========== archive_context ==========

async def _archive_context(context_id: str, *, ctx) -> str:
    """Archive a context item."""
    rag_manager = ctx.rag_manager
    if not rag_manager:
        return "[ERROR] RAG subsystem not initialized"

    success, error = rag_manager.archive_context(
        context_id, ctx={"agent_id": ctx.agent_id}
    )
    if not success:
        return f"[ERROR] {error}"
    return f"Archived context: {context_id}"


TOOL_ARCHIVE_CONTEXT = Tool(
    name="archive_context",
    description="Archive a context item (requires RAG enabled).",
    input_schema={
        "type": "object",
        "properties": {
            "context_id": {"type": "string", "description": "Context item ID to archive"},
        },
        "required": ["context_id"],
    },
    handler=_archive_context,
    permission_level="L2",
)


# ========== delete_context ==========

async def _delete_context(context_id: str, *, ctx) -> str:
    """Delete a context item (L3 approval required)."""
    rag_manager = ctx.rag_manager
    if not rag_manager:
        return "[ERROR] RAG subsystem not initialized"

    success, error = rag_manager.delete_context(
        context_id, ctx={"agent_id": ctx.agent_id}
    )
    if not success:
        return f"[ERROR] {error}"
    return f"Deleted context: {context_id}"


TOOL_DELETE_CONTEXT = Tool(
    name="delete_context",
    description="Delete a context item permanently (requires L3 approval and RAG enabled).",
    input_schema={
        "type": "object",
        "properties": {
            "context_id": {"type": "string", "description": "Context item ID to delete"},
        },
        "required": ["context_id"],
    },
    handler=_delete_context,
    permission_level="L3",
)


# ========== read_sensitive_context ==========

async def _read_sensitive_context(context_id: str, chunk_id: str, *, ctx) -> str:
    """Read full content of a high-sensitivity chunk (L3 approval required)."""
    rag_manager = ctx.rag_manager
    if not rag_manager:
        return "[ERROR] RAG subsystem not initialized"

    content, error = rag_manager.read_sensitive_context(
        context_id, chunk_id, ctx={"agent_id": ctx.agent_id}
    )
    if error:
        return f"[ERROR] {error}"
    if not content:
        return f"Chunk not found: {chunk_id}"

    return f"""[SENSITIVE CONTENT]
Context: {context_id}
Chunk: {chunk_id}
Content:
{content}"""


TOOL_READ_SENSITIVE_CONTEXT = Tool(
    name="read_sensitive_context",
    description="Read full content of a high-sensitivity chunk (requires L3 approval and RAG enabled).",
    input_schema={
        "type": "object",
        "properties": {
            "context_id": {"type": "string", "description": "Context item ID"},
            "chunk_id": {"type": "string", "description": "Chunk ID to read"},
        },
        "required": ["context_id", "chunk_id"],
    },
    handler=_read_sensitive_context,
    permission_level="L3",
)


# ========== reindex_context (Phase 8 M3) ==========

async def _reindex_context(context_id: str, dry_run: bool = False, *, ctx) -> str:
    """Re-chunk and re-index an item's source file."""
    rag_manager = ctx.rag_manager
    if not rag_manager:
        return "[ERROR] RAG subsystem not initialized"

    success, message = rag_manager.reindex_context(
        context_id,
        ctx={
            "agent_id": ctx.agent_id,
            "workspace_dir": ctx.workspace_dir,
            "sandbox_mode": ctx.sandbox_mode,
        },
        dry_run=dry_run,
    )
    if not success:
        return f"[ERROR] {message}"
    prefix = "Reindex dry-run" if dry_run else "Reindexed"
    return f"{prefix}: {context_id}; {message}"


TOOL_REINDEX_CONTEXT = Tool(
    name="reindex_context",
    description="Re-chunk and re-index an item's source file with active-version mapping.",
    input_schema={
        "type": "object",
        "properties": {
            "context_id": {"type": "string", "description": "Context item ID to reindex"},
            "dry_run": {"type": "boolean", "description": "Preview diff without switching active version", "default": False},
        },
        "required": ["context_id"],
    },
    handler=_reindex_context,
    permission_level="L2",
)


# ========== diff_context / reembed_context (Phase 8.3.5) ==========

async def _diff_context(context_id: str, last: bool = False, *, ctx) -> str:
    rag_manager = ctx.rag_manager
    if not rag_manager:
        return "[ERROR] RAG subsystem not initialized"
    success, message = rag_manager.diff_context(
        context_id,
        ctx={
            "agent_id": ctx.agent_id,
            "workspace_dir": ctx.workspace_dir,
            "sandbox_mode": ctx.sandbox_mode,
        },
        last=last,
    )
    if not success:
        return f"[ERROR] {message}"
    return f"Diff: {context_id}; {message}"


TOOL_DIFF_CONTEXT = Tool(
    name="diff_context",
    description="Preview current source file vs active RAG index, or inspect the last stored reindex diff.",
    input_schema={
        "type": "object",
        "properties": {
            "context_id": {"type": "string", "description": "Context item ID"},
            "last": {"type": "boolean", "description": "Show last stored diff instead of current dry-run", "default": False},
        },
        "required": ["context_id"],
    },
    handler=_diff_context,
    permission_level="L1",
)


async def _reembed_context(context_id: str, *, ctx) -> str:
    rag_manager = ctx.rag_manager
    if not rag_manager:
        return "[ERROR] RAG subsystem not initialized"
    success, message = rag_manager.reembed_context(
        context_id,
        ctx={"agent_id": ctx.agent_id, "workspace_dir": ctx.workspace_dir},
    )
    if not success:
        return f"[ERROR] {message}"
    return f"Reembedded: {context_id}; {message}"


TOOL_REEMBED_CONTEXT = Tool(
    name="reembed_context",
    description="Recompute embeddings for active chunks without rechunking or changing FTS.",
    input_schema={
        "type": "object",
        "properties": {
            "context_id": {"type": "string", "description": "Context item ID"},
        },
        "required": ["context_id"],
    },
    handler=_reembed_context,
    permission_level="L2",
)


# ========== rebind_context (Phase 8 M3) ==========

async def _rebind_context(context_id: str, new_path: str, *, ctx) -> str:
    """Update an item's source_path. Hash must match (else suggests reindex)."""
    rag_manager = ctx.rag_manager
    if not rag_manager:
        return "[ERROR] RAG subsystem not initialized"

    success, error = rag_manager.rebind_context(
        context_id,
        new_path,
        ctx={
            "agent_id": ctx.agent_id,
            "workspace_dir": ctx.workspace_dir,
            "sandbox_mode": ctx.sandbox_mode,
        },
    )
    if not success:
        return f"[ERROR] {error}"
    return f"Rebound {context_id} to {new_path}"


TOOL_REBIND_CONTEXT = Tool(
    name="rebind_context",
    description="Rebind an indexed item to a new file path (hash must match).",
    input_schema={
        "type": "object",
        "properties": {
            "context_id": {"type": "string", "description": "Context item ID"},
            "new_path": {"type": "string", "description": "New source path"},
        },
        "required": ["context_id", "new_path"],
    },
    handler=_rebind_context,
    permission_level="L2",
)


# ========== rag_status (Phase 8 M4.5) ==========

async def _rag_status(*, ctx) -> str:
    """Return a human-readable RAG health snapshot."""
    rag_manager = ctx.rag_manager
    if not rag_manager:
        return "[ERROR] RAG subsystem not initialized"
    return rag_manager.status_text()


TOOL_RAG_STATUS = Tool(
    name="rag_status",
    description="Show RAG subsystem health snapshot (FTS, vector backend, embedding, lifecycle counters).",
    input_schema={"type": "object", "properties": {}},
    handler=_rag_status,
    permission_level="L0",
)


# ============================================================
# Phase 8 M5: memory tools
# ============================================================


async def _memory_remember(content: str, memory_type: str = "user_preference", *, ctx) -> str:
    """Submit a long-term memory candidate for user approval."""
    rag_manager = ctx.rag_manager
    if not rag_manager:
        return "[ERROR] RAG subsystem not initialized"
    cand_id, approval_id, status = rag_manager.remember(
        content,
        ctx={
            "agent_id": ctx.agent_id,
            "chat_id": ctx.chat_id,
            "channel_name": getattr(ctx, "channel_name", None),
        },
        memory_type=memory_type,
    )
    if status.startswith("rejected:"):
        return f"[ERROR] Memory rejected: {status.split(':', 1)[1]}"
    if status == "submitted":
        return (
            f"Memory candidate submitted for approval.\n"
            f"  candidate_id: {cand_id}\n"
            f"  approval_id : {approval_id}\n"
            f"Run `/memory approve {cand_id}` after reviewing."
        )
    return f"Memory candidate {cand_id}: {status}"


TOOL_MEMORY_REMEMBER = Tool(
    name="memory_remember",
    description="Submit a long-term memory candidate (requires user approval).",
    input_schema={
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Memory content"},
            "memory_type": {
                "type": "string",
                "description": "Memory type",
                "default": "user_preference",
            },
        },
        "required": ["content"],
    },
    handler=_memory_remember,
    permission_level="L3",
)


async def _memory_search(query: str, top_k: int = 3, scope: str = "agent", *, ctx) -> str:
    """Search long-term memories for the current agent.

    Phase 9 M9.5: ``scope`` may be agent / workspace / user / all. Channel
    name is required (fail-closed); calls without channel_name on ctx are
    rejected.
    """
    rag_manager = ctx.rag_manager
    if not rag_manager:
        return "[ERROR] RAG subsystem not initialized"
    results, error = rag_manager.search_memory(
        query,
        ctx={
            "agent_id": ctx.agent_id,
            "chat_id": getattr(ctx, "chat_id", None),
            "workspace_dir": ctx.workspace_dir,
            "session_id": getattr(ctx, "session_id", None),
            "channel_name": getattr(ctx, "channel_name", None),
        },
        top_k=top_k,
        scope=scope,
    )
    if error:
        # Phase 9 M9.5: fail-closed scope violations get a dedicated audit event
        if "fail-closed" in error and getattr(ctx, "audit_logger", None):
            try:
                ctx.audit_logger.log_security_event(
                    event_type="memory_scope_violation_blocked",
                    details={"reason": error, "scope": scope, "tool": "memory_search"},
                    chat_id=getattr(ctx, "chat_id", None),
                    agent_id=ctx.agent_id,
                )
            except Exception:
                pass
        return f"[ERROR] {error}"
    if not results:
        return f"No memories found for query: {query}"
    lines = [f"Found {len(results)} memory result(s):\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] type={r.source_type} item={r.item_id}")
        lines.append(f"    {r.content[:300]}")
    return "\n".join(lines)


TOOL_MEMORY_SEARCH = Tool(
    name="memory_search",
    description="Search long-term memories for the current agent.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "top_k": {"type": "integer", "default": 3},
            "scope": {
                "type": "string",
                "enum": ["agent", "workspace", "user", "all"],
                "default": "agent",
                "description": "Memory scope: agent (private), workspace (shared in workspace), user (cross-agent), all.",
            },
        },
        "required": ["query"],
    },
    handler=_memory_search,
    permission_level="L1",
)


async def _memory_list(status: str = "active", *, ctx) -> str:
    rag_manager = ctx.rag_manager
    if not rag_manager:
        return "[ERROR] RAG subsystem not initialized"
    items = rag_manager.list_memories(
        ctx={"agent_id": ctx.agent_id}, status=status
    )
    if not items:
        return "No long-term memories."
    lines = [f"Long-term memories ({len(items)}):"]
    for it in items:
        pin = " [PIN]" if it.pinned else ""
        lines.append(f"- {it.item_id}: {it.title or it.source_type}{pin}")
    return "\n".join(lines)


TOOL_MEMORY_LIST = Tool(
    name="memory_list",
    description="List stored long-term memories owned by the current agent.",
    input_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "default": "active"},
        },
    },
    handler=_memory_list,
    permission_level="L1",
)


async def _memory_inspect(memory_id: str, *, ctx) -> str:
    rag_manager = ctx.rag_manager
    if not rag_manager:
        return "[ERROR] RAG subsystem not initialized"
    item, error = rag_manager.inspect_memory(
        memory_id, ctx={"agent_id": ctx.agent_id}
    )
    if error or not item:
        return f"[ERROR] {error or 'not found'}"
    return (
        f"Memory: {item.item_id}\n"
        f"Type: {item.source_type}\n"
        f"Scope: {item.scope_type}/{item.scope_id}\n"
        f"Status: {item.status}\n"
        f"Pinned: {bool(item.pinned)}\n"
        f"Confidence: {item.confidence:.2f}\n"
        f"Source chain: {item.source_chain_json or '(none)'}"
    )


TOOL_MEMORY_INSPECT = Tool(
    name="memory_inspect",
    description="Inspect a long-term memory item.",
    input_schema={
        "type": "object",
        "properties": {"memory_id": {"type": "string"}},
        "required": ["memory_id"],
    },
    handler=_memory_inspect,
    permission_level="L1",
)


async def _memory_delete(memory_id: str, *, ctx) -> str:
    rag_manager = ctx.rag_manager
    if not rag_manager:
        return "[ERROR] RAG subsystem not initialized"
    ok, error = rag_manager.delete_memory(
        memory_id, ctx={"agent_id": ctx.agent_id}
    )
    if not ok:
        return f"[ERROR] {error}"
    return f"Deleted memory: {memory_id}"


TOOL_MEMORY_DELETE = Tool(
    name="memory_delete",
    description="Delete a long-term memory (requires L3 approval).",
    input_schema={
        "type": "object",
        "properties": {"memory_id": {"type": "string"}},
        "required": ["memory_id"],
    },
    handler=_memory_delete,
    permission_level="L3",
)


async def _memory_pin(memory_id: str, *, ctx) -> str:
    rag_manager = ctx.rag_manager
    if not rag_manager:
        return "[ERROR] RAG subsystem not initialized"
    ok, error = rag_manager.pin_memory(memory_id, ctx={"agent_id": ctx.agent_id})
    if not ok:
        return f"[ERROR] {error}"
    return f"Pinned: {memory_id}"


TOOL_MEMORY_PIN = Tool(
    name="memory_pin",
    description="Pin a memory so lifecycle cleanup never removes it.",
    input_schema={
        "type": "object",
        "properties": {"memory_id": {"type": "string"}},
        "required": ["memory_id"],
    },
    handler=_memory_pin,
    permission_level="L2",
)


async def _memory_unpin(memory_id: str, *, ctx) -> str:
    rag_manager = ctx.rag_manager
    if not rag_manager:
        return "[ERROR] RAG subsystem not initialized"
    ok, error = rag_manager.unpin_memory(memory_id, ctx={"agent_id": ctx.agent_id})
    if not ok:
        return f"[ERROR] {error}"
    return f"Unpinned: {memory_id}"


TOOL_MEMORY_UNPIN = Tool(
    name="memory_unpin",
    description="Remove pinned protection from a memory.",
    input_schema={
        "type": "object",
        "properties": {"memory_id": {"type": "string"}},
        "required": ["memory_id"],
    },
    handler=_memory_unpin,
    permission_level="L2",
)


async def _memory_compact_to_rag(*, ctx) -> str:
    """Surface pending candidates produced by auto-extractors for review.

    Auto extractors (session compaction / TaskState pruning / WorkflowMerger)
    write candidates with ``status='pending'`` to ``memory_candidates``;
    this tool just lists what is awaiting approval. Approval still requires
    ``/memory approve <candidate_id>``.
    """
    rag_manager = ctx.rag_manager
    if not rag_manager:
        return "[ERROR] RAG subsystem not initialized"
    pending = rag_manager.list_pending_memories(limit=20)
    if not pending:
        return "No pending memory candidates."
    lines = [f"{len(pending)} pending memory candidate(s):"]
    for c in pending:
        src = c.source_type or "?"
        lines.append(f"- {c.candidate_id} [{src}] {c.content[:200]}")
    return "\n".join(lines)


TOOL_MEMORY_COMPACT_TO_RAG = Tool(
    name="memory_compact_to_rag",
    description="List pending memory candidates queued by auto-extractors.",
    input_schema={"type": "object", "properties": {}},
    handler=_memory_compact_to_rag,
    permission_level="L3",
)
