"""Phase 10 M10.2: ObservationBuilder.

Translates the loop's per-iteration outcome into a uniform
``ReActObservation`` structure. Pure functions, no I/O.
"""

from __future__ import annotations

from typing import Any

from mini_claw.agent.react_models import ReActObservation


# Phase 10 §6: ``agent.react.max_observation_chars`` overrides this when
# the loop forwards it; the default keeps the legacy behavior.
DEFAULT_OBSERVATION_MAX_CHARS = 2500


def _truncate(text: str, n: int = 400) -> str:
    if not text:
        return ""
    return text if len(text) <= n else text[: n - 3] + "..."


def build_tool_success_observation(
    tool_name: str,
    result: Any,
    *,
    raw_result_ref: str | None = None,
    max_chars: int | None = None,
) -> ReActObservation:
    cap = max_chars if max_chars is not None else DEFAULT_OBSERVATION_MAX_CHARS
    summary = _truncate(str(result) if result is not None else "", n=cap)
    artifacts: dict[str, Any] = {}
    if isinstance(result, str) and (
        result.lstrip().startswith(("[]", "No results", "0 result"))
        or result.strip() == ""
    ):
        # Surface this for should_reflect's empty_search heuristic.
        artifacts["looks_empty"] = True
    return ReActObservation(
        observation_type="tool_success",
        tool_name=tool_name,
        summary=summary,
        raw_result_ref=raw_result_ref,
        artifacts=artifacts,
    )


def build_tool_error_observation(
    tool_name: str,
    error: str,
    *,
    raw_result_ref: str | None = None,
    max_chars: int | None = None,
) -> ReActObservation:
    cap = max_chars if max_chars is not None else DEFAULT_OBSERVATION_MAX_CHARS
    return ReActObservation(
        observation_type="tool_error",
        tool_name=tool_name,
        summary=_truncate(error, n=cap),
        error=_truncate(error, n=min(200, cap)),
        raw_result_ref=raw_result_ref,
    )


def build_permission_denied_observation(
    tool_name: str,
    reason: str,
    *,
    max_chars: int | None = None,
) -> ReActObservation:
    cap = max_chars if max_chars is not None else DEFAULT_OBSERVATION_MAX_CHARS
    summary_cap = min(160, cap)
    reason_cap = min(300, cap)
    return ReActObservation(
        observation_type="permission_denied",
        tool_name=tool_name,
        summary=f"PermissionGate denied {tool_name}: {_truncate(reason, n=summary_cap)}",
        permission_action="deny",
        permission_reason=_truncate(reason, n=reason_cap),
    )


def build_approval_required_observation(
    tool_name: str,
    reason: str,
    *,
    max_chars: int | None = None,
) -> ReActObservation:
    cap = max_chars if max_chars is not None else DEFAULT_OBSERVATION_MAX_CHARS
    summary_cap = min(160, cap)
    reason_cap = min(300, cap)
    return ReActObservation(
        observation_type="approval_required",
        tool_name=tool_name,
        summary=f"PermissionGate requires approval for {tool_name}: {_truncate(reason, n=summary_cap)}",
        permission_action="need_approval",
        permission_reason=_truncate(reason, n=reason_cap),
    )


def build_approval_rejected_observation(tool_name: str | None = None) -> ReActObservation:
    return ReActObservation(
        observation_type="approval_rejected",
        tool_name=tool_name,
        summary="User rejected the approval request.",
        permission_action="rejected",
    )


def build_chain_blocked_observation(
    tool_name: str,
    reason: str,
    *,
    max_chars: int | None = None,
) -> ReActObservation:
    cap = max_chars if max_chars is not None else DEFAULT_OBSERVATION_MAX_CHARS
    summary_cap = min(160, cap)
    reason_cap = min(300, cap)
    return ReActObservation(
        observation_type="chain_blocked",
        tool_name=tool_name,
        summary=f"ChainDetector blocked {tool_name}: {_truncate(reason, n=summary_cap)}",
        permission_action="chain_block",
        permission_reason=_truncate(reason, n=reason_cap),
    )


def build_direct_answer_observation(
    answer: str,
    *,
    max_chars: int | None = None,
) -> ReActObservation:
    cap = max_chars if max_chars is not None else min(400, DEFAULT_OBSERVATION_MAX_CHARS)
    return ReActObservation(
        observation_type="direct_answer",
        summary=_truncate(answer, n=cap),
    )


def build_empty_search_result_observation(
    tool_name: str,
    query_summary: str,
    *,
    max_chars: int | None = None,
) -> ReActObservation:
    cap = max_chars if max_chars is not None else 120
    return ReActObservation(
        observation_type="empty_search_result",
        tool_name=tool_name,
        summary=f"{tool_name} returned no results for: {_truncate(query_summary, n=cap)}",
    )


def build_max_iteration_observation() -> ReActObservation:
    return ReActObservation(
        observation_type="max_iteration",
        summary="Reached MAX_ITERATIONS without converging.",
    )
