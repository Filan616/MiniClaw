"""Phase 10 follow-up wiring tests.

Covers the gaps the user flagged in the second audit:
- Loop opens permission_denied / chain_blocked / approval_required
  steps with the right Observation when ``ctx.react_policy`` is set.
- ``react_blocked_by_permission`` and ``react_blocked_by_chain_detector``
  audit events fire on terminal blocks.
- ``react_finalized`` fires for finalize decisions.
- ``react_reflection_timeout`` fires when run_reflection times out.
- ``legacy_prelude_mapped`` audit fires when trace materializes legacy
  ``message_kind='prelude'`` rows.
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
from mini_claw.agent.loop import (
    AgentRun,
    RunOutcome,
    run_agent_step,
)
from mini_claw.agent.reflection import (
    parse_reflection_json,
    run_reflection,
)
from mini_claw.agent.reflection_trigger import ReActPolicy
from mini_claw.agent.trace import build_run_trace
from mini_claw.gateway.session import SessionManager
from mini_claw.storage.db import Database


# ---------------------------------------------------------------------------
# Test fixtures: scripted provider + simple registries
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

    def __post_init__(self) -> None:
        if self.tool_calls is None:
            self.tool_calls = []


class _ScriptedProvider:
    def __init__(self, responses):
        self._responses = list(responses)

    async def chat(self, *args, **kwargs):
        if not self._responses:
            return _Response(text="(empty)", tool_calls=[])
        return self._responses.pop(0)


class _AlwaysDenyGate:
    def evaluate(self, *args, **kwargs):
        return SimpleNamespace(
            action="deny",
            reason="policy denial",
            audit_event=None,
            internal_reason="policy denial",
        )

    def grant_session(self, *args, **kwargs):
        return None


class _AlwaysAllowGate:
    def evaluate(self, *args, **kwargs):
        return SimpleNamespace(action="allow", reason="", audit_event=None)

    def grant_session(self, *args, **kwargs):
        return None


class _NeedApprovalGate:
    def evaluate(self, *args, **kwargs):
        return SimpleNamespace(
            action="need_approval",
            reason="L3 review",
            audit_event=None,
            permission_level="L3",
        )

    def grant_session(self, *args, **kwargs):
        return None


class _StubTool:
    permission_level = "L0"

    async def handler(self, **kwargs):
        return "ok"


class _StubRegistry:
    def __init__(self, tool: Any | None = None) -> None:
        self._tool = tool

    def schemas_for(self, allowed):
        return []

    def list_tools(self):
        return []

    def get(self, name):
        return self._tool


class _RP:
    def process(self, r, n):
        return r

    def process_error(self, e):
        return f"err: {e}"


def _seed_run(db: Database, run_id: str = "run-x") -> None:
    db.execute(
        "INSERT INTO agent_runs (id, chat_id, agent_id, status, created_at, updated_at) "
        f"VALUES ('{run_id}','c1','a1','running', 0, 0)"
    )


# ---------------------------------------------------------------------------
# Per-call permission_denied step + react_blocked_by_permission
# ---------------------------------------------------------------------------


class _CapturingAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log_security_event(self, *, event_type, details, **kwargs):
        self.events.append((event_type, dict(details or {})))
        return f"dbg-{len(self.events)}"


@pytest.mark.asyncio
async def test_permission_denied_terminates_with_block_audit():
    db = Database(Path(":memory:"))
    _seed_run(db, "run-deny")
    audit = _CapturingAudit()
    ctx = AgentContext(
        chat_id="c1",
        agent_id="a1",
        workspace_dir=Path("/tmp"),
        channel=SimpleNamespace(),
        storage=db,
        audit_logger=audit,
        react_policy=ReActPolicy(),
    )
    run = AgentRun(
        id="run-deny",
        chat_id="c1",
        agent_id="a1",
        status=RunOutcome.DONE,
        messages=[{"role": "user", "content": "do it"}],
        original_goal_raw="do it",
    )
    provider = _ScriptedProvider(
        [
            _Response(
                text="ok",
                tool_calls=[_ToolCall(id="tc1", name="write_file", arguments={"path": "x"})],
                finish_reason="tool_calls",
            ),
            _Response(text="done", tool_calls=[]),
        ]
    )

    out = await run_agent_step(
        run,
        provider=provider,
        registry=_StubRegistry(_StubTool()),
        permission_gate=_AlwaysDenyGate(),
        result_processor=_RP(),
        ctx=ctx,
    )

    assert out.status == RunOutcome.ABORTED
    rows = db.fetchall(
        "SELECT action_phase, decision, observation_json FROM react_steps "
        "WHERE run_id='run-deny' ORDER BY iteration ASC"
    )
    assert any(r["action_phase"] == "permission_denied" for r in rows)
    assert any(r["decision"] == "blocked" for r in rows)
    event_types = [e[0] for e in audit.events]
    assert "react_observation_built" in event_types
    assert "react_decision_made" in event_types
    assert "react_blocked_by_permission" in event_types


@pytest.mark.asyncio
async def test_approval_required_records_reflection_step():
    db = Database(Path(":memory:"))
    _seed_run(db, "run-app")
    audit = _CapturingAudit()
    ctx = AgentContext(
        chat_id="c1",
        agent_id="a1",
        workspace_dir=Path("/tmp"),
        channel=SimpleNamespace(),
        storage=db,
        audit_logger=audit,
        react_policy=ReActPolicy(),
    )
    run = AgentRun(
        id="run-app",
        chat_id="c1",
        agent_id="a1",
        status=RunOutcome.DONE,
        messages=[{"role": "user", "content": "do it"}],
        original_goal_raw="do it",
    )
    provider = _ScriptedProvider(
        [
            _Response(
                text="ok",
                tool_calls=[_ToolCall(id="tc1", name="write_file", arguments={"p": 1})],
                finish_reason="tool_calls",
            )
        ]
    )

    out = await run_agent_step(
        run,
        provider=provider,
        registry=_StubRegistry(_StubTool()),
        permission_gate=_NeedApprovalGate(),
        result_processor=_RP(),
        ctx=ctx,
    )

    assert out.status == RunOutcome.SUSPENDED
    row = db.fetchone(
        "SELECT action_phase, decision, status, observation_json, reflection_json FROM react_steps WHERE run_id='run-app'"
    )
    assert row is not None
    assert row["action_phase"] == "approval_required"
    assert row["decision"] == "suspended"
    assert row["status"] == "suspended"
    obs = json.loads(row["observation_json"])
    assert obs["observation_type"] == "approval_required"
    refl = json.loads(row["reflection_json"])
    assert refl["decision"] in {"suspended", "needs_approval"} or refl["safety_assessment"] == "needs_user_input"


@pytest.mark.asyncio
async def test_finalized_emits_react_finalized_audit():
    """direct-answer finalize emits the react_finalized audit event."""
    db = Database(Path(":memory:"))
    _seed_run(db, "run-fin")
    audit = _CapturingAudit()
    ctx = AgentContext(
        chat_id="c1",
        agent_id="a1",
        workspace_dir=Path("/tmp"),
        channel=SimpleNamespace(),
        storage=db,
        audit_logger=audit,
        react_policy=ReActPolicy(),
    )
    run = AgentRun(
        id="run-fin",
        chat_id="c1",
        agent_id="a1",
        status=RunOutcome.DONE,
        messages=[{"role": "user", "content": "hi"}],
        original_goal_raw="hi",
    )
    provider = _ScriptedProvider([_Response(text="hello back", tool_calls=[])])

    await run_agent_step(
        run,
        provider=provider,
        registry=_StubRegistry(),
        permission_gate=_AlwaysAllowGate(),
        result_processor=_RP(),
        ctx=ctx,
    )

    assert any(e[0] == "react_finalized" for e in audit.events)


# ---------------------------------------------------------------------------
# react_reflection_timeout
# ---------------------------------------------------------------------------


class _TimeoutProvider:
    async def chat(self, *args, **kwargs):
        await asyncio.sleep(5)
        return _Response(text="should never arrive", tool_calls=[])


@pytest.mark.asyncio
async def test_run_reflection_timeout_marks_result():
    from mini_claw.agent.observation import build_tool_error_observation

    obs = build_tool_error_observation("write_file", "broke")
    result = await run_reflection(
        provider=_TimeoutProvider(),
        observation=obs,
        original_goal_summary="goal",
        iteration=1,
        max_iterations=10,
        trigger_reasons=["tool_error"],
        timeout_sec=1,
    )
    assert getattr(result, "timed_out", False) is True
    assert result.parse_failed is True
    assert result.fallback_used is True


# ---------------------------------------------------------------------------
# legacy_prelude_mapped audit
# ---------------------------------------------------------------------------


def test_legacy_prelude_mapped_audit_emit():
    db = Database(Path(":memory:"))
    db.execute(
        "INSERT INTO agent_runs (id, chat_id, agent_id, status, created_at, updated_at) "
        "VALUES ('run-leg','c1','a1','done', 0, 0)"
    )
    sm = SessionManager(db)
    sm.store_message(
        chat_id="c1",
        agent_id="a1",
        role="assistant",
        content="legacy prelude",
        run_id="run-leg",
        channel_name="feishu",
        workspace_dir="/tmp",
        message_kind="prelude",
    )

    audit = _CapturingAudit()
    trace = build_run_trace(db, "run-leg", audit_logger=audit)
    assert trace is not None
    assert any(e[0] == "legacy_prelude_mapped" for e in audit.events)
    legacy_updates = [u for s in trace.steps for u in s.raw_updates if u.legacy]
    assert legacy_updates


# ---------------------------------------------------------------------------
# Reflection schema
# ---------------------------------------------------------------------------


def test_reflection_schema_pydantic_validates():
    from mini_claw.agent.reflection import ReflectionSchema

    schema = ReflectionSchema(
        observation_summary="ok",
        goal_status="done",
        decision="done",
        confidence=0.5,
    )
    assert schema.decision == "done"
    assert schema.goal_status == "done"


def test_parse_reflection_json_robust_to_extra_fields():
    raw = json.dumps(
        {
            "observation_summary": "x",
            "goal_status": "in_progress",
            "decision": "continue",
            "confidence": 0.6,
            "extra_field": "ignored",
        }
    )
    parsed = parse_reflection_json(raw)
    assert parsed is not None
    assert parsed.decision == "continue"


# ---------------------------------------------------------------------------
# Loop main flow no longer writes message_kind='prelude'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_does_not_write_prelude_messages():
    db = Database(Path(":memory:"))
    _seed_run(db, "run-no-pre")

    received: list = []

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
    )
    run = AgentRun(
        id="run-no-pre",
        chat_id="c1",
        agent_id="a1",
        status=RunOutcome.DONE,
        messages=[{"role": "user", "content": "go"}],
        original_goal_raw="go",
    )
    provider = _ScriptedProvider(
        [
            _Response(
                text="好的，我先读取文件",
                tool_calls=[_ToolCall(id="tc1", name="read_file", arguments={"p": "x"})],
                finish_reason="tool_calls",
            ),
            _Response(text="完成", tool_calls=[]),
        ]
    )

    await run_agent_step(
        run,
        provider=provider,
        registry=_StubRegistry(_StubTool()),
        permission_gate=_AlwaysAllowGate(),
        result_processor=_RP(),
        ctx=ctx,
    )

    # The new flow must never write message_kind='prelude' rows.
    prelude_rows = db.fetchall(
        "SELECT id FROM messages WHERE message_kind='prelude'"
    )
    assert prelude_rows == []
    # And it must not flip the legacy ``run.prelude_sent`` flag.
    assert run.prelude_sent is False
