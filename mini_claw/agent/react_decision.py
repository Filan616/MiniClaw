"""Phase 10 M10.2: DecisionController.

Maps an Observation+Reflection pair to a final ``ReActDecision``. Hard
safety boundaries (PermissionGate deny, ChainDetector block, approval
rejected) take precedence over the Reflection's recommendation. This is
the contract from P11 in plans/ReAct.md.
"""

from __future__ import annotations

from mini_claw.agent.react_models import (
    ReActDecision,
    ReActObservation,
    ReflectionResult,
)


def decide_from_reflection(
    observation: ReActObservation,
    reflection: ReflectionResult,
) -> ReActDecision:
    obs_type = observation.observation_type

    # Hard boundaries first — Reflection cannot override these.
    if obs_type == "permission_denied":
        return ReActDecision(
            action="block",
            reason="PermissionGate denied",
            final_response_hint=reflection.final_response_hint
            or "该操作被权限策略拒绝。",
        )
    if obs_type == "chain_blocked":
        return ReActDecision(
            action="block",
            reason="ChainDetector blocked",
            final_response_hint=reflection.final_response_hint
            or "该操作链被安全策略阻断。",
        )
    if obs_type == "approval_rejected":
        return ReActDecision(
            action="block",
            reason="User rejected approval",
            final_response_hint=reflection.final_response_hint
            or "用户已拒绝该操作。",
        )
    if obs_type == "approval_required":
        return ReActDecision(
            action="suspend",
            reason="approval required",
            final_response_hint=reflection.final_response_hint,
        )
    if obs_type == "max_iteration":
        return ReActDecision(
            action="fail",
            reason="max iterations reached",
            final_response_hint=reflection.final_response_hint
            or "达到最大迭代次数仍未收敛。",
        )

    decision = reflection.decision
    if decision == "done":
        return ReActDecision(
            action="finalize",
            reason="reflection: done",
            final_response_hint=reflection.final_response_hint,
        )
    if decision == "continue":
        return ReActDecision(
            action="continue",
            reason="reflection: continue",
            final_response_hint=reflection.final_response_hint,
        )
    if decision == "suspended":
        return ReActDecision(
            action="suspend",
            reason="reflection: needs approval",
            final_response_hint=reflection.final_response_hint,
        )
    if decision == "blocked":
        return ReActDecision(
            action="block",
            reason=f"reflection: {reflection.safety_assessment}",
            final_response_hint=reflection.final_response_hint,
        )
    if decision == "failed":
        return ReActDecision(
            action="fail",
            reason=f"reflection: {reflection.safety_assessment}",
            final_response_hint=reflection.final_response_hint,
        )

    return ReActDecision(action="continue", reason="default continue")
