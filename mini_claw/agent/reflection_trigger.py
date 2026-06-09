"""Phase 10 M10.2: ReActPolicy + Reflection trigger.

``should_reflect`` is the *single* entry point that decides whether to
run a Reflection cycle for the current iteration. Centralizing the
conditions here is the contract from P9 in plans/ReAct.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mini_claw.agent.react_models import ReActObservation, ReflectionTriggerResult


# Priority order: anything with a higher priority short-circuits the
# trigger result's ``priority`` field.
_REASON_PRIORITY: tuple[str, ...] = (
    "approval_rejected",
    "chain_blocked",
    "permission_denied",
    "tool_error",
    "hallucination_guard",
    "repeated_tool_call",
    "iteration_threshold",
    "empty_search_result",
    "every_iteration",
    "before_finalize",
)

_TERMINAL_REASONS: frozenset[str] = frozenset(
    {"approval_rejected", "chain_blocked", "permission_denied"}
)


@dataclass(slots=True)
class ReActPolicy:
    """Resolved per-iteration ReAct policy.

    Built by :func:`mini_claw.agent.react_policy.resolve_react_policy`.
    Defaults match Phase 10 ``controlled`` mode: every-iteration
    Reflection is OFF, error/permission/etc. triggers are ON,
    ``before_finalize`` deterministic-first is ON.
    """

    mode: str = "controlled"
    reflect_every_iteration: bool = False
    reflect_before_finalize: bool = True
    reflect_before_finalize_mode: str = "deterministic_first"

    reflect_on_tool_error: bool = True
    reflect_on_permission_denied: bool = True
    reflect_on_approval_rejected: bool = True
    reflect_on_chain_blocked: bool = True
    reflect_on_repeated_tool_call: bool = True
    reflect_on_hallucination_guard: bool = True
    reflect_on_empty_rag_result: bool = True

    reflect_on_iteration_threshold: int | None = 7
    reflect_on_iteration_threshold_ratio: float | None = 0.7

    reflection_timeout_sec: int = 15
    max_reflection_chars: int = 4000
    max_observation_chars: int = 2500
    store_reflection: bool = True
    finalizer_enabled: bool = True
    finalizer_timeout_sec: int = 20

    extra: dict[str, Any] = field(default_factory=dict)

    def apply_node_override(self, node_policy: "ReActPolicy") -> None:
        for fld in (
            "mode",
            "reflect_every_iteration",
            "reflect_before_finalize",
        ):
            value = getattr(node_policy, fld, None)
            if value is not None:
                setattr(self, fld, value)

    def apply_high_risk_defaults(self) -> None:
        self.mode = "strict"
        self.reflect_every_iteration = True
        self.reflect_before_finalize = True


def compute_iteration_threshold(
    *,
    max_iterations: int,
    absolute: int | None,
    ratio: float | None,
) -> int | None:
    """Combine absolute and ratio thresholds into a single integer."""
    candidates: list[int] = []
    if absolute is not None:
        candidates.append(int(absolute))
    if ratio is not None:
        candidates.append(max(1, int(max_iterations * ratio)))
    if not candidates:
        return None
    return min(candidates)


def _select_priority(reasons: list[str]) -> str:
    if not reasons:
        return ""
    by_index = {r: i for i, r in enumerate(_REASON_PRIORITY)}
    reasons_sorted = sorted(reasons, key=lambda r: by_index.get(r, 999))
    return reasons_sorted[0]


def _has_terminal(reasons: list[str]) -> bool:
    return any(r in _TERMINAL_REASONS for r in reasons)


def should_reflect(
    observation: ReActObservation,
    *,
    iteration: int,
    max_iterations: int,
    policy: ReActPolicy,
    repeated_tool_call_detected: bool = False,
    hallucination_guard_triggered: bool = False,
) -> ReflectionTriggerResult:
    """Single entry point for whether the loop should reflect this iteration."""
    reasons: list[str] = []

    if policy.reflect_every_iteration:
        reasons.append("every_iteration")

    obs_type = observation.observation_type

    if obs_type == "permission_denied" and policy.reflect_on_permission_denied:
        reasons.append("permission_denied")
    if obs_type == "chain_blocked" and policy.reflect_on_chain_blocked:
        reasons.append("chain_blocked")
    if obs_type == "approval_rejected" and policy.reflect_on_approval_rejected:
        reasons.append("approval_rejected")
    if obs_type == "tool_error" and policy.reflect_on_tool_error:
        reasons.append("tool_error")
    if obs_type == "empty_search_result" and policy.reflect_on_empty_rag_result:
        reasons.append("empty_search_result")

    if repeated_tool_call_detected and policy.reflect_on_repeated_tool_call:
        reasons.append("repeated_tool_call")
    if hallucination_guard_triggered and policy.reflect_on_hallucination_guard:
        reasons.append("hallucination_guard")

    threshold = compute_iteration_threshold(
        max_iterations=max_iterations,
        absolute=policy.reflect_on_iteration_threshold,
        ratio=policy.reflect_on_iteration_threshold_ratio,
    )
    if threshold is not None and iteration >= threshold:
        reasons.append("iteration_threshold")

    if obs_type == "direct_answer" and policy.reflect_before_finalize:
        reasons.append("before_finalize")

    # Dedupe while preserving first occurrence.
    seen: set[str] = set()
    deduped: list[str] = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            deduped.append(r)

    return ReflectionTriggerResult(
        should_reflect=bool(deduped),
        reasons=deduped,
        priority=_select_priority(deduped),
        terminal=_has_terminal(deduped),
    )
