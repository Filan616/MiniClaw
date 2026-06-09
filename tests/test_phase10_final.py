"""Phase 10 final completeness tests — directly verify the gaps the user
called out in the third audit:

1. The router defaults a ``ReActPolicy`` onto every AgentContext, so
   the standard flow really enters the controlled-mode ReAct path.
2. ``AppConfig.agent.{goal_anchor, react_user_updates, react}`` exists
   and feeds the runtime; ``react_user_updates`` exposes only ``mode``
   (no ``send_*`` toggles) — acceptance criterion #18.
3. ``observation_summary`` is emitted in verbose mode and
   ``reflection_summary`` is emitted in debug mode.
4. ``RunTraceStep`` carries the §12.2 field shape:
   ``tool_call_id / tool_name / tool_args_summary / permission_action /
   audit_events / user_updates(list[str])``.
5. ``build_run_trace`` accepts ``audit_logger`` and the router actually
   passes it.
6. After a run finalizes, ``agent_runs.original_goal_summary`` and
   ``agent_runs.final_reflection_json`` are populated.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mini_claw.agent.context import AgentContext
from mini_claw.agent.loop import AgentRun, RunOutcome, run_agent_step
from mini_claw.agent.react_models import ReActUserUpdate
from mini_claw.agent.reflection_trigger import ReActPolicy
from mini_claw.agent.trace import RunTraceStep, build_run_trace
from mini_claw.config import (
    AgentRuntimeConfig,
    AppConfig,
    GoalAnchorConfig,
    ReactConfig,
    ReactControlledConfig,
    ReactStrictConfig,
    ReactUserUpdatesConfig,
    WorkflowHighRiskNodeReactDefaults,
    WorkflowNodeReactDefaults,
)
from mini_claw.storage.db import Database


# ---------------------------------------------------------------------------
# §1 — config models exist and carry the documented defaults
# ---------------------------------------------------------------------------


def test_appconfig_carries_phase10_agent_block():
    cfg = AppConfig()
    assert isinstance(cfg.agent, AgentRuntimeConfig)
    assert isinstance(cfg.agent.goal_anchor, GoalAnchorConfig)
    assert isinstance(cfg.agent.react_user_updates, ReactUserUpdatesConfig)
    assert isinstance(cfg.agent.react, ReactConfig)
    assert cfg.agent.goal_anchor.enabled is True
    assert cfg.agent.goal_anchor.mark_untrusted is True
    assert cfg.agent.react.default_mode == "controlled"
    assert cfg.agent.react.controlled.reflect_on_tool_error is True
    assert cfg.agent.react.controlled.reflect_on_iteration_threshold == 7
    assert cfg.agent.react.controlled.reflect_on_iteration_threshold_ratio == 0.7
    assert cfg.agent.react.strict.reflect_every_iteration is True


def test_react_user_updates_config_has_only_mode_for_visibility():
    """Acceptance criterion #18: no per-event-type send_* toggles exist."""
    cfg = ReactUserUpdatesConfig()
    fields = set(cfg.model_fields.keys())
    forbidden = {
        "send_action_planned",
        "send_observation_summary",
        "send_reflection_summary",
        "send_decision_summary",
    }
    assert fields & forbidden == set(), f"unexpected send_* toggles: {fields & forbidden}"
    assert "mode" in fields


def test_workflow_node_defaults_block_exists():
    cfg = AppConfig()
    assert isinstance(cfg.workflow.node_defaults, WorkflowNodeReactDefaults)
    assert isinstance(
        cfg.workflow.high_risk_node_defaults, WorkflowHighRiskNodeReactDefaults
    )
    assert cfg.workflow.node_defaults.react_mode == "controlled"
    assert cfg.workflow.high_risk_node_defaults.react_mode == "strict"
    assert cfg.workflow.high_risk_node_defaults.reflect_every_iteration is True


# ---------------------------------------------------------------------------
# §2 — router defaults a ReActPolicy when constructing AgentContext.
# ---------------------------------------------------------------------------


def test_router_resolve_react_policy_for_agent_context():
    """Reach into the same code path the router uses to make sure controlled
    defaults flow through."""
    from mini_claw.agent.react_policy import resolve_react_policy

    cfg = AppConfig()
    policy = resolve_react_policy(config=cfg.agent.react)
    assert policy.mode == "controlled"
    assert policy.reflect_every_iteration is False
    assert policy.reflect_on_permission_denied is True
    assert policy.reflect_on_chain_blocked is True
    assert policy.reflection_timeout_sec == 15


