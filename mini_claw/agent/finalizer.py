"""Phase 10 M10.2: Finalizer.

Generates the final user-facing reply *independently* from the
Reflection JSON. Per P14 in plans/ReAct.md, the Reflection JSON is for
internal state — never echoed to the user.

The default implementation is deterministic: it composes a short
message from the decision, observation, and Reflection's
``final_response_hint``. Callers may swap in an LLM-driven Finalizer
later; the contract here is "no tools, no chain-of-thought".
"""

from __future__ import annotations

from mini_claw.agent.react_models import (
    ReActDecision,
    ReActObservation,
    ReflectionResult,
)


def finalize_response(
    *,
    decision: ReActDecision,
    observation: ReActObservation,
    reflection: ReflectionResult | None = None,
    raw_final_text: str | None = None,
) -> str:
    """Compose the final user-visible response.

    ``raw_final_text`` is the LLM's own final reply (when present);
    we prefer it for ``finalize`` decisions because it carries the
    actual answer the user asked for.
    """
    hint = (decision.final_response_hint or "").strip()
    if reflection and not hint:
        hint = (reflection.final_response_hint or "").strip()

    if decision.action == "finalize":
        if raw_final_text:
            return raw_final_text.strip()
        if observation.observation_type == "direct_answer" and observation.summary:
            return observation.summary.strip()
        if hint:
            return hint
        return "任务已完成。"

    if decision.action == "block":
        if hint:
            return hint
        return f"无法继续：{decision.reason}。"

    if decision.action == "suspend":
        return hint or "操作需要进一步确认，请审批后我会继续。"

    if decision.action == "fail":
        return hint or "任务未能完成，请稍后再试或换一种方式。"

    # continue: should not normally surface to user
    return hint or "继续处理中…"
