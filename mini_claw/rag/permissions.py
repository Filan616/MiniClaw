"""RAG permission helper functions (Phase 8 M2).

These functions are called by :class:`mini_claw.permissions.gate.PermissionGate`
when evaluating RAG tool calls. They encapsulate RAG-specific checks:
- index_context: workspace + non-sensitive + non-bypass + file size + non-binary
- search_context: scope isolation (agent/workspace/session) + sensitivity level
- delete_context: owner check
- memory_write: source chain completeness

M2.5 will extend this with RAG chain detection integration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = [
    "check_index_permission",
    "check_search_scope",
    "check_delete_permission",
    "check_memory_write_permission",
]


def check_index_permission(
    path: str,
    ctx: dict[str, Any],
    config: Any,  # RagConfig
    policy: Any,  # PermissionPolicy
) -> tuple[bool, str]:
    """Check if indexing *path* is allowed.

    Returns ``(allowed: bool, deny_reason: str)``.

    Checks (in order):
    1. bypass mode → deny (M2 plan: "index_context + bypass 不放行")
    2. sensitive path → deny (unless config.security.allow_sensitive_index=True)
    3. path outside workspace → deny
    4. file does not exist or is directory → deny
    5. file size > config.chunk.max_file_size_mb → deny
    6. binary file → deny (config.chunk.binary_file_policy controls)
    """
    sandbox_mode = ctx.get("sandbox_mode", "safe")
    workspace_dir = ctx.get("workspace_dir")

    # 1. Bypass mode check
    if sandbox_mode == "bypass" and not config.security.allow_index_in_bypass:
        return False, "index_context not allowed in bypass mode"

    # 2. Sensitive path check
    if policy.is_sensitive_path(path):
        if not config.security.allow_sensitive_index:
            return False, "sensitive path indexing denied by policy"
        # If allow_sensitive_index=True, still require approval (handled by gate L4)

    # 3. Workspace check
    if workspace_dir:
        if not policy.path_in_workspace(path, Path(workspace_dir)):
            return False, "path outside workspace"

    # 4. File existence
    file_path = Path(path)
    if not file_path.exists():
        return False, "file not found"
    if file_path.is_dir():
        return False, "path is a directory, not a file"

    # 5. File size check
    max_size_bytes = config.chunk.max_file_size_mb * 1024 * 1024
    if file_path.stat().st_size > max_size_bytes:
        return False, f"file exceeds max size {config.chunk.max_file_size_mb}MB"

    # 6. Binary file check
    if config.chunk.binary_file_policy == "deny":
        if _is_binary(file_path):
            return False, "binary file indexing denied by policy"

    return True, ""


def check_search_scope(
    scope_filter: dict[str, Any],
    ctx: dict[str, Any],
    config: Any,  # RagConfig
) -> tuple[bool, str]:
    """Check if search scope is allowed.

    Returns ``(allowed: bool, deny_reason: str)``.

    Checks:
    1. scope_filter must match current agent_id / workspace_dir / session_id (default scope)
    2. cross-agent context sharing requires config.sharing.allow_cross_agent_context=True
    3. (M2.5) query not in EXFIL_QUERY_KEYWORDS

    Phase 9 fix: workspace_dir comparison normalizes both sides via Path.resolve()
    to handle cases where stored paths contain unresolved relative segments
    (e.g. "workspaces/../..") vs. resolved canonical forms.
    """
    current_agent_id = ctx.get("agent_id", "")
    current_workspace_dir = ctx.get("workspace_dir")
    current_session_id = ctx.get("session_id")

    requested_agent_id = scope_filter.get("owner_agent_id")
    requested_workspace_dir = scope_filter.get("workspace_dir")
    requested_session_id = scope_filter.get("session_id")

    # Agent scope check
    if requested_agent_id and requested_agent_id != current_agent_id:
        if not config.sharing.allow_cross_agent_context:
            return False, "cross-agent context access denied by policy"

    # Workspace scope check (normalize both sides to handle unresolved paths)
    if requested_workspace_dir:
        try:
            req_norm = str(Path(str(requested_workspace_dir)).resolve())
        except (OSError, ValueError):
            req_norm = str(requested_workspace_dir)
        try:
            cur_norm = (
                str(Path(str(current_workspace_dir)).resolve())
                if current_workspace_dir is not None
                else None
            )
        except (OSError, ValueError):
            cur_norm = str(current_workspace_dir) if current_workspace_dir else None

        if req_norm != cur_norm:
            if not config.sharing.allow_workspace_context_sharing:
                return False, "cross-workspace context access denied by policy"

    # Session scope check (always enforced unless explicitly overridden)
    if requested_session_id and requested_session_id != current_session_id:
        # Session isolation is strict by default
        return False, "cross-session context access not allowed"

    return True, ""


def check_delete_permission(
    item_id: str,
    ctx: dict[str, Any],
    store: Any,  # RagStore
) -> tuple[bool, str]:
    """Check if deleting *item_id* is allowed.

    Returns ``(allowed: bool, deny_reason: str)``.

    Checks:
    1. item exists
    2. current agent owns the item (owner_agent_id matches ctx agent_id)
    """
    item = store.get_item(item_id)
    if item is None:
        return False, "item not found"

    current_agent_id = ctx.get("agent_id", "")
    if item.owner_agent_id != current_agent_id:
        return False, "cannot delete item owned by another agent"

    return True, ""


def check_memory_write_permission(
    candidate: Any,  # MemoryCandidate
) -> tuple[bool, str]:
    """Check if memory candidate has complete source chain (M5 traceability).

    Returns ``(allowed: bool, deny_reason: str)``.

    Checks:
    1. source_chain_json is non-empty
    2. created_by_agent_id is set
    3. created_from_chat_id is set
    """
    if not candidate.source_chain_json or candidate.source_chain_json == "{}":
        return False, "memory candidate missing source_chain_json"

    if not candidate.created_by_agent_id:
        return False, "memory candidate missing created_by_agent_id"

    if not candidate.created_from_chat_id:
        return False, "memory candidate missing created_from_chat_id"

    return True, ""


def _is_binary(path: Path) -> bool:
    """Heuristic: read first 8192 bytes; if contains null byte → binary."""
    try:
        with path.open("rb") as f:
            chunk = f.read(8192)
        return b"\x00" in chunk
    except OSError:
        # If can't read, assume binary to be safe
        return True