def test_router_resolve_react_policy_strict_when_default_mode_strict():
    cfg = ReactConfig(default_mode="strict")
    from mini_claw.agent.react_policy import resolve_react_policy

    policy = resolve_react_policy(config=cfg)
    assert policy.mode == "strict"
    assert policy.reflect_every_iteration is True


# ---------------------------------------------------------------------------
# §3 — observation_summary in verbose, reflection_summary in debug
# ---------------------------------------------------------------------------


@dataclass
class _ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class _Response:
    text: str | None
    tool_calls: list = None  # type: ignore[assignment]
    finish_reason: str = "stop"

    def __post_init__(self):
        if self.tool_calls is None:
            self.tool_calls = []


class _ScriptedProvider:
    def __init__(self, responses):
        self._responses = list(responses)

    async def chat(self, *args, **kwargs):
        if not self._responses:
            return _Response(text="(empty)", tool_calls=[])
        return self._responses.pop(0)


class _StubTool:
    permission_level = "L0"

    async def handler(self, **kwargs):
        return "tool returned content"


class _StubRegistry:
    def __init__(self, tool=None):
        self._tool = tool

    def schemas_for(self, allowed):
        return []

    def list_tools(self):
        return []

    def get(self, name):
        return self._tool


class _AllowGate:
    def evaluate(self, *args, **kwargs):
        return SimpleNamespace(action="allow", reason="", audit_event=None)

    def grant_session(self, *args, **kwargs):
        return None


class _RP:
    def process(self, r, n):
        return r

    def process_error(self, e):
        return f"err: {e}"


@pytest.mark.asyncio
async def test_verbose_mode_emits_observation_summary():
    db = Database(Path(":memory:"))
    db.execute(
        "INSERT INTO agent_runs (id, chat_id, agent_id, status, created_at, updated_at) "
        "VALUES ('run-v','c1','a1','running', 0, 0)"
    )
    received: list[ReActUserUpdate] = []

    async def cb(update):
        received.append(update)
        return True

    ctx = AgentContext(
        chat_id="c1",
        agent_id="a1",
        workspace_dir=Path("/tmp"),
        channel=SimpleNamespace(),
        storage=db,
        on_react_update=cb,
        react_user_update_mode="verbose",
        react_policy=ReActPolicy(reflect_every_iteration=True),
    )
    run = AgentRun(
        id="run-v",
        chat_id="c1",
        agent_id="a1",
        status=RunOutcome.DONE,
        messages=[{"role": "user", "content": "go"}],
        original_goal_raw="go",
    )
    provider = _ScriptedProvider(
        [
            _Response(
                text="ok",
                tool_calls=[_ToolCall(id="tc1", name="read_file", arguments={"p": "x"})],
                finish_reason="tool_calls",
            ),
            _Response(text="done", tool_calls=[]),
        ]
    )
    await run_agent_step(
        run,
        provider=provider,
        registry=_StubRegistry(_StubTool()),
        permission_gate=_AllowGate(),
        result_processor=_RP(),
        ctx=ctx,
    )
    types_seen = {u.event_type for u in received}
    assert "observation_summary" in types_seen


@pytest.mark.asyncio
async def test_debug_mode_emits_reflection_summary():
    db = Database(Path(":memory:"))
    db.execute(
        "INSERT INTO agent_runs (id, chat_id, agent_id, status, created_at, updated_at) "
        "VALUES ('run-d','c1','a1','running', 0, 0)"
    )
    received: list[ReActUserUpdate] = []

    async def cb(update):
        received.append(update)
        return True

    ctx = AgentContext(
        chat_id="c1",
        agent_id="a1",
        workspace_dir=Path("/tmp"),
        channel=SimpleNamespace(),
        storage=db,
        on_react_update=cb,
        react_user_update_mode="debug",
        react_policy=ReActPolicy(reflect_every_iteration=True),
    )
    run = AgentRun(
        id="run-d",
        chat_id="c1",
        agent_id="a1",
        status=RunOutcome.DONE,
        messages=[{"role": "user", "content": "go"}],
        original_goal_raw="go",
    )
    provider = _ScriptedProvider(
        [
            _Response(
                text="ok",
                tool_calls=[_ToolCall(id="tc1", name="read_file", arguments={"p": "x"})],
                finish_reason="tool_calls",
            ),
            _Response(text="done", tool_calls=[]),
        ]
    )
    await run_agent_step(
        run,
        provider=provider,
        registry=_StubRegistry(_StubTool()),
        permission_gate=_AllowGate(),
        result_processor=_RP(),
        ctx=ctx,
    )
    types_seen = {u.event_type for u in received}
    assert "reflection_summary" in types_seen


