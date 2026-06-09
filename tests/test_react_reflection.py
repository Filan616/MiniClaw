"""Phase 10 M10.2: should_reflect / Reflection / DecisionController tests."""

import json

from mini_claw.agent.observation import (
    build_approval_rejected_observation,
    build_approval_required_observation,
    build_chain_blocked_observation,
    build_direct_answer_observation,
    build_empty_search_result_observation,
    build_max_iteration_observation,
    build_permission_denied_observation,
    build_tool_error_observation,
    build_tool_success_observation,
)
from mini_claw.agent.react_decision import decide_from_reflection
from mini_claw.agent.react_models import ReflectionResult
from mini_claw.agent.reflection import (
    fallback_reflection,
    parse_reflection_json,
)
from mini_claw.agent.reflection_trigger import (
    ReActPolicy,
    compute_iteration_threshold,
    should_reflect,
)


# ---------------------------------------------------------------------------
# compute_iteration_threshold
# ---------------------------------------------------------------------------


def test_threshold_combines_absolute_and_ratio_via_min():
    assert compute_iteration_threshold(max_iterations=10, absolute=7, ratio=0.5) == 5


def test_threshold_returns_none_when_both_disabled():
    assert compute_iteration_threshold(max_iterations=10, absolute=None, ratio=None) is None


def test_threshold_only_absolute():
    assert compute_iteration_threshold(max_iterations=10, absolute=8, ratio=None) == 8


# ---------------------------------------------------------------------------
# should_reflect
# ---------------------------------------------------------------------------


def test_should_reflect_permission_denied_triggers_terminal():
    obs = build_permission_denied_observation("write_file", "denied path")
    res = should_reflect(obs, iteration=1, max_iterations=10, policy=ReActPolicy())
    assert res.should_reflect is True
    assert "permission_denied" in res.reasons
    assert res.terminal is True


def test_should_reflect_chain_blocked_triggers_terminal():
    obs = build_chain_blocked_observation("write_file", "chain blocked")
    res = should_reflect(obs, iteration=1, max_iterations=10, policy=ReActPolicy())
    assert "chain_blocked" in res.reasons
    assert res.terminal is True


def test_should_reflect_approval_rejected_terminal():
    obs = build_approval_rejected_observation("run_shell")
    res = should_reflect(obs, iteration=1, max_iterations=10, policy=ReActPolicy())
    assert "approval_rejected" in res.reasons
    assert res.terminal is True


def test_should_reflect_iteration_threshold_single_reason():
    """absolute+ratio 合并为 *一个* reason。"""
    obs = build_tool_success_observation("read_file", "ok")
    policy = ReActPolicy(
        reflect_on_iteration_threshold=7,
        reflect_on_iteration_threshold_ratio=0.5,
    )
    res = should_reflect(obs, iteration=8, max_iterations=10, policy=policy)
    assert res.reasons.count("iteration_threshold") == 1


def test_should_reflect_before_finalize_for_direct_answer():
    obs = build_direct_answer_observation("done")
    res = should_reflect(obs, iteration=2, max_iterations=10, policy=ReActPolicy())
    assert "before_finalize" in res.reasons


def test_should_reflect_disabled_when_all_off():
    obs = build_tool_success_observation("read_file", "ok")
    policy = ReActPolicy(
        reflect_every_iteration=False,
        reflect_before_finalize=False,
        reflect_on_tool_error=False,
        reflect_on_permission_denied=False,
        reflect_on_approval_rejected=False,
        reflect_on_chain_blocked=False,
        reflect_on_repeated_tool_call=False,
        reflect_on_hallucination_guard=False,
        reflect_on_empty_rag_result=False,
        reflect_on_iteration_threshold=None,
        reflect_on_iteration_threshold_ratio=None,
    )
    res = should_reflect(obs, iteration=1, max_iterations=10, policy=policy)
    assert res.should_reflect is False


def test_should_reflect_dedupes_reasons():
    obs = build_tool_error_observation("run_shell", "boom")
    policy = ReActPolicy(reflect_every_iteration=True)
    res = should_reflect(obs, iteration=8, max_iterations=10, policy=policy)
    # tool_error + every_iteration + iteration_threshold all fire — but each only once.
    assert len(res.reasons) == len(set(res.reasons))


