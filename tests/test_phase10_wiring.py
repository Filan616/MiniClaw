"""Phase 10 wiring tests — ensures the new flow really uses
``message_kind='react_update'`` and approval resume creates a fresh step.

These tests exercise the *integration* surface: SessionManager mirror,
get_history filter, count_messages exclusion, and the loop/resume
behaviour around react_steps.
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
    resume_after_approval,
    run_agent_step,
)
from mini_claw.agent.react_models import ReActUserUpdate
from mini_claw.agent.react_update import hash_text
from mini_claw.gateway.session import SessionManager
from mini_claw.storage.db import Database


# ---------------------------------------------------------------------------
# SessionManager: react_update never bleeds into context / count / search
# ---------------------------------------------------------------------------


def _seed_message(
    db: Database,
    *,
    chat_id: str = "c1",
    agent_id: str = "a1",
    role: str = "assistant",
    content: str = "hi",
    message_kind: str = "normal",
    metadata: dict | None = None,
) -> int | None:
    sm = SessionManager(db)
    return sm.store_message(
        chat_id=chat_id,
        agent_id=agent_id,
        role=role,
        content=content,
        run_id="r1",
        channel_name="feishu",
        workspace_dir="/tmp",
        message_kind=message_kind,
        metadata=metadata,
    )


def test_store_message_accepts_metadata_json():
    db = Database(Path(":memory:"))
    msg_id = _seed_message(
        db,
        message_kind="react_update",
        metadata={
            "react_update_id": "u1",
            "react_step_id": "s1",
            "react_event_type": "action_planned",
            "visible_level": "normal",
            "is_important": False,
        },
    )
    row = db.fetchone("SELECT metadata_json, message_kind FROM messages WHERE id=?", (msg_id,))
    assert row["message_kind"] == "react_update"
    parsed = json.loads(row["metadata_json"])
    assert parsed["react_update_id"] == "u1"
    assert parsed["react_event_type"] == "action_planned"


def test_get_history_filters_react_updates_by_default():
    db = Database(Path(":memory:"))
    sm = SessionManager(db)
    _seed_message(db, role="user", content="hello", message_kind="normal")
    _seed_message(db, role="assistant", content="好的，我先读取这个文件", message_kind="react_update")
    _seed_message(db, role="assistant", content="ok done", message_kind="normal")

    history = sm.get_history("c1", "a1", channel_name="feishu")
    contents = [m.get("content") for m in history]
    assert "hello" in contents
    assert "ok done" in contents
    assert "好的，我先读取这个文件" not in contents

    full = sm.get_history("c1", "a1", channel_name="feishu", include_react_updates=True)
    full_contents = [m.get("content") for m in full]
    assert "好的，我先读取这个文件" in full_contents


def test_count_messages_excludes_react_updates():
    db = Database(Path(":memory:"))
    sm = SessionManager(db)
    _seed_message(db, role="user", content="hello", message_kind="normal")
    _seed_message(db, role="assistant", content="upd", message_kind="react_update")
    _seed_message(db, role="assistant", content="prelude legacy", message_kind="prelude")

    assert sm.count_messages("c1", "a1", channel_name="feishu") == 1


def test_chat_search_like_excludes_process_messages():
    db = Database(Path(":memory:"))
    _seed_message(db, role="assistant", content="readme content", message_kind="normal")
    _seed_message(db, role="assistant", content="readme update", message_kind="react_update")

    from mini_claw.chat_search.retriever import ChatSearchRetriever

    retr = ChatSearchRetriever(db, config={})
    results = retr._search_like(
        "readme",
        where_clause="messages.channel_name = ?",
        params=("feishu",),
        top_k=10,
    )
    contents = [r["content"] for r in results]
    assert "readme content" in contents
    assert "readme update" not in contents


# ---------------------------------------------------------------------------
# AgentLoop: ReActUserUpdate mirror + approval resume produces new step
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


class _GrantingTool:
    def __init__(self):
        self.permission_level = "L0"
        self.calls: list[dict] = []

    async def handler(self, **kwargs):
        ctx = kwargs.pop("ctx", None)
        self.calls.append(kwargs)
        return "tool ran"


class _SingleToolRegistry:
    def __init__(self, tool_name: str, tool: Any):
        self._tools = {tool_name: tool}

    def schemas_for(self, allowed):
        return []

    def get(self, name):
        return self._tools.get(name)


@pytest.mark.asyncio
async def test_react_update_callback_drives_persistence_and_step_link():
    """When the loop emits an action_planned update, the row hits both
    react_user_updates and the user_updates_json on the corresponding step."""
    db = Database(Path(":memory:"))
    db.execute(
        "INSERT INTO agent_runs (id, chat_id, agent_id, status, created_at, updated_at) "
        "VALUES ('run-x','c1','a1','running', 0, 0)"
    )

    received: list[ReActUserUpdate] = []

    async def cb(update: ReActUserUpdate) -> bool:
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
        id="run-x",
        chat_id="c1",
        agent_id="a1",
        status=RunOutcome.DONE,
        messages=[{"role": "user", "content": "帮我读文件"}],
        original_goal_raw="帮我读文件",
    )
    provider = _ScriptedProvider(
        [
            _Response(
                text="好的，我先读取这个文件。",
                tool_calls=[_ToolCall(id="tc1", name="read_file", arguments={"path": "x"})],
                finish_reason="tool_calls",
            ),
            _Response(text="完成", tool_calls=[]),
        ]
    )

    await run_agent_step(
        run,
        provider=provider,
        registry=_StubRegistry(),
        permission_gate=_StubGate(),
        result_processor=SimpleNamespace(process=lambda r, n: r, process_error=lambda e: str(e)),
        ctx=ctx,
    )

    # action_planned reached the callback.
    assert received, "expected ReActUserUpdate to be delivered"
    upd = received[0]
    assert upd.event_type == "action_planned"
    assert upd.text_hash == hash_text(upd.text)
    # And was persisted into react_user_updates by the loop's safety net.
    rows = db.fetchall("SELECT update_id, step_id, event_type, text_hash FROM react_user_updates")
    assert any(r["update_id"] == upd.update_id for r in rows)
    # Step references the update via user_updates_json.
    step_row = db.fetchone(
        "SELECT user_updates_json FROM react_steps WHERE step_id=?",
        (upd.step_id,),
    )
    assert step_row is not None
    payload = json.loads(step_row["user_updates_json"])
    assert any(item["update_id"] == upd.update_id for item in payload)


@pytest.mark.asyncio
async def test_resume_after_approval_creates_new_consecutive_step():
    """Approve resume creates a *new* step, iteration counter monotonic."""
    db = Database(Path(":memory:"))
    db.execute(
        "INSERT INTO agent_runs (id, chat_id, agent_id, status, created_at, updated_at) "
        "VALUES ('run-y','c1','a1','running', 0, 0)"
    )

    tool = _GrantingTool()
    registry = _SingleToolRegistry("write_file", tool)
    ctx = AgentContext(
        chat_id="c1",
        agent_id="a1",
        workspace_dir=Path("/tmp"),
        channel=SimpleNamespace(),
        storage=db,
    )
    run = AgentRun(
        id="run-y",
        chat_id="c1",
        agent_id="a1",
        status=RunOutcome.SUSPENDED,
        messages=[{"role": "user", "content": "go"}],
        pending_approval_id="appr-1",
        pending_tool_call=json.dumps(
            {"id": "tc1", "name": "write_file", "arguments": {"path": "x", "content": "y"}, "level": "L3"}
        ),
        step_counter=1,  # simulate the approval_required step that already happened
    )

    # Provider returns immediate done after the resumed tool call.
    provider = _ScriptedProvider([_Response(text="ok", tool_calls=[])])

    await resume_after_approval(
        run,
        approval="approved",
        provider=provider,
        registry=registry,
        permission_gate=_StubGate(),
        result_processor=SimpleNamespace(process=lambda r, n: r, process_error=lambda e: str(e)),
        ctx=ctx,
    )

    rows = db.fetchall(
        "SELECT iteration, action_phase, decision FROM react_steps WHERE run_id='run-y' ORDER BY iteration ASC"
    )
    iterations = [r["iteration"] for r in rows]
    assert iterations == sorted(iterations), "iteration must be monotonic"
    # Step iteration 2 is the resumed tool_call.
    resumed = next(r for r in rows if r["iteration"] == 2)
    assert resumed["action_phase"] == "tool_call"
    assert resumed["decision"] in {"continue", "failed"}


@pytest.mark.asyncio
async def test_resume_after_approval_rejected_records_blocked_step():
    db = Database(Path(":memory:"))
    db.execute(
        "INSERT INTO agent_runs (id, chat_id, agent_id, status, created_at, updated_at) "
        "VALUES ('run-z','c1','a1','running', 0, 0)"
    )
    ctx = AgentContext(
        chat_id="c1",
        agent_id="a1",
        workspace_dir=Path("/tmp"),
        channel=SimpleNamespace(),
        storage=db,
    )
    run = AgentRun(
        id="run-z",
        chat_id="c1",
        agent_id="a1",
        status=RunOutcome.SUSPENDED,
        messages=[{"role": "user", "content": "go"}],
        pending_approval_id="appr-2",
        pending_tool_call=json.dumps(
            {"id": "tc1", "name": "write_file", "arguments": {"path": "x"}, "level": "L3"}
        ),
        step_counter=1,
    )
    provider = _ScriptedProvider([_Response(text="ok stopping", tool_calls=[])])
    await resume_after_approval(
        run,
        approval="rejected",
        provider=provider,
        registry=_StubRegistry(),
        permission_gate=_StubGate(),
        result_processor=SimpleNamespace(process=lambda r, n: r, process_error=lambda e: str(e)),
        ctx=ctx,
    )
    rejected = db.fetchone(
        "SELECT action_phase, decision, observation_json FROM react_steps "
        "WHERE run_id='run-z' AND action_phase='approval_rejected'"
    )
    assert rejected is not None
    assert rejected["decision"] == "blocked"
    obs = json.loads(rejected["observation_json"])
    assert obs["observation_type"] == "approval_rejected"


# ---------------------------------------------------------------------------
# /workflow inspect --trace
# ---------------------------------------------------------------------------


def test_render_workflow_trace_combines_node_runs():
    """The renderer walks workflow_nodes and joins each node's run trace."""
    from mini_claw.gateway.router import Gateway

    db = Database(Path(":memory:"))
    db.execute(
        "INSERT INTO workflow_runs "
        "(workflow_id, chat_id, agent_id, status, spec_json, created_at, updated_at) "
        "VALUES ('wf-1','c1','a1','done','{}', 0, 0)"
    )
    db.execute(
        "INSERT INTO workflow_nodes (workflow_id, node_id, status, agent_run_id) "
        "VALUES ('wf-1','impl','done','run-imp')"
    )
    db.execute(
        "INSERT INTO agent_runs (id, chat_id, agent_id, status, original_goal_raw, "
        "original_goal_summary, iterations, created_at, updated_at) "
        "VALUES ('run-imp','c1','a1','done','build it','build it', 1, 0, 0)"
    )
    db.execute(
        "INSERT INTO react_steps "
        "(step_id, run_id, chat_id, agent_id, iteration, action_phase, "
        " decision, status, created_at, updated_at) "
        "VALUES ('rs-1','run-imp','c1','a1', 1, 'tool_call', 'continue', 'observed', 0, 0)"
    )

    gw = object.__new__(Gateway)
    gw._storage = db  # type: ignore[attr-defined]
    # Stub workflow_store with a get_run/list_nodes only — that's all
    # _render_workflow_trace touches.
    gw._workflow_store = SimpleNamespace(
        get_run=lambda wid: db.fetchone(
            "SELECT * FROM workflow_runs WHERE workflow_id=?", (wid,)
        ),
        list_nodes=lambda wid: db.fetchall(
            "SELECT * FROM workflow_nodes WHERE workflow_id=? ORDER BY rowid",
            (wid,),
        ),
    )

    text = gw._render_workflow_trace("wf-1")
    assert "Workflow wf-1" in text
    assert "Node impl" in text
    assert "Step 1" in text


def test_render_workflow_trace_unknown_workflow():
    from mini_claw.gateway.router import Gateway

    db = Database(Path(":memory:"))
    gw = object.__new__(Gateway)
    gw._storage = db  # type: ignore[attr-defined]
    gw._workflow_store = SimpleNamespace(
        get_run=lambda wid: None,
        list_nodes=lambda wid: [],
    )
    text = gw._render_workflow_trace("nope")
    assert text == "Workflow not found: nope"
