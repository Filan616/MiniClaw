"""Phase 10 ReAct data structures.

Pure dataclass definitions only — no I/O, no DB, no LLM. Imports here
must remain cheap so other modules can pull these types without picking
up storage or provider dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


ActionPhase = Literal[
    "tool_call",
    "direct_answer",
    "permission_denied",
    "approval_required",
    "approval_rejected",
    "chain_blocked",
    "tool_error",
    "max_iteration",
]

ObservationType = Literal[
    "tool_success",
    "tool_error",
    "permission_denied",
    "approval_required",
    "approval_rejected",
    "chain_blocked",
    "direct_answer",
    "empty_search_result",
    "max_iteration",
]

UpdateEventType = Literal[
    "action_planned",
    "observation_summary",
    "reflection_summary",
    "decision_summary",
]

VisibleLevel = Literal["normal", "verbose", "debug"]
SendStatus = Literal["pending", "sent", "failed", "skipped"]
ReActMode = Literal["controlled", "strict"]
DecisionAction = Literal["continue", "finalize", "block", "suspend", "fail"]
StepDecision = Literal["continue", "finalize", "blocked", "suspended", "failed"]
StepStatus = Literal[
    "pending",
    "running",
    "observed",
    "reflected",
    "completed",
    "failed",
    "suspended",
]
GoalStatus = Literal[
    "not_started",
    "in_progress",
    "done",
    "blocked",
    "failed",
    "needs_approval",
]
SafetyAssessment = Literal[
    "safe_to_continue",
    "blocked_by_permission",
    "blocked_by_user_rejection",
    "blocked_by_policy",
    "needs_user_input",
    "failed_unrecoverable",
]
ReflectionDecision = Literal["continue", "done", "blocked", "suspended", "failed"]


@dataclass(slots=True)
class ReActObservation:
    """What the loop observed after the most recent Action."""

    observation_type: ObservationType
    tool_name: str | None = None
    summary: str = ""
    raw_result_ref: str | None = None
    error: str | None = None
    permission_action: str | None = None
    permission_reason: str | None = None
    artifacts: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ReflectionResult:
    """Structured Reflection output (validated against ReflectionSchema)."""

    observation_summary: str
    goal_status: GoalStatus
    completed_requirements: list[str]
    remaining_requirements: list[str]
    safety_assessment: SafetyAssessment
    safe_next_action: str
    forbidden_next_actions: list[str]
    decision: ReflectionDecision
    final_response_hint: str
    confidence: float
    fallback_used: bool = False
    parse_failed: bool = False
    timed_out: bool = False
    raw_text: str | None = None


@dataclass(slots=True)
class ReflectionTriggerResult:
    """Outcome of should_reflect() for a single iteration."""

    should_reflect: bool
    reasons: list[str]
    priority: str = ""
    terminal: bool = False


@dataclass(slots=True)
class ReActDecision:
    """Final decision for an iteration after Observation+Reflection."""

    action: DecisionAction
    reason: str
    final_response_hint: str = ""


@dataclass(slots=True)
class ReActUserUpdate:
    """A user-visible process message tied to a single ReActStep.

    Stored in ``react_user_updates`` and (for actually-sent updates)
    mirrored to ``messages`` with ``message_kind='react_update'``.
    """

    update_id: str
    step_id: str
    run_id: str
    chat_id: str
    agent_id: str
    event_type: UpdateEventType
    text: str
    text_hash: str
    visible_level: VisibleLevel = "normal"
    is_important: bool = False
    send_status: SendStatus = "pending"
    channel_message_id: str | None = None
    error: str | None = None
    created_at: int = 0
    sent_at: int | None = None


@dataclass(slots=True)
class ReActStep:
    """A single ReAct iteration's state record.

    NOT a fact source for tools/security — those remain ``tool_calls``
    and ``security_audit``. ReActStep records *state and decisions*.
    """

    step_id: str
    run_id: str
    chat_id: str
    agent_id: str
    iteration: int
    action_phase: ActionPhase
    assistant_content_hash: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_refs: list[dict[str, Any]] = field(default_factory=list)
    permission_decisions: list[dict[str, Any]] = field(default_factory=list)
    observation: dict[str, Any] = field(default_factory=dict)
    reflection: dict[str, Any] = field(default_factory=dict)
    reflection_triggered: bool = False
    reflection_reasons: list[str] = field(default_factory=list)
    user_updates: list[dict[str, Any]] = field(default_factory=list)
    decision: StepDecision = "continue"
    status: StepStatus = "pending"
    created_at: int = 0
    updated_at: int = 0
