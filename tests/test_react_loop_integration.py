"""Phase 10: AgentLoop ↔ ReAct integration smoke tests.

These tests stub out the LLM provider so they cover only the loop's
ReAct hooks: Goal Anchor injection, ReActStep persistence, and
ReActUserUpdate dispatch.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mini_claw.agent.context import AgentContext
from mini_claw.agent.loop import AgentRun, RunOutcome, run_agent_step
from mini_claw.agent.reflection_trigger import ReActPolicy
from mini_claw.storage.db import Database


# ---------------------------------------------------------------------------
# Stubs
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
    """Plays back a queued list of ProviderResponses."""

    def __init__(self, responses: list[_Response]) -> None:
        self._responses = list(responses)

    async def chat(self, *args, **kwargs) -> _Response:  # noqa: D401
        if not self._responses:
            return _Response(text="(no more responses)", tool_calls=[])
        return self._responses.pop(0)


class _StubRegistry:
    def schemas_for(self, allowed):
        return []

    def list_tools(self):
        return []

    def get(self, name):
        return None


class _StubGate:
    def evaluate(self, *args, **kwargs):
        return SimpleNamespace(action="allow", reason="", audit_event=None)

    def grant_session(self, *args, **kwargs):
        return None


class _StubChannel:
    """Channel without send_stream_chunk so streaming is disabled."""


class _StubResultProcessor:
    def process(self, result, name):
        return result

    def process_error(self, exc):
        return f"err: {exc}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_run(text: str) -> AgentRun:
    return AgentRun(
        id="run-test",
        chat_id="chat1",
        agent_id="agent1",
        status=RunOutcome.DONE,
        messages=[{"role": "user", "content": text}],
        original_goal_raw=text,
    )


def _make_ctx(*, storage: Database, on_react_update=None, react_policy=None) -> AgentContext:
    return AgentContext(
        chat_id="chat1",
        agent_id="agent1",
        workspace_dir=Path("/tmp"),
        channel=_StubChannel(),
        storage=storage,
        on_react_update=on_react_update,
        react_policy=react_policy,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_answer_records_react_step_legacy_path():
    """No react_policy → legacy DONE behavior, but step is still persisted."""
    storage = Database(Path(":memory:"))
    storage.execute(
        "INSERT INTO agent_runs (id, chat_id, agent_id, status, created_at, updated_at) "
        "VALUES ('run-test','chat1','agent1','running', 0, 0)"
    )
    run = _make_run("hello")
    ctx = _make_ctx(storage=storage)
    provider = _ScriptedProvider([_Response(text="hi there", tool_calls=[])])

    out = await run_agent_step(
        run,
        provider=provider,
        registry=_StubRegistry(),
        permission_gate=_StubGate(),
        result_processor=_StubResultProcessor(),
        ctx=ctx,
    )

    assert out.status == RunOutcome.DONE
    assert out.final_answer == "hi there"
    rows = storage.fetchall("SELECT step_id, action_phase, decision, status FROM react_steps WHERE run_id='run-test'")
    assert rows
    assert rows[-1]["action_phase"] == "direct_answer"
    assert rows[-1]["decision"] == "finalize"


@pytest.mark.asyncio
async def test_direct_answer_with_react_policy_runs_reflection():
    """With ReActPolicy set, the loop builds Reflection (fallback path) + DecisionController."""
    storage = Database(Path(":memory:"))
    storage.execute(
        "INSERT INTO agent_runs (id, chat_id, agent_id, status, created_at, updated_at) "
        "VALUES ('run-test','chat1','agent1','running', 0, 0)"
    )
    run = _make_run("hello")
    policy = ReActPolicy()
    ctx = _make_ctx(storage=storage, react_policy=policy)
    provider = _ScriptedProvider([_Response(text="all done", tool_calls=[])])

    out = await run_agent_step(
        run,
        provider=provider,
        registry=_StubRegistry(),
        permission_gate=_StubGate(),
        result_processor=_StubResultProcessor(),
        ctx=ctx,
    )

    assert out.status == RunOutcome.DONE
    rows = storage.fetchall(
        "SELECT reflection_triggered, reflection_reasons_json, decision FROM react_steps WHERE run_id='run-test'"
    )
    assert rows
    last = rows[-1]
    assert last["reflection_triggered"] == 1
    assert "before_finalize" in (last["reflection_reasons_json"] or "")
    assert last["decision"] == "finalize"


@pytest.mark.asyncio
async def test_react_user_update_callback_invoked_for_action_planned():
    """When tool_calls are present, the loop emits an action_planned update."""
    storage = Database(Path(":memory:"))
    storage.execute(
        "INSERT INTO agent_runs (id, chat_id, agent_id, status, created_at, updated_at) "
        "VALUES ('run-test','chat1','agent1','running', 0, 0)"
    )

    received: list[Any] = []

    async def on_update(update) -> bool:
        received.append(update)
        return True

    run = _make_run("帮我读取文件")
    ctx = _make_ctx(storage=storage, on_react_update=on_update)
    # First response carries a tool_call; second short-circuits with direct answer.
    response_with_tool = _Response(
        text="好的，我先读取这个文件。",
        tool_calls=[_ToolCall(id="t1", name="read_file", arguments={"path": "x"})],
        finish_reason="tool_calls",
    )
    response_done = _Response(text="完成", tool_calls=[])
    provider = _ScriptedProvider([response_with_tool, response_done])

    # _StubRegistry.get returns None which makes the loop record the
    # tool as "unknown" and continue — that is enough to exercise the
    # ReActUserUpdate emission.
    await run_agent_step(
        run,
        provider=provider,
        registry=_StubRegistry(),
        permission_gate=_StubGate(),
        result_processor=_StubResultProcessor(),
        ctx=ctx,
    )

    assert received, "expected at least one ReActUserUpdate"
    first = received[0]
    assert first.event_type == "action_planned"
    assert first.text  # non-empty after sanitize
    # text_hash must be the hash of the actually-sent text.
    from mini_claw.agent.react_update import hash_text

    assert first.text_hash == hash_text(first.text)


@pytest.mark.asyncio
async def test_goal_anchor_no_extra_llm_call():
    """Goal Anchor injection must not consume extra provider responses."""
    storage = Database(Path(":memory:"))
    storage.execute(
        "INSERT INTO agent_runs (id, chat_id, agent_id, status, created_at, updated_at) "
        "VALUES ('run-test','chat1','agent1','running', 0, 0)"
    )
    run = _make_run("a" * 1500)  # long enough to truncate
    ctx = _make_ctx(storage=storage)
    provider = _ScriptedProvider([_Response(text="ok", tool_calls=[])])

    await run_agent_step(
        run,
        provider=provider,
        registry=_StubRegistry(),
        permission_gate=_StubGate(),
        result_processor=_StubResultProcessor(),
        ctx=ctx,
    )

    # Provider should still have its second-call slot empty — only one chat() call.
    assert provider._responses == []
    assert run.original_goal_summary  # cached on first injection
