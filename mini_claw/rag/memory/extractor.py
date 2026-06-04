"""Memory candidate extractors (Phase 8 M5).

Three sources, three functions. Each returns ``list[MemoryCandidate]``
with full source-chain metadata so audit can answer "where did this
memory come from?" months later.

Cheap pure functions: NO LLM calls here. Consolidation happens later
in :func:`mini_claw.rag.memory.consolidator.consolidate`.

Heuristic gate: each extractor only emits candidates when the input
text contains decision/preference keywords (避免每次会话都生成噪声候选).
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

from mini_claw.rag.memory.candidate import MemoryCandidate

__all__ = [
    "extract_from_session_compaction",
    "extract_from_task_state",
    "extract_from_workflow_merger",
    "extract_from_agent_summary",
]


# Phrases that suggest the surrounding text records a stable decision /
# preference / rule worth promoting to long-term memory.
_DECISION_KEYWORDS: tuple[str, ...] = (
    "decided",
    "agreed",
    "prefer",
    "preference",
    "rule",
    "policy",
    "constraint",
    "convention",
    "always",
    "never",
    "must",
    # Phase 9 WM-2: structured action verbs that surface as workflow decisions
    "migrate",
    "use ",
    "adopt",
    "switch to",
    "deprecate",
    "require",
    "enforce",
    "决定",
    "选择",
    "偏好",
    "约定",
    "原则",
    "规则",
    "约束",
    "始终",
    "从不",
    "必须",
)

# Phase 9 M9.4: Enhanced classification keywords for workspace-scoped types
_PROJECT_CONSTRAINT_KEYWORDS = (
    "project",
    "constraint",
    "requirement",
    "must not",
    "禁止",
    "项目约束",
    "项目要求",
)

_ARCHITECTURE_KEYWORDS = (
    "architecture",
    "design",
    "pattern",
    "structure",
    "架构",
    "设计模式",
    "系统结构",
)

_TECH_STACK_KEYWORDS = (
    "use",
    "library",
    "framework",
    "dependency",
    "技术栈",
    "依赖",
    "框架",
    "库",
)


def _classify_memory_type(text: str) -> tuple[str, str]:
    """Phase 9 M9.4: Classify memory type and scope.

    Returns: (memory_type, scope_type)
    - workspace-scoped: project_constraint, architecture_decision, tech_stack_choice
    - agent-scoped: user_preference, project_rule
    """
    lower = text.lower()

    # Workspace-scoped types (Phase 9 M9.3)
    if any(k in lower for k in _PROJECT_CONSTRAINT_KEYWORDS):
        return "project_constraint", "workspace"
    if any(k in lower for k in _ARCHITECTURE_KEYWORDS):
        return "architecture_decision", "workspace"
    if any(k in lower for k in _TECH_STACK_KEYWORDS):
        return "tech_stack_choice", "workspace"

    # Agent-scoped types (default)
    if any(k in lower for k in ("prefer", "preference", "偏好")):
        return "user_preference", "agent"

    return "project_rule", "agent"



def _has_decision_marker(text: str) -> bool:
    if not text:
        return False
    lower = text.lower()
    return any(kw in lower for kw in _DECISION_KEYWORDS)


def _now() -> int:
    return int(time.time())


def _new_id() -> str:
    return f"cand-{uuid.uuid4().hex[:12]}"


# ====================================================================
# Phase 9 ac-3: Structured extraction from tool_calls
# ====================================================================


def _extract_from_tool_calls(
    messages: list[dict[str, Any]],
    *,
    chat_id: str,
    agent_id: str,
    session_id: str | None,
    channel: str | None,
    workspace_dir: str | None,
    msg_ids: list[str],
) -> list[MemoryCandidate]:
    """Phase 9 ac-3: Extract memory candidates from structured tool_calls field.

    Focuses on run_shell and write_file tool calls which contain verifiable
    structured information about commands executed and files modified.

    This is more reliable than text-based heuristics because:
    1. Tool calls are structured data from actual tool execution
    2. run_shell success/error logs contain concrete facts
    3. write_file operations record actual changes made

    Returns:
        List of MemoryCandidate with source_type='tool_calls'
    """
    candidates: list[MemoryCandidate] = []

    for msg in messages:
        if msg.get("role") != "assistant":
            continue

        # Collect message ID
        if "id" in msg:
            msg_id_str = str(msg["id"])
            if msg_id_str not in msg_ids:
                msg_ids.append(msg_id_str)

        # Extract tool_calls field
        tool_calls = msg.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            continue

        for call in tool_calls:
            if not isinstance(call, dict):
                continue

            # OpenAI-style: {"function": {"name": "...", "arguments": "..."}}
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            tool_name = fn.get("name") or call.get("name") or ""
            args = fn.get("arguments") or call.get("arguments") or {}

            # Only extract from run_shell and write_file (structured operations)
            if tool_name not in ("run_shell", "write_file"):
                continue

            # Extract structured facts
            if tool_name == "run_shell":
                fact_content = _extract_run_shell_fact(args)
            elif tool_name == "write_file":
                fact_content = _extract_write_file_fact(args)
            else:
                continue

            if not fact_content or len(fact_content) < 12 or len(fact_content) > 600:
                continue

            # Classify memory type and scope
            memory_type, scope_type = _classify_memory_type(fact_content)

            # Determine scope_id
            if scope_type == "workspace":
                if not workspace_dir:
                    continue
                scope_id = workspace_dir
            else:
                scope_id = agent_id

            candidates.append(
                MemoryCandidate(
                    candidate_id=_new_id(),
                    content=fact_content,
                    memory_type=memory_type,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    source_type="tool_calls",
                    status="pending",
                    created_at=_now(),
                    updated_at=_now(),
                    stability=4,  # Higher than text extraction (3)
                    reuse_value=4,
                    sensitivity=1,
                    confidence=0.85,  # Higher confidence for structured data
                    source_chain_json=json.dumps(
                        {
                            "source": "tool_calls",
                            "tool_name": tool_name,
                            "chat_id": chat_id,
                            "agent_id": agent_id,
                            "session_id": session_id,
                            "channel": channel,
                            "workspace_dir": workspace_dir,
                        }
                    ),
                    source_message_ids=",".join(msg_ids) if msg_ids else None,
                    source_session_id=session_id,
                    created_by_agent_id=agent_id,
                    created_from_chat_id=chat_id,
                    created_from_channel=channel,
                )
            )

    return candidates


def _extract_run_shell_fact(args: dict | str) -> str | None:
    """Extract memory-worthy fact from run_shell arguments.

    Focus on test commands, build commands, and successful operations
    that represent project knowledge.
    """
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            return None

    if not isinstance(args, dict):
        return None

    command = args.get("command", "")
    if not isinstance(command, str) or len(command) < 5:
        return None

    # Extract commands that represent project knowledge
    command_lower = command.lower()

    # Test commands
    if any(kw in command_lower for kw in ["pytest", "npm test", "cargo test", "go test", "python -m unittest"]):
        return f"Test command: {command}"

    # Build commands
    if any(kw in command_lower for kw in ["npm run build", "cargo build", "make", "mvn compile"]):
        return f"Build command: {command}"

    # Lint/format commands
    if any(kw in command_lower for kw in ["black", "ruff", "eslint", "prettier", "cargo fmt"]):
        return f"Lint/format command: {command}"

    # Only return if it looks decision-worthy
    if _has_decision_marker(command):
        return f"Shell command: {command}"

    return None


def _extract_write_file_fact(args: dict | str) -> str | None:
    """Extract memory-worthy fact from write_file arguments.

    Focus on configuration files and important project files.
    """
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            return None

    if not isinstance(args, dict):
        return None

    file_path = args.get("file_path", "")
    if not isinstance(file_path, str):
        return None

    # Only extract writes to important configuration/project files
    important_files = [
        "pyproject.toml",
        "package.json",
        "Cargo.toml",
        "go.mod",
        "pom.xml",
        "Makefile",
        "Dockerfile",
        ".gitignore",
        "requirements.txt",
        "README.md",
        "CLAUDE.md",
    ]

    file_name = file_path.split("/")[-1].split("\\")[-1]
    if file_name in important_files:
        return f"Modified project file: {file_name}"

    return None


# ====================================================================
# Source 1: Session compaction (SessionManager.compact_history)
# ====================================================================


def extract_from_session_compaction(
    messages: list[dict[str, Any]],
    *,
    chat_id: str,
    agent_id: str,
    session_id: str | None = None,
    channel: str | None = None,
    workspace_dir: str | None = None,
    source_priority: list[str] | None = None,
) -> list[MemoryCandidate]:
    """Scan compacted messages for decision-shaped statements.

    Phase 9 ac-3: Extracts from TWO sources (priority order):
    1. Structured tool_calls field (run_shell, write_file) - HIGHER PRIORITY
    2. Message text content (fallback for user decisions)

    When source_priority=['tool_calls'], ONLY structured extraction is used.
    This ensures auto memory candidates come from verifiable structured sources.

    Phase 9 M9.4: Now classifies into workspace/agent scope based on content;
    workspace_dir param required for workspace-scoped candidates.

    Phase 9 ac-4: source_priority param filters which sources are allowed.
    If source_priority is provided and 'compaction' is not in the list,
    returns empty list (no extraction).
    """
    if not messages:
        return []

    # Phase 9 ac-4: Check source_priority filter
    if source_priority is not None and "compaction" not in source_priority:
        return []

    candidates: list[MemoryCandidate] = []
    msg_ids: list[str] = []

    # Phase 9 ac-3: Extract from structured tool_calls FIRST (higher priority)
    tool_call_candidates = _extract_from_tool_calls(
        messages=messages,
        chat_id=chat_id,
        agent_id=agent_id,
        session_id=session_id,
        channel=channel,
        workspace_dir=workspace_dir,
        msg_ids=msg_ids,
    )
    candidates.extend(tool_call_candidates)

    # If source_priority restricts to tool_calls only, return structured extraction only
    if source_priority is not None and source_priority == ["tool_calls"]:
        return candidates[:5]

    # Otherwise, also extract from text content (legacy behavior for user decisions)
    accumulated_text: list[str] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        if "id" in m:
            msg_id_str = str(m["id"])
            if msg_id_str not in msg_ids:
                msg_ids.append(msg_id_str)
        accumulated_text.append(content)

    full_text = "\n".join(accumulated_text)
    if not _has_decision_marker(full_text):
        return candidates[:5]  # Return tool_call candidates if any

    # One candidate per decision-shaped sentence (rough split). Cap at 5 total
    # so a long compaction window doesn't flood the approval queue.
    sentences = _split_sentences(full_text)
    for sent in sentences:
        if len(candidates) >= 5:
            break
        if not _has_decision_marker(sent):
            continue
        sent = sent.strip()
        if len(sent) < 12 or len(sent) > 600:
            continue

        # Phase 9 M9.4: Classify into workspace/agent scope
        memory_type, scope_type = _classify_memory_type(sent)

        # Determine scope_id based on scope_type
        if scope_type == "workspace":
            if not workspace_dir:
                # Skip workspace-scoped candidates if workspace_dir unknown
                continue
            scope_id = workspace_dir
        else:
            scope_id = agent_id

        candidates.append(
            MemoryCandidate(
                candidate_id=_new_id(),
                content=sent,
                memory_type=memory_type,
                scope_type=scope_type,
                scope_id=scope_id,
                source_type="compaction",
                status="pending",
                created_at=_now(),
                updated_at=_now(),
                stability=3,
                reuse_value=3,
                sensitivity=1,
                confidence=0.75,
                source_chain_json=json.dumps(
                    {
                        "source": "session_compaction",
                        "chat_id": chat_id,
                        "agent_id": agent_id,
                        "session_id": session_id,
                        "channel": channel,
                        "workspace_dir": workspace_dir,
                    }
                ),
                source_message_ids=",".join(msg_ids) if msg_ids else None,
                source_session_id=session_id,
                created_by_agent_id=agent_id,
                created_from_chat_id=chat_id,
                created_from_channel=channel,
            )
        )

    return candidates[:5]  # Cap at 5 total


# ====================================================================
# Source 2: TaskState pruning
# ====================================================================


def extract_from_task_state(
    task_state: Any,
    *,
    chat_id: str,
    agent_id: str,
    channel: str | None = None,
    max_facts: int = 5,
    source_priority: list[str] | None = None,
) -> list[MemoryCandidate]:
    """Promote stable facts from TaskState before they get pruned.

    ``task_state`` is the :class:`mini_claw.agent.task_state.TaskState`
    instance. We scan ``key_facts`` and select up to ``max_facts``
    decision-shaped entries. Pinned facts (if TaskState exposes a
    ``pinned`` set) are preferred.

    Phase 9 ac-4: source_priority param filters which sources are allowed.
    If source_priority is provided and 'task_state' is not in the list,
    returns empty list (no extraction).
    """
    if task_state is None:
        return []

    # Phase 9 ac-4: Check source_priority filter
    if source_priority is not None and "task_state" not in source_priority:
        return []
    facts: list[str] = list(getattr(task_state, "key_facts", None) or [])
    if not facts:
        return []

    pinned_set = set(getattr(task_state, "pinned_facts", None) or [])

    def _rank(text: str) -> tuple[int, int]:
        # Pinned first, then decision-shaped, then by length (shorter is sharper)
        return (
            0 if text in pinned_set else 1,
            0 if _has_decision_marker(text) else 1,
        )

    facts.sort(key=_rank)

    chosen: list[MemoryCandidate] = []
    for fact in facts:
        if len(chosen) >= max_facts:
            break
        if not isinstance(fact, str):
            continue
        text = fact.strip()
        if len(text) < 10 or len(text) > 600:
            continue
        if not (_has_decision_marker(text) or text in pinned_set):
            continue

        chosen.append(
            MemoryCandidate(
                candidate_id=_new_id(),
                content=text,
                memory_type="task_fact",
                scope_type="agent",
                scope_id=agent_id,
                source_type="task_state",
                status="pending",
                created_at=_now(),
                updated_at=_now(),
                stability=4 if text in pinned_set else 3,
                reuse_value=3,
                sensitivity=1,
                confidence=0.8 if text in pinned_set else 0.7,
                source_chain_json=json.dumps(
                    {
                        "source": "task_state",
                        "chat_id": chat_id,
                        "agent_id": agent_id,
                        "pinned": text in pinned_set,
                    }
                ),
                created_by_agent_id=agent_id,
                created_from_chat_id=chat_id,
                created_from_channel=channel,
            )
        )
    return chosen


# ====================================================================
# Source 3: WorkflowMerger.final_summary
# ====================================================================


def extract_from_workflow_merger(
    merged_result: dict[str, Any],
    *,
    workflow_id: str,
    chat_id: str,
    agent_id: str,
    channel: str | None = None,
    workspace_dir: str | None = None,
    workflow_intent: str | None = None,
    source_priority: list[str] | None = None,
) -> list[MemoryCandidate]:
    """Promote workflow ``key_findings`` to candidates.

    Only items that contain decision-shaped wording are emitted; ad-hoc
    "fixed bug X" findings are skipped.

    Phase 9 P0.1: workspace_dir param added; scope_id now uses workspace_dir
    for true workspace isolation (previously incorrectly used agent_id).

    Phase 9 WM-1: workflow_intent param added to capture workflow objective
    in source_chain_json for better memory context.

    Phase 9 WM-2: Map workflow spec.intent (coding/security/test) to workspace
    memory types (module_boundary/security_rule/implementation_note).

    Phase 9 WM-4: workspace_dir fallback to agent_id for backward compatibility.
    Workflow-derived memory is always workspace-scoped, but scope_id falls back
    to agent_id when workspace_dir is unavailable.

    Phase 9 ac-4: source_priority param filters which sources are allowed.
    If source_priority is provided and 'workflow' is not in the list,
    returns empty list (no extraction).
    """
    if not isinstance(merged_result, dict):
        return []

    # Phase 9 ac-4: Check source_priority filter
    if source_priority is not None and "workflow" not in source_priority:
        return []

    # Phase 9 WM-2: Determine workflow type and map to appropriate memory type
    workflow_type = _infer_workflow_type(workflow_intent)
    memory_type = _map_workflow_memory_type("key_findings", "workflow_finding", workflow_type)

    candidates: list[MemoryCandidate] = []
    items = merged_result.get("key_findings") or []
    if not isinstance(items, list):
        return candidates

    for raw in items:
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if len(text) < 12 or len(text) > 600:
            continue
        if not _has_decision_marker(text):
            continue
        candidates.append(
            MemoryCandidate(
                candidate_id=_new_id(),
                content=text,
                memory_type=memory_type,
                scope_type="workspace",
                scope_id=str(workspace_dir) if workspace_dir else agent_id,
                source_type="workflow",
                status="pending",
                created_at=_now(),
                updated_at=_now(),
                stability=4,  # workflow-derived facts tend to be vetted
                reuse_value=3,
                sensitivity=1,
                confidence=0.8,
                source_chain_json=json.dumps(
                    {
                        "source": "workflow",
                        "workflow_id": workflow_id,
                        "workflow_intent": workflow_intent,
                        "workflow_type": workflow_type,
                        "field": "key_findings",
                        "chat_id": chat_id,
                        "agent_id": agent_id,
                        "workspace_dir": str(workspace_dir) if workspace_dir else None,
                    }
                ),
                source_workflow_id=workflow_id,
                created_by_agent_id=agent_id,
                created_from_chat_id=chat_id,
                created_from_channel=channel,
            )
        )
        if len(candidates) >= 5:
            return candidates
    return candidates


# ====================================================================
# Helpers
# ====================================================================


_SENT_SPLIT_RE = re.compile(r"(?<=[。.!?！？])\s+|\n+")


def _split_sentences(text: str) -> list[str]:
    if not text:
        return []
    parts = _SENT_SPLIT_RE.split(text)
    return [p.strip() for p in parts if p and p.strip()]


def _infer_workflow_type(workflow_intent: str | None) -> str:
    """Phase 9 WM-2: Infer workflow type from workflow_intent string.

    workflow_intent format: "workflow_name: reason (task: user_task)"
    Extract the workflow_name to determine if it's coding/security/test focused.

    Returns: "coding", "security", "test", or "generic"
    """
    if not workflow_intent:
        return "generic"

    intent_lower = workflow_intent.lower()

    # Extract workflow name (before first colon)
    workflow_name = workflow_intent.split(":")[0].strip().lower()

    # Direct workflow name mapping
    if "security" in workflow_name or "security_review" in workflow_name:
        return "security"
    if "test" in workflow_name or "debug" in workflow_name or "fix" in workflow_name:
        return "test"
    if "code_review" in workflow_name or "migration" in workflow_name:
        return "coding"

    # Fallback: check full intent string for keywords
    if any(kw in intent_lower for kw in ["security", "permission", "audit", "vulnerability"]):
        return "security"
    if any(kw in intent_lower for kw in ["test", "verify", "debug", "fix", "bug"]):
        return "test"
    if any(kw in intent_lower for kw in ["code", "implement", "refactor", "migration"]):
        return "coding"

    return "generic"


def _map_workflow_memory_type(field: str, base_type: str, workflow_type: str) -> str:
    """Phase 9 WM-2: Map workflow field + type to specific workspace memory type.

    Mapping specification:
    - coding workflows → module_boundary (key_findings), implementation_note (others)
    - security workflows → security_rule (all fields)
    - test workflows → bug_root_cause (remaining_risks), implementation_note (others)
    - generic workflows → use base_type (workflow_finding, constraint, operational_rule)

    Args:
        field: "key_findings", "remaining_risks", or "recommended_next_steps"
        base_type: Original generic type (workflow_finding, constraint, operational_rule)
        workflow_type: Inferred type (coding, security, test, generic)

    Returns:
        Specific workspace memory type from WORKSPACE_MEMORY_TYPES
    """
    # Security workflows: all findings become security_rule
    if workflow_type == "security":
        return "security_rule"

    # Test/debug workflows
    if workflow_type == "test":
        if field == "remaining_risks":
            return "bug_root_cause"
        return "implementation_note"

    # Coding workflows (code_review, migration)
    if workflow_type == "coding":
        if field == "key_findings":
            return "module_boundary"
        return "implementation_note"

    # Generic workflows: use base_type
    return base_type


# ====================================================================
# Phase 9 M9.4: Agent summary extractor - DEPRECATED
# ====================================================================
# ac-1: This extractor is deprecated and disabled. Auto memory candidates
# should only come from structured sources (tool calls, workflow results,
# task state, session compaction with decision markers), NOT from natural
# language summaries like run.final_answer.
#
# Structured sources:
# - extract_from_session_compaction: messages with decision markers
# - extract_from_task_state: TaskState.key_facts (structured facts)
# - extract_from_workflow_merger: workflow key_findings (structured output)
#
# Natural language summaries lack the structure and auditability required
# for reliable auto memory extraction.


def extract_from_agent_summary(
    summary_text: str,
    agent_id: str,
    chat_id: str,
    channel_name: str = "legacy",
    workspace_dir: str | None = None,
    source_priority: list[str] | None = None,
) -> list[MemoryCandidate]:
    """Extract memory candidates from agent-generated summary.

    **DEPRECATED (ac-1)**: This function is disabled. Auto memory candidates
    should only come from structured sources (tool calls, workflow results,
    task state, session compaction), not from natural language summaries.

    Args:
        summary_text: The agent's summary/reflection text
        agent_id: Agent identifier
        chat_id: Chat identifier
        channel_name: Channel name for isolation
        workspace_dir: Optional workspace directory
        source_priority: Optional list of allowed source types (ignored)

    Returns:
        Empty list (extraction disabled per Phase 9 structured-sources-only requirement)
    """
    # ac-1: Disabled - natural language summaries are not structured sources
    return []
