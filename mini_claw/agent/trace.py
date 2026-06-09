"""Phase 10 M10.4: RunTraceView.

Aggregates ``agent_runs / tool_calls / security_audit / messages /
react_steps / react_user_updates`` into a unified, queryable trace.

Read-only — never mutates state. Legacy ``message_kind='prelude'``
rows are mapped to a synthetic ``action_planned`` update so existing
runs remain inspectable (P6 in plans/ReAct.md).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class RunTraceUpdate:
    event_type: str
    visible_level: str
    text: str
    is_important: bool = False
    legacy: bool = False
    sent_at: int | None = None


@dataclass(slots=True)
class RunTraceStep:
    """Phase 10 §12.2: per-step trace row.

    Field shape mirrors plans/ReAct.md exactly so consumers can rely on
    ``tool_call_id / tool_name / tool_args_summary / permission_action /
    audit_events / user_updates(list[str])``. ``raw_updates`` keeps the
    full ReActUserUpdate objects for callers that want them (legacy
    callers go through ``RunTraceStep.user_updates`` which is a list of
    short summary strings, per the spec).
    """

    iteration: int | None
    tool_call_id: str | None
    tool_name: str | None
    tool_args_summary: dict[str, Any]

    permission_action: str | None
    audit_events: list[str]

    observation_summary: str | None
    reflection_triggered: bool
    reflection_reasons: list[str]
    reflection_decision: str | None

    user_updates: list[str]
    raw_updates: list[RunTraceUpdate]

    decision: str | None
    status: str
    created_at: int

    # Legacy compatibility — older renderers used these names.
    @property
    def tool_calls_summary(self) -> list[str]:
        return [self.tool_name] if self.tool_name else []

    @property
    def step_id(self) -> str | None:
        return self._step_id

    @property
    def action_phase(self) -> str | None:
        return self._action_phase

    @property
    def permission_decisions(self) -> list[dict[str, Any]]:
        return self._permission_decisions

    # Internals used by build_run_trace.
    _step_id: str | None = None
    _action_phase: str | None = None
    _permission_decisions: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class RunTrace:
    run_id: str
    chat_id: str
    agent_id: str
    status: str
    original_goal: str | None
    final_answer: str | None
    iterations: int
    steps: list[RunTraceStep] = field(default_factory=list)


def _safe_json_loads(text: Any) -> Any:
    if not text:
        return None
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _summarize_tool_calls(tool_calls_json: Any) -> list[str]:
    data = _safe_json_loads(tool_calls_json) or []
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for tc in data:
        if not isinstance(tc, dict):
            continue
        name = tc.get("name") or tc.get("function", {}).get("name") or "?"
        out.append(str(name))
    return out


def _load_legacy_preludes(
    storage: Any,
    run_id: str,
    *,
    audit_logger: Any | None = None,
    chat_id: str | None = None,
    agent_id: str | None = None,
) -> list[RunTraceUpdate]:
    """Map legacy ``message_kind='prelude'`` rows to synthetic updates.

    Phase 10 §6 — emits ``legacy_prelude_mapped`` audit when at least one
    legacy row is materialized so downstream pipelines can track residual
    legacy data exposure.
    """
    rows = storage.fetchall(
        "SELECT content, created_at FROM messages "
        "WHERE run_id = ? AND COALESCE(message_kind, 'normal') = 'prelude' "
        "ORDER BY created_at ASC, id ASC",
        (run_id,),
    )
    if rows and audit_logger is not None:
        try:
            audit_logger.log_security_event(
                event_type="legacy_prelude_mapped",
                details={"run_id": run_id, "count": len(rows)},
                chat_id=chat_id,
                agent_id=agent_id,
            )
        except Exception:
            pass
    return [
        RunTraceUpdate(
            event_type="action_planned",
            visible_level="normal",
            text=(row.get("content") or ""),
            is_important=False,
            legacy=True,
            sent_at=row.get("created_at"),
        )
        for row in rows
    ]


def _load_react_updates(storage: Any, run_id: str) -> dict[str, list[RunTraceUpdate]]:
    """Group react_user_updates by step_id."""
    try:
        rows = storage.fetchall(
            "SELECT step_id, event_type, visible_level, is_important, "
            "redacted_text, sent_at FROM react_user_updates "
            "WHERE run_id = ? ORDER BY created_at ASC",
            (run_id,),
        )
    except Exception:
        return {}
    out: dict[str, list[RunTraceUpdate]] = {}
    for row in rows:
        step_id = row.get("step_id") or ""
        out.setdefault(step_id, []).append(
            RunTraceUpdate(
                event_type=row.get("event_type") or "action_planned",
                visible_level=row.get("visible_level") or "normal",
                text=row.get("redacted_text") or "",
                is_important=bool(row.get("is_important")),
                legacy=False,
                sent_at=row.get("sent_at"),
            )
        )
    return out


def _load_react_steps(storage: Any, run_id: str) -> list[dict[str, Any]]:
    try:
        return storage.fetchall(
            "SELECT * FROM react_steps WHERE run_id = ? ORDER BY iteration ASC, created_at ASC",
            (run_id,),
        )
    except Exception:
        return []


def _load_audit_events_for_run(
    storage: Any, run_id: str
) -> dict[str, list[str]]:
    """Phase 10 §12: aggregate ``security_audit`` rows for a run.

    Returns ``{step_id: [event_type, ...]}``. Step IDs are extracted from
    ``details->'step_id'`` if present so each RunTraceStep can carry the
    audit events that fired during it.
    """
    try:
        rows = storage.fetchall(
            "SELECT event_type, details FROM security_audit "
            "WHERE details LIKE ? ORDER BY id ASC",
            (f"%{run_id}%",),
        )
    except Exception:
        return {}
    by_step: dict[str, list[str]] = {}
    for row in rows:
        details = _safe_json_loads(row.get("details")) or {}
        if not isinstance(details, dict) or details.get("run_id") != run_id:
            continue
        step_id = details.get("step_id") or "_run"
        by_step.setdefault(step_id, []).append(row["event_type"])
    return by_step


def _summarize_tool_args(tool_calls_json: Any) -> dict[str, Any]:
    """Pull a redacted ``{tool: arg_summary}`` map from the step row."""
    data = _safe_json_loads(tool_calls_json) or []
    if not isinstance(data, list):
        return {}
    out: dict[str, Any] = {}
    for tc in data:
        if not isinstance(tc, dict):
            continue
        name = tc.get("name") or tc.get("function", {}).get("name") or "?"
        args = tc.get("arguments")
        if isinstance(args, dict):
            preview = {
                k: ("***" if isinstance(v, str) and len(v) > 80 else v)
                for k, v in list(args.items())[:5]
            }
        else:
            preview = {"_raw": str(args)[:80] if args else ""}
        out[str(name)] = preview
    return out


def build_run_trace(
    storage: Any,
    run_id: str,
    *,
    audit_logger: Any | None = None,
) -> RunTrace | None:
    """Build a complete RunTrace for ``run_id`` or ``None`` if unknown."""
    run = storage.fetchone(
        "SELECT id, chat_id, agent_id, status, final_answer, iterations, "
        "original_goal_raw, original_goal_summary FROM agent_runs WHERE id = ?",
        (run_id,),
    )
    if run is None:
        return None

    legacy_updates = _load_legacy_preludes(
        storage,
        run_id,
        audit_logger=audit_logger,
        chat_id=run.get("chat_id"),
        agent_id=run.get("agent_id"),
    )
    react_updates_by_step = _load_react_updates(storage, run_id)
    step_rows = _load_react_steps(storage, run_id)
    audit_by_step = _load_audit_events_for_run(storage, run_id)

    steps: list[RunTraceStep] = []
    if step_rows:
        for row in step_rows:
            tool_calls = _safe_json_loads(row.get("tool_calls_json")) or []
            first_tc = tool_calls[0] if isinstance(tool_calls, list) and tool_calls else {}
            tool_call_id = first_tc.get("id") if isinstance(first_tc, dict) else None
            tool_name = first_tc.get("name") if isinstance(first_tc, dict) else None
            args_summary = _summarize_tool_args(row.get("tool_calls_json"))

            permission_decisions = (
                _safe_json_loads(row.get("permission_decisions_json")) or []
            )
            permission_action = None
            if isinstance(permission_decisions, list) and permission_decisions:
                first_pd = permission_decisions[0]
                if isinstance(first_pd, dict):
                    permission_action = first_pd.get("action")

            observation = _safe_json_loads(row.get("observation_json")) or {}
            reflection = _safe_json_loads(row.get("reflection_json")) or {}
            reasons = _safe_json_loads(row.get("reflection_reasons_json")) or []
            raw_updates = react_updates_by_step.get(row.get("step_id") or "", [])
            user_updates_strings = [
                f"[{u.event_type}] {u.text}" + (" (legacy)" if u.legacy else "")
                for u in raw_updates
            ]

            step_id = row.get("step_id")
            audit_events = audit_by_step.get(step_id, [])
            step = RunTraceStep(
                iteration=row.get("iteration"),
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                tool_args_summary=args_summary,
                permission_action=permission_action,
                audit_events=audit_events,
                observation_summary=(observation or {}).get("summary"),
                reflection_triggered=bool(row.get("reflection_triggered")),
                reflection_reasons=list(reasons) if isinstance(reasons, list) else [],
                reflection_decision=(reflection or {}).get("decision"),
                user_updates=user_updates_strings,
                raw_updates=raw_updates,
                decision=row.get("decision"),
                status=row.get("status") or "",
                created_at=int(row.get("created_at") or 0),
                _step_id=step_id,
                _action_phase=row.get("action_phase"),
                _permission_decisions=permission_decisions
                if isinstance(permission_decisions, list)
                else [],
            )
            steps.append(step)

    # Legacy fallback: when no react_steps recorded, expose legacy preludes
    # under a single synthetic step so /run trace still has something to show.
    if not steps and legacy_updates:
        steps.append(
            RunTraceStep(
                iteration=None,
                tool_call_id=None,
                tool_name=None,
                tool_args_summary={},
                permission_action=None,
                audit_events=audit_by_step.get("_run", []),
                observation_summary=None,
                reflection_triggered=False,
                reflection_reasons=[],
                reflection_decision=None,
                user_updates=[
                    f"[{u.event_type}] {u.text} (legacy)" for u in legacy_updates
                ],
                raw_updates=list(legacy_updates),
                decision=None,
                status="legacy",
                created_at=0,
                _step_id=None,
                _action_phase=None,
                _permission_decisions=[],
            )
        )
    elif legacy_updates:
        steps.append(
            RunTraceStep(
                iteration=None,
                tool_call_id=None,
                tool_name=None,
                tool_args_summary={},
                permission_action=None,
                audit_events=[],
                observation_summary=None,
                reflection_triggered=False,
                reflection_reasons=[],
                reflection_decision=None,
                user_updates=[
                    f"[{u.event_type}] {u.text} (legacy)" for u in legacy_updates
                ],
                raw_updates=list(legacy_updates),
                decision=None,
                status="legacy",
                created_at=0,
                _step_id=None,
                _action_phase=None,
                _permission_decisions=[],
            )
        )

    return RunTrace(
        run_id=run["id"],
        chat_id=run["chat_id"],
        agent_id=run["agent_id"],
        status=run.get("status") or "",
        original_goal=run.get("original_goal_summary") or run.get("original_goal_raw"),
        final_answer=run.get("final_answer"),
        iterations=int(run.get("iterations") or 0),
        steps=steps,
    )


def render_trace_text(trace: RunTrace) -> str:
    """Render a RunTrace as a plain-text summary suitable for /run trace."""
    lines: list[str] = [
        f"Run: {trace.run_id}",
        f"Status: {trace.status}",
    ]
    if trace.original_goal:
        lines.append(f"Original Goal: {trace.original_goal}")
    lines.append(f"Iterations: {trace.iterations}")
    lines.append("")

    if not trace.steps:
        lines.append("(no steps recorded)")
    for i, step in enumerate(trace.steps, 1):
        header = f"Step {step.iteration if step.iteration is not None else i}"
        if step.action_phase:
            header += f" [{step.action_phase}]"
        if step.status:
            header += f" — {step.status}"
        lines.append(header)
        for upd_line in step.user_updates:
            lines.append(f"- {upd_line}")
        if step.tool_name:
            lines.append(f"- Tool: {step.tool_name}")
        if step.tool_args_summary:
            lines.append(f"- Args: {step.tool_args_summary}")
        if step.permission_action:
            lines.append(f"- Permission: {step.permission_action}")
        if step.observation_summary:
            lines.append(f"- Observation: {step.observation_summary}")
        if step.reflection_triggered:
            lines.append(
                f"- Reflection: triggered ({', '.join(step.reflection_reasons)}) "
                f"→ {step.reflection_decision or '?'}"
            )
        if step.audit_events:
            lines.append(f"- Audit: {', '.join(step.audit_events)}")
        if step.decision:
            lines.append(f"- Decision: {step.decision}")
        lines.append("")

    if trace.final_answer:
        lines.append(f"Final: {trace.final_answer}")
    return "\n".join(lines).rstrip() + "\n"