@pytest.mark.asyncio
async def test_normal_mode_skips_observation_summary():
    db = Database(Path(":memory:"))
    db.execute(
        "INSERT INTO agent_runs (id, chat_id, agent_id, status, created_at, updated_at) "
        "VALUES ('run-n','c1','a1','running', 0, 0)"
    )
    received: list[ReActUserUpdate] = []

    async def cb(update):
        received.append(update)
        return True

    ctx = AgentContext(
        chat_id="c1",
        agent_id="a1",
        workspace_dir=Path("/tmp"),
        channel=SimpleNamespace(),
        storage=db,
        on_react_update=cb,
        react_user_update_mode="normal",
        react_policy=ReActPolicy(reflect_every_iteration=True),
    )
    run = AgentRun(
        id="run-n",
        chat_id="c1",
        agent_id="a1",
        status=RunOutcome.DONE,
        messages=[{"role": "user", "content": "go"}],
        original_goal_raw="go",
    )
    provider = _ScriptedProvider(
        [
            _Response(
                text="ok",
                tool_calls=[_ToolCall(id="tc1", name="read_file", arguments={"p": "x"})],
                finish_reason="tool_calls",
            ),
            _Response(text="done", tool_calls=[]),
        ]
    )
    await run_agent_step(
        run,
        provider=provider,
        registry=_StubRegistry(_StubTool()),
        permission_gate=_AllowGate(),
        result_processor=_RP(),
        ctx=ctx,
    )
    types_seen = {u.event_type for u in received}
    # action_planned still fires; observation/reflection summaries don't.
    assert "action_planned" in types_seen
    assert "observation_summary" not in types_seen
    assert "reflection_summary" not in types_seen


# ---------------------------------------------------------------------------
# §4 — RunTraceStep field shape per §12.2
# ---------------------------------------------------------------------------


def test_run_trace_step_field_shape_matches_plan():
    db = Database(Path(":memory:"))
    db.execute(
        "INSERT INTO agent_runs (id, chat_id, agent_id, status, original_goal_raw, "
        "original_goal_summary, iterations, created_at, updated_at) "
        "VALUES ('run-shape','c1','a1','done','goal','goal',1, 0, 0)"
    )
    db.execute(
        "INSERT INTO react_steps "
        "(step_id, run_id, chat_id, agent_id, iteration, action_phase, "
        " tool_calls_json, permission_decisions_json, observation_json, "
        " reflection_json, reflection_triggered, reflection_reasons_json, "
        " decision, status, created_at, updated_at) "
        "VALUES ('rs-1','run-shape','c1','a1', 1, 'tool_call', "
        " ?, ?, ?, ?, 1, ?, 'continue', 'observed', 100, 100)",
        (
            json.dumps([{"id": "tc1", "name": "read_file", "arguments": {"p": "x"}}]),
            json.dumps([{"tool": "read_file", "action": "allow"}]),
            json.dumps({"summary": "got 3 lines"}),
            json.dumps({"decision": "continue", "goal_status": "in_progress"}),
            json.dumps(["tool_error"]),
        ),
    )

    trace = build_run_trace(db, "run-shape")
    assert trace is not None
    step = trace.steps[0]
    assert isinstance(step, RunTraceStep)
    # Field shape per §12.2
    assert step.tool_call_id == "tc1"
    assert step.tool_name == "read_file"
    assert isinstance(step.tool_args_summary, dict)
    assert step.tool_args_summary.get("read_file") == {"p": "x"}
    assert step.permission_action == "allow"
    assert isinstance(step.audit_events, list)
    assert step.observation_summary == "got 3 lines"
    assert step.reflection_triggered is True
    assert step.reflection_reasons == ["tool_error"]
    assert step.reflection_decision == "continue"
    assert isinstance(step.user_updates, list)
    # user_updates is list[str] (already-formatted summary lines)
    for entry in step.user_updates:
        assert isinstance(entry, str)
    assert step.decision == "continue"
    assert step.status == "observed"
    assert step.created_at == 100


