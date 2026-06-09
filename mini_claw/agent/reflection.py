"""Phase 10 M10.2: ReflectionEngine + deterministic fallback.

The engine asks the provider for a structured Reflection JSON, parses
it against the schema, and falls back to a deterministic mapping if
parsing fails or the provider times out (P13 in plans/ReAct.md).

Reflection is *not* allowed to override hard safety boundaries — that
job belongs to :mod:`mini_claw.agent.react_decision`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from mini_claw.agent.react_models import ReActObservation, ReflectionResult

logger = logging.getLogger(__name__)


class ReflectionSchema(BaseModel):
    """Pydantic schema for the LLM's structured Reflection output.

    Phase 10 §4.4 contract — used for documentation and runtime validation
    when callers want strict typing. ``parse_reflection_json`` accepts any
    JSON shape and coerces to ``ReflectionResult`` for the runtime, but
    callers who need stricter validation (eg. custom Reflection runners)
    can validate against this model first.
    """

    observation_summary: str = ""
    goal_status: str = "in_progress"
    completed_requirements: list[str] = Field(default_factory=list)
    remaining_requirements: list[str] = Field(default_factory=list)
    safety_assessment: str = "safe_to_continue"
    safe_next_action: str = ""
    forbidden_next_actions: list[str] = Field(default_factory=list)
    decision: str = "continue"
    final_response_hint: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


_VALID_GOAL_STATUS = {
    "not_started",
    "in_progress",
    "done",
    "blocked",
    "failed",
    "needs_approval",
}
_VALID_SAFETY = {
    "safe_to_continue",
    "blocked_by_permission",
    "blocked_by_user_rejection",
    "blocked_by_policy",
    "needs_user_input",
    "failed_unrecoverable",
}
_VALID_DECISION = {"continue", "done", "blocked", "suspended", "failed"}


REFLECTION_SYSTEM_PROMPT = """\
你是 MiniClaw 的 ReAct 状态评估器。

你不会执行工具。
你不会请求绕过权限。
你只根据当前目标、上一步 action、observation 和安全决策，输出结构化 JSON。

请只输出 JSON，字段如下：

{
  "observation_summary": "...",
  "goal_status": "not_started | in_progress | done | blocked | failed | needs_approval",
  "completed_requirements": [],
  "remaining_requirements": [],
  "safety_assessment": "safe_to_continue | blocked_by_permission | blocked_by_user_rejection | blocked_by_policy | needs_user_input | failed_unrecoverable",
  "safe_next_action": "...",
  "forbidden_next_actions": [],
  "decision": "continue | done | blocked | suspended | failed",
  "final_response_hint": "...",
  "confidence": 0.0
}

