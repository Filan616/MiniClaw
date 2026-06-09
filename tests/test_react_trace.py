"""Phase 10 M10.4: RunTraceView tests."""

from pathlib import Path

import pytest

from mini_claw.agent.trace import (
    build_run_trace,
    render_trace_text,
)
from mini_claw.gateway.session import SessionManager
from mini_claw.storage.db import Database


@pytest.fixture()
def db():
    return Database(Path(":memory:"))


def _seed_run(db: Database, *, run_id: str = "r1", goal: str | None = None) -> None:
    import time

    now = int(time.time())
    db.execute(
        "INSERT INTO agent_runs (id, chat_id, agent_id, status, user_message, "
        "final_answer, iterations, original_goal_raw, original_goal_summary, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            "chat1",
            "agent1",
            "done",
            goal or "test",
            "OK",
            2,
            goal,
            goal,
            now,
            now,
        ),
    )


def _seed_react_step(
    db: Database,
    *,
    run_id: str,
    iteration: int,
    action_phase: str,
    decision: str,
    status: str = "completed",
) -> str:
    import json
    import time

    step_id = f"rs-{iteration}"
    now = int(time.time())
    db.execute(
        "INSERT INTO react_steps "
        "(step_id, run_id, chat_id, agent_id, iteration, action_phase, "
        " tool_calls_json, observation_json, reflection_json, "
        " reflection_triggered, reflection_reasons_json, decision, status, "
        " created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            step_id,
            run_id,
            "chat1",
            "agent1",
            iteration,
            action_phase,
            json.dumps([{"name": "read_file"}]),
            json.dumps({"summary": "ok"}),
            json.dumps({"decision": decision}),
            1,
            json.dumps(["before_finalize"]),
            decision,
            status,
            now,
            now,
        ),
    )
    return step_id


def test_build_trace_returns_none_for_unknown_run(db):
    assert build_run_trace(db, "nope") is None


def test_build_trace_basic_run(db):
    _seed_run(db, run_id="run-a", goal="读 README")
    _seed_react_step(db, run_id="run-a", iteration=1, action_phase="tool_call", decision="continue", status="observed")
    _seed_react_step(db, run_id="run-a", iteration=2, action_phase="direct_answer", decision="finalize")

    trace = build_run_trace(db, "run-a")
    assert trace is not None
    assert trace.run_id == "run-a"
    assert trace.original_goal == "读 README"
    assert len(trace.steps) == 2
    assert trace.steps[0].iteration == 1
    assert trace.steps[1].decision == "finalize"
    assert trace.steps[0].tool_calls_summary == ["read_file"]


def test_build_trace_legacy_prelude_mapped(db):
    _seed_run(db, run_id="run-legacy")
    sm = SessionManager(db)
    sm.store_message(
        chat_id="chat1",
        agent_id="agent1",
        role="assistant",
        content="好的，我先读取这个文件。",
        run_id="run-legacy",
        channel_name="feishu",
        workspace_dir="/tmp",
        message_kind="prelude",
    )

    trace = build_run_trace(db, "run-legacy")
    assert trace is not None
    assert trace.steps, "legacy prelude should produce a synthetic step"
    legacy_updates = [
        upd for step in trace.steps for upd in step.raw_updates if upd.legacy
    ]
    assert legacy_updates, "should include at least one legacy prelude update"
    assert legacy_updates[0].event_type == "action_planned"


def test_render_trace_text_smoke(db):
    _seed_run(db, run_id="run-r", goal="hello")
    _seed_react_step(db, run_id="run-r", iteration=1, action_phase="direct_answer", decision="finalize")
    trace = build_run_trace(db, "run-r")
    rendered = render_trace_text(trace)
    assert "Run: run-r" in rendered
    assert "Original Goal: hello" in rendered
    assert "Step 1" in rendered