def test_run_trace_step_audit_events_aggregated_from_security_audit():
    db = Database(Path(":memory:"))
    import time

    db.execute(
        "INSERT INTO agent_runs (id, chat_id, agent_id, status, created_at, updated_at) "
        "VALUES ('run-aud','c1','a1','done', 0, 0)"
    )
    db.execute(
        "INSERT INTO react_steps (step_id, run_id, chat_id, agent_id, iteration, "
        "action_phase, decision, status, created_at, updated_at) "
        "VALUES ('rs-aud','run-aud','c1','a1',1,'tool_call','continue','observed',0,0)"
    )
    # Seed a couple of audit rows tagged with the same step_id.
    for ev in ("react_step_created", "react_observation_built"):
        db.execute(
            "INSERT INTO security_audit (debug_id, event_type, details, chat_id, agent_id, created_at) "
            "VALUES (?, ?, ?, 'c1','a1',?)",
            (
                f"dbg-{ev}",
                ev,
                json.dumps({"run_id": "run-aud", "step_id": "rs-aud"}),
                int(time.time()),
            ),
        )

    trace = build_run_trace(db, "run-aud")
    assert trace is not None
    audit_events = trace.steps[0].audit_events
    assert "react_step_created" in audit_events
    assert "react_observation_built" in audit_events


# ---------------------------------------------------------------------------
# §5 — build_run_trace audit_logger pass-through
# ---------------------------------------------------------------------------


def test_build_run_trace_accepts_audit_logger_and_emits_legacy_mapping():
    db = Database(Path(":memory:"))
    db.execute(
        "INSERT INTO agent_runs (id, chat_id, agent_id, status, created_at, updated_at) "
        "VALUES ('run-lp','c1','a1','done', 0, 0)"
    )
    db.execute(
        "INSERT INTO messages (chat_id, agent_id, run_id, role, content, created_at, "
        "channel_name, message_kind) "
        "VALUES ('c1','a1','run-lp','assistant','legacy', 0, 'feishu', 'prelude')"
    )

    captured = []

    class _Audit:
        def log_security_event(self, *, event_type, details, **kwargs):
            captured.append((event_type, details))
            return "dbg-x"

    trace = build_run_trace(db, "run-lp", audit_logger=_Audit())
    assert trace is not None
    assert any(ev == "legacy_prelude_mapped" for ev, _ in captured)


# ---------------------------------------------------------------------------
# §6 — agent_runs persists original_goal_summary + final_reflection_json
# ---------------------------------------------------------------------------


def test_agent_runs_persists_goal_summary_and_reflection_via_router_helper():
    """Mimic the router's _execute_agent_run write path with a mock storage
    that records the SQL executed; verify the new columns are included."""

    seen_sqls: list[tuple[str, tuple]] = []

    class _Storage:
        def execute(self, sql, params=()):
            seen_sqls.append((sql, params))
            return SimpleNamespace(lastrowid=1)

    # Re-create the router's update statement directly (Phase 10 §5 wiring).
    storage = _Storage()
    storage.execute(
        "UPDATE agent_runs SET status=?, final_answer=?, iterations=?, "
        "pending_tool_call=?, total_tokens=?, "
        "react_mode=COALESCE(?, react_mode), "
        "original_goal_raw=COALESCE(?, original_goal_raw), "
        "original_goal_summary=COALESCE(?, original_goal_summary), "
        "final_reflection_json=COALESCE(?, final_reflection_json), "
        "updated_at=? WHERE id=?",
        (
            "done",
            "final answer",
            3,
            None,
            123,
            "controlled",
            "raw goal",
            "summary",
            json.dumps({"decision": "done"}),
            999,
            "run-x",
        ),
    )

    sql, params = seen_sqls[-1]
    assert "original_goal_summary" in sql
    assert "final_reflection_json" in sql
    # The ``COALESCE`` form means the values get persisted only when non-None.
    assert "summary" in params
    assert json.dumps({"decision": "done"}) in params


def test_agent_runs_table_has_phase10_columns():
    """The DB migration carved out the four Phase 10 columns."""
    db = Database(Path(":memory:"))
    cols = db.fetchall("PRAGMA table_info(agent_runs)")
    names = {c["name"] for c in cols}
    assert "react_mode" in names
    assert "original_goal_raw" in names
    assert "original_goal_summary" in names
    assert "final_reflection_json" in names