硬性安全规则：
1. PermissionGate deny 是硬边界，不得建议绕过。
2. ChainDetector block 是硬边界，不得建议换工具、换路径或拆分步骤继续。
3. Approval rejected 是用户明确拒绝，不得继续尝试同类操作。
4. 敏感路径被拒绝时，只能解释原因或请求用户提供非敏感替代文件。
5. 不得建议开启 bypass，除非用户明确主动要求并走现有 /bypass 流程。
6. 不得建议使用 run_shell 绕过 read_file/write_file 的拒绝。
7. 不得编造工具没有返回的信息。
8. 如果目标已经完成，decision 必须是 done。
9. 如果无法安全继续，decision 必须是 blocked 或 failed。
10. 只输出 JSON，不输出其他文本。
"""


def build_reflection_user_prompt(
    *,
    original_goal_summary: str,
    iteration: int,
    max_iterations: int,
    trigger_reasons: list[str],
    observation: ReActObservation,
    permission_summary: str = "",
) -> str:
    return (
        "原始用户目标：\n"
        f"{original_goal_summary or '(empty)'}\n\n"
        f"当前迭代：{iteration}/{max_iterations}\n\n"
        f"触发原因：{', '.join(trigger_reasons) or '(none)'}\n\n"
        "上一步 Observation：\n"
        f"{observation.summary or '(empty)'}\n\n"
        f"Observation 类型：{observation.observation_type}\n"
        + (f"Observation 错误：{observation.error}\n" if observation.error else "")
        + (
            f"Permission 决策：{observation.permission_action} — {observation.permission_reason}\n"
            if observation.permission_action
            else ""
        )
        + (f"\nPermission / Safety Context:\n{permission_summary}\n" if permission_summary else "")
    )


def parse_reflection_json(raw: str) -> ReflectionResult | None:
    """Best-effort JSON parser. Returns None on irrecoverable failure."""
    if not raw:
        return None
    text = raw.strip()
    # Strip ```json fences if present.
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence_match:
        text = fence_match.group(1).strip()
    # Find the first JSON object substring.
    if not text.startswith("{"):
        brace = text.find("{")
        if brace == -1:
            return None
        text = text[brace:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    goal_status = data.get("goal_status", "in_progress")
    if goal_status not in _VALID_GOAL_STATUS:
        goal_status = "in_progress"

    safety = data.get("safety_assessment", "safe_to_continue")
    if safety not in _VALID_SAFETY:
        safety = "safe_to_continue"

    decision = data.get("decision", "continue")
    if decision not in _VALID_DECISION:
        decision = "continue"

    try:
        confidence = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    def _slist(key: str) -> list[str]:
        v = data.get(key, [])
        return [str(x) for x in v if isinstance(x, (str, int, float))] if isinstance(v, list) else []

    return ReflectionResult(
        observation_summary=str(data.get("observation_summary", "")),
        goal_status=goal_status,  # type: ignore[arg-type]
        completed_requirements=_slist("completed_requirements"),
        remaining_requirements=_slist("remaining_requirements"),
        safety_assessment=safety,  # type: ignore[arg-type]
        safe_next_action=str(data.get("safe_next_action", "")),
        forbidden_next_actions=_slist("forbidden_next_actions"),
        decision=decision,  # type: ignore[arg-type]
        final_response_hint=str(data.get("final_response_hint", "")),
        confidence=confidence,
    )


def fallback_reflection(observation: ReActObservation) -> ReflectionResult:
    """Deterministic fallback when LLM Reflection fails or times out.

    See P13 in plans/ReAct.md — fallback is mandatory.
    """
    obs_type = observation.observation_type
    if obs_type == "permission_denied":
        return ReflectionResult(
            observation_summary=observation.summary or "permission denied",
            goal_status="blocked",
            completed_requirements=[],
            remaining_requirements=[],
            safety_assessment="blocked_by_permission",
            safe_next_action="向用户解释权限被拒绝，请求改用更低权限的方案。",
            forbidden_next_actions=["bypass permission", "retry with elevated privileges"],
            decision="blocked",
            final_response_hint="该操作被权限策略拒绝，无法继续。",
            confidence=0.95,
            fallback_used=True,
        )
    if obs_type == "chain_blocked":
        return ReflectionResult(
            observation_summary=observation.summary or "chain blocked",
            goal_status="blocked",
            completed_requirements=[],
            remaining_requirements=[],
            safety_assessment="blocked_by_policy",
            safe_next_action="停止当前工具链，向用户说明被链式攻击检测器阻断。",
            forbidden_next_actions=["retry same chain", "split into smaller steps to evade"],
            decision="blocked",
            final_response_hint="该工具链被安全策略阻断。",
            confidence=0.95,
            fallback_used=True,
        )
    if obs_type == "approval_rejected":
        return ReflectionResult(
            observation_summary=observation.summary or "approval rejected",
            goal_status="blocked",
            completed_requirements=[],
            remaining_requirements=[],
            safety_assessment="blocked_by_user_rejection",
            safe_next_action="尊重用户拒绝，停止该方向的尝试。",
            forbidden_next_actions=["retry similar approval", "rephrase same request"],
            decision="blocked",
            final_response_hint="用户已拒绝该操作。",
            confidence=0.95,
            fallback_used=True,
        )
    if obs_type == "approval_required":
        return ReflectionResult(
            observation_summary=observation.summary or "approval required",
            goal_status="needs_approval",
            completed_requirements=[],
            remaining_requirements=[],
            safety_assessment="needs_user_input",
            safe_next_action="等待用户审批。",
            forbidden_next_actions=[],
            decision="suspended",
            final_response_hint="该操作需要用户审批。",
            confidence=0.9,
            fallback_used=True,
        )
    if obs_type == "tool_error":
        return ReflectionResult(
            observation_summary=observation.summary or "tool error",
            goal_status="in_progress",
            completed_requirements=[],
            remaining_requirements=[],
            safety_assessment="safe_to_continue",
            safe_next_action="检查参数或换一个工具继续尝试。",
            forbidden_next_actions=[],
            decision="continue",
            final_response_hint="",
            confidence=0.6,
            fallback_used=True,
        )
    if obs_type == "direct_answer":
        return ReflectionResult(
            observation_summary=observation.summary or "direct answer",
            goal_status="done",
            completed_requirements=[],
            remaining_requirements=[],
            safety_assessment="safe_to_continue",
            safe_next_action="",
            forbidden_next_actions=[],
            decision="done",
            final_response_hint=observation.summary,
            confidence=0.7,
            fallback_used=True,
        )
    # Default: keep going.
    return ReflectionResult(
        observation_summary=observation.summary or "",
        goal_status="in_progress",
        completed_requirements=[],
        remaining_requirements=[],
        safety_assessment="safe_to_continue",
        safe_next_action="继续推进任务。",
        forbidden_next_actions=[],
        decision="continue",
        final_response_hint="",
        confidence=0.5,
        fallback_used=True,
    )


async def run_reflection(
    *,
    provider: Any,
    observation: ReActObservation,
    original_goal_summary: str,
    iteration: int,
    max_iterations: int,
    trigger_reasons: list[str],
    permission_summary: str = "",
    timeout_sec: int = 15,
    max_reflection_chars: int = 4000,
) -> ReflectionResult:
    """Run the LLM Reflection cycle. Always returns a ReflectionResult.

    Phase 10 §6 — ``max_reflection_chars`` caps both the prompt input
    and the parsed ``raw_text`` we keep on the result so reflection
    never blows up the audit trail or downstream rendering.
    """
    user_prompt = build_reflection_user_prompt(
        original_goal_summary=original_goal_summary,
        iteration=iteration,
        max_iterations=max_iterations,
        trigger_reasons=trigger_reasons,
        observation=observation,
        permission_summary=permission_summary,
    )
    if max_reflection_chars and len(user_prompt) > max_reflection_chars:
        user_prompt = user_prompt[: max_reflection_chars - 3] + "..."
    messages = [
        {"role": "system", "content": REFLECTION_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    if provider is None:
        result = fallback_reflection(observation)
        result.parse_failed = True
        return result

    try:
        coro = provider.chat(messages=messages, tools=None, stream=False)
        response = await asyncio.wait_for(coro, timeout=timeout_sec)
    except asyncio.TimeoutError:
        logger.warning("reflection timed out after %ss", timeout_sec)
        result = fallback_reflection(observation)
        result.parse_failed = True
        result.timed_out = True
        result.raw_text = f"<timeout after {timeout_sec}s>"
        return result
    except Exception:
        logger.warning("reflection provider call failed", exc_info=True)
        result = fallback_reflection(observation)
        result.parse_failed = True
        return result

    raw = getattr(response, "text", None) or ""
    parsed = parse_reflection_json(raw)
    keep = max_reflection_chars if max_reflection_chars else 500
    if parsed is None:
        result = fallback_reflection(observation)
        result.parse_failed = True
        result.raw_text = raw[:keep]
        return result
    parsed.raw_text = raw[:keep]
    return parsed
