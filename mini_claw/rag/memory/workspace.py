"""Phase 9 M9.3: Workspace memory — project-scoped memories.

Workspace memories are shared across all agents working in the same workspace_dir.
Typical types: project_constraint, architecture_decision, tech_stack_choice, project_preference.
"""

from __future__ import annotations

from typing import Any

from mini_claw.rag.models import WORKSPACE_MEMORY_TYPES


def remember_workspace(
    content: str,
    *,
    memory_type: str = "project_constraint",
    ctx: dict[str, Any],
    rag_manager: Any,
) -> tuple[str | None, str | None, str]:
    """Submit a workspace-scoped memory candidate.

    Wrapper around RagManager.remember() that forces scope_type='workspace'.

    Args:
        content: Memory text
        memory_type: One of WORKSPACE_MEMORY_TYPES (default: project_constraint)
        ctx: Must contain workspace_dir
        rag_manager: RagManager instance

    Returns:
        (candidate_id, approval_id, status) — same as RagManager.remember()
    """
    workspace_dir = ctx.get("workspace_dir")
    if not workspace_dir:
        return None, None, "rejected:no_workspace_dir"

    if memory_type not in WORKSPACE_MEMORY_TYPES:
        return None, None, f"rejected:invalid_workspace_type:{memory_type}"

    return rag_manager.remember(
        content,
        ctx=ctx,
        memory_type=memory_type,
        scope_type="workspace",
        scope_id=str(workspace_dir),
    )