# ---------------------------------------------------------------------------
# parse_reflection_json + fallback
# ---------------------------------------------------------------------------


def test_parse_reflection_full_object():
    raw = json.dumps(
        {
            "observation_summary": "ok",
            "goal_status": "in_progress",
            "completed_requirements": [],
            "remaining_requirements": ["x"],
            "safety_assessment": "safe_to_continue",
            "safe_next_action": "go",
            "forbidden_next_actions": [],
            "decision": "continue",
            "final_response_hint": "",
            "confidence": 0.8,
        }
    )
    parsed = parse_reflection_json(raw)
    assert isinstance(parsed, ReflectionResult)
    assert parsed.decision == "continue"
    assert parsed.confidence == 0.8


def test_parse_reflection_with_code_fence():
    raw = "```json\n" + json.dumps({"goal_status": "done", "decision": "done"}) + "\n```"
    parsed = parse_reflection_json(raw)
    assert parsed is not None
    assert parsed.decision == "done"


def test_parse_reflection_invalid_returns_none():
    assert parse_reflection_json("not json at all") is None
    assert parse_reflection_json("") is None


def test_fallback_permission_denied_returns_blocked():
    obs = build_permission_denied_observation("write_file", "x")
    r = fallback_reflection(obs)
    assert r.decision == "blocked"
    assert r.safety_assessment == "blocked_by_permission"
    assert r.fallback_used is True


def test_fallback_direct_answer_returns_done():
    obs = build_direct_answer_observation("hello")
    r = fallback_reflection(obs)
    assert r.decision == "done"
    assert r.fallback_used is True


def test_fallback_chain_blocked_terminal():
    r = fallback_reflection(build_chain_blocked_observation("x", "y"))
    assert r.decision == "blocked"
    assert r.safety_assessment == "blocked_by_policy"


# ---------------------------------------------------------------------------
# DecisionController hard boundaries
# ---------------------------------------------------------------------------


def _refl(decision: str, safety: str = "safe_to_continue") -> ReflectionResult:
    return ReflectionResult(
        observation_summary="",
        goal_status="in_progress",
        completed_requirements=[],
        remaining_requirements=[],
        safety_assessment=safety,  # type: ignore[arg-type]
        safe_next_action="",
        forbidden_next_actions=[],
        decision=decision,  # type: ignore[arg-type]
        final_response_hint="",
        confidence=0.5,
    )


def test_decision_permission_denied_overrides_continue():
    obs = build_permission_denied_observation("write_file", "blocked")
    decision = decide_from_reflection(obs, _refl("continue"))
    assert decision.action == "block"


def test_decision_chain_blocked_overrides_done():
    obs = build_chain_blocked_observation("write_file", "blocked")
    decision = decide_from_reflection(obs, _refl("done"))
    assert decision.action == "block"


def test_decision_approval_rejected_overrides_continue():
    obs = build_approval_rejected_observation("run_shell")
    decision = decide_from_reflection(obs, _refl("continue"))
    assert decision.action == "block"


def test_decision_approval_required_suspends():
    obs = build_approval_required_observation("write_file", "needs approval")
    decision = decide_from_reflection(obs, _refl("continue"))
    assert decision.action == "suspend"


def test_decision_max_iteration_fails():
    obs = build_max_iteration_observation()
    decision = decide_from_reflection(obs, _refl("continue"))
    assert decision.action == "fail"


def test_decision_done_finalizes_when_safe():
    obs = build_direct_answer_observation("done")
    decision = decide_from_reflection(obs, _refl("done"))
    assert decision.action == "finalize"


def test_decision_continue_when_safe_tool_success():
    obs = build_tool_success_observation("read_file", "ok")
    decision = decide_from_reflection(obs, _refl("continue"))
    assert decision.action == "continue"


def test_decision_failed_propagates():
    obs = build_tool_error_observation("run_shell", "boom")
    decision = decide_from_reflection(obs, _refl("failed", safety="failed_unrecoverable"))
    assert decision.action == "fail"
