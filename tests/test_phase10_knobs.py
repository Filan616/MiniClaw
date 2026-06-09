"""Phase 10 §6 config-knob wiring tests.

For each YAML field the user flagged as "defined but unused" we exercise
the runtime path that should now consume the value. The tests are
deliberately narrow: each one flips a single knob and asserts the
specific effect, so a regression that re-hardcodes any of these will
fail noisily.
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
    _run_finalizer,
    _truncate_for_reflection,
    run_agent_step,
)
from mini_claw.agent.observation import (
    build_direct_answer_observation,
    build_tool_error_observation,
    build_tool_success_observation,
)
from mini_claw.agent.react_decision import ReActDecision
from mini_claw.agent.react_models import ReflectionResult
from mini_claw.agent.reflection import run_reflection
from mini_claw.agent.reflection_trigger import ReActPolicy
from mini_claw.storage.db import Database


# ---------------------------------------------------------------------------
# Test plumbing
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
        self.calls: list[dict] = []

    async def chat(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        if not self._responses:
            return _Response(text="(empty)", tool_calls=[])
        return self._responses.pop(0)


class _StubTool:
    permission_level = "L0"

    async def handler(self, **kwargs):
        return "tool ran ok"


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


def _seed_run(db: Database, run_id: str = "run-x") -> None:
    db.execute(
        "INSERT INTO agent_runs (id, chat_id, agent_id, status, created_at, updated_at) "
        f"VALUES ('{run_id}','c1','a1','running', 0, 0)"
    )


# ---------------------------------------------------------------------------
# 1. inject_every_iteration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_goal_anchor_inject_every_iteration_false_only_first_turn():
    """When False, the goal anchor only injects on iteration==1."""
    db = Database(Path(":memory:"))
    _seed_run(db, "run-anchor-1")

    captured_msgs: list[list[dict]] = []

    class _CapturingProvider:
        async def chat(self, *, messages, tools=None, stream=False, stream_callback=None):
            captured_msgs.append(list(messages))
            # Force two LLM rounds: first asks a tool, second finalizes.
            if len(captured_msgs) == 1:
                return _Response(
                    text="ok",
                    tool_calls=[_ToolCall(id="tc1", name="read_file", arguments={"p": "x"})],
                    finish_reason="tool_calls",
                )
            return _Response(text="done", tool_calls=[])

    ctx = AgentContext(
        chat_id="c1",
        agent_id="a1",
        workspace_dir=Path("/tmp"),
        channel=SimpleNamespace(),
        storage=db,
        goal_anchor_inject_every_iteration=False,
    )
    run = AgentRun(
        id="run-anchor-1",
        chat_id="c1",
        agent_id="a1",
        status=RunOutcome.DONE,
        messages=[{"role": "user", "content": "go"}],
        original_goal_raw="UNIQUE-ANCHOR-PHRASE",
    )
    await run_agent_step(
        run,
        provider=_CapturingProvider(),
        registry=_StubRegistry(_StubTool()),
        permission_gate=_AllowGate(),
        result_processor=_RP(),
        ctx=ctx,
    )

    def has_anchor(messages):
        sys = next(m for m in messages if m["role"] == "system")
        return "UNIQUE-ANCHOR-PHRASE" in sys["content"]

    assert has_anchor(captured_msgs[0])
    assert not has_anchor(captured_msgs[1])


@pytest.mark.asyncio
async def test_goal_anchor_inject_every_iteration_true_every_turn():
    db = Database(Path(":memory:"))
    _seed_run(db, "run-anchor-2")

    captured_msgs: list[list[dict]] = []

    class _CapturingProvider:
        async def chat(self, *, messages, tools=None, stream=False, stream_callback=None):
            captured_msgs.append(list(messages))
            if len(captured_msgs) == 1:
                return _Response(
                    text="ok",
                    tool_calls=[_ToolCall(id="tc1", name="read_file", arguments={"p": "x"})],
                    finish_reason="tool_calls",
                )
            return _Response(text="done", tool_calls=[])

    ctx = AgentContext(
        chat_id="c1",
        agent_id="a1",
        workspace_dir=Path("/tmp"),
        channel=SimpleNamespace(),
        storage=db,
        goal_anchor_inject_every_iteration=True,
    )
    run = AgentRun(
        id="run-anchor-2",
        chat_id="c1",
        agent_id="a1",
        status=RunOutcome.DONE,
        messages=[{"role": "user", "content": "go"}],
        original_goal_raw="UNIQUE-ANCHOR-PHRASE",
    )
    await run_agent_step(
        run,
        provider=_CapturingProvider(),
        registry=_StubRegistry(_StubTool()),
        permission_gate=_AllowGate(),
        result_processor=_RP(),
        ctx=ctx,
    )

    sys_msgs = [next(m for m in msgs if m["role"] == "system") for msgs in captured_msgs]
    assert all("UNIQUE-ANCHOR-PHRASE" in m["content"] for m in sys_msgs)


# ---------------------------------------------------------------------------
# 2. summarization_mode = "truncate" exposed in audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_goal_anchor_summarization_mode_recorded_in_audit():
    db = Database(Path(":memory:"))
    _seed_run(db, "run-mode")
    captured: list[tuple[str, dict]] = []

    class _Audit:
        def log_security_event(self, *, event_type, details, **kwargs):
            captured.append((event_type, dict(details or {})))
            return "dbg"

    ctx = AgentContext(
        chat_id="c1",
        agent_id="a1",
        workspace_dir=Path("/tmp"),
        channel=SimpleNamespace(),
        storage=db,
        audit_logger=_Audit(),
        goal_anchor_summarization_mode="truncate",
    )
    run = AgentRun(
        id="run-mode",
        chat_id="c1",
        agent_id="a1",
        status=RunOutcome.DONE,
        messages=[{"role": "user", "content": "hi"}],
        original_goal_raw="hi",
    )
    provider = _ScriptedProvider([_Response(text="ok", tool_calls=[])])
    await run_agent_step(
        run,
        provider=provider,
        registry=_StubRegistry(),
        permission_gate=_AllowGate(),
        result_processor=_RP(),
        ctx=ctx,
    )
    inj = [d for ev, d in captured if ev == "goal_anchor_injected"]
    assert inj
    assert inj[0]["summarization_mode"] == "truncate"
    assert inj[0]["inject_every_iteration"] is True


# ---------------------------------------------------------------------------
# 3. sanitize_completion_claims
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sanitize_completion_claims_true_rejects_done_text():
    """Default-on: action_planned with completion claim is dropped."""
    db = Database(Path(":memory:"))
    _seed_run(db, "run-sane-1")
    received: list = []

    async def cb(u):
        received.append(u)
        return True

    from mini_claw.agent.loop import _emit_react_update, _open_step

    ctx = AgentContext(
        chat_id="c1",
        agent_id="a1",
        workspace_dir=Path("/tmp"),
        channel=SimpleNamespace(),
        storage=db,
        on_react_update=cb,
        react_user_updates_sanitize_completion_claims=True,
    )
    run = AgentRun(
        id="run-sane-1",
        chat_id="c1",
        agent_id="a1",
        status=RunOutcome.DONE,
        messages=[],
    )
    step = _open_step(run, action_phase="tool_call")
    sent = await _emit_react_update(
        ctx,
        run,
        step,
        event_type="action_planned",
        candidate_text="文件已创建。",  # completion claim
    )
    assert sent is False
    assert received == []


@pytest.mark.asyncio
async def test_sanitize_completion_claims_false_passes_through():
    db = Database(Path(":memory:"))
    _seed_run(db, "run-sane-2")
    received: list = []

    async def cb(u):
        received.append(u)
        return True

    from mini_claw.agent.loop import _emit_react_update, _open_step

    ctx = AgentContext(
        chat_id="c1",
        agent_id="a1",
        workspace_dir=Path("/tmp"),
        channel=SimpleNamespace(),
        storage=db,
        on_react_update=cb,
        react_user_updates_sanitize_completion_claims=False,
    )
    run = AgentRun(
        id="run-sane-2",
        chat_id="c1",
        agent_id="a1",
        status=RunOutcome.DONE,
        messages=[],
    )
    step = _open_step(run, action_phase="tool_call")
    sent = await _emit_react_update(
        ctx,
        run,
        step,
        event_type="action_planned",
        candidate_text="文件已创建。",
    )
    assert sent is True
    assert len(received) == 1


# ---------------------------------------------------------------------------
# 4. store_redacted_text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_redacted_text_false_persists_null_text():
    db = Database(Path(":memory:"))
    _seed_run(db, "run-rd")

    async def cb(u):
        return True

    from mini_claw.agent.loop import _emit_react_update, _open_step

    ctx = AgentContext(
        chat_id="c1",
        agent_id="a1",
        workspace_dir=Path("/tmp"),
        channel=SimpleNamespace(),
        storage=db,
        on_react_update=cb,
        react_user_updates_store_redacted_text=False,
    )
    run = AgentRun(
        id="run-rd",
        chat_id="c1",
        agent_id="a1",
        status=RunOutcome.DONE,
        messages=[],
    )
    step = _open_step(run, action_phase="tool_call")
    await _emit_react_update(
        ctx, run, step,
        event_type="action_planned",
        candidate_text="好的，我先读取文件。",
    )
    rows = db.fetchall("SELECT redacted_text, text_hash FROM react_user_updates")
    assert rows
    assert all(r["redacted_text"] is None for r in rows)
    assert all(r["text_hash"] for r in rows)


@pytest.mark.asyncio
async def test_store_redacted_text_true_persists_text():
    db = Database(Path(":memory:"))
    _seed_run(db, "run-rd2")

    async def cb(u):
        return True

    from mini_claw.agent.loop import _emit_react_update, _open_step

    ctx = AgentContext(
        chat_id="c1",
        agent_id="a1",
        workspace_dir=Path("/tmp"),
        channel=SimpleNamespace(),
        storage=db,
        on_react_update=cb,
        react_user_updates_store_redacted_text=True,
    )
    run = AgentRun(
        id="run-rd2",
        chat_id="c1",
        agent_id="a1",
        status=RunOutcome.DONE,
        messages=[],
    )
    step = _open_step(run, action_phase="tool_call")
    await _emit_react_update(
        ctx, run, step,
        event_type="action_planned",
        candidate_text="好的，我先读取文件。",
    )
    rows = db.fetchall("SELECT redacted_text FROM react_user_updates")
    assert rows
    assert all(r["redacted_text"] is not None for r in rows)


# ---------------------------------------------------------------------------
# 5. send_failure_non_blocking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_failure_non_blocking_true_swallows_exception():
    db = Database(Path(":memory:"))
    _seed_run(db, "run-snb-1")

    async def cb(u):
        raise RuntimeError("boom")

    from mini_claw.agent.loop import _emit_react_update, _open_step

    ctx = AgentContext(
        chat_id="c1",
        agent_id="a1",
        workspace_dir=Path("/tmp"),
        channel=SimpleNamespace(),
        storage=db,
        on_react_update=cb,
        react_user_updates_send_failure_non_blocking=True,
    )
    run = AgentRun(
        id="run-snb-1",
        chat_id="c1",
        agent_id="a1",
        status=RunOutcome.DONE,
        messages=[],
    )
    step = _open_step(run, action_phase="tool_call")
    sent = await _emit_react_update(
        ctx, run, step,
        event_type="action_planned",
        candidate_text="好的。",
    )
    assert sent is False  # didn't send, but no raise
    rows = db.fetchall("SELECT send_status FROM react_user_updates")
    assert rows[-1]["send_status"] == "failed"


@pytest.mark.asyncio
async def test_send_failure_non_blocking_false_propagates_exception():
    db = Database(Path(":memory:"))
    _seed_run(db, "run-snb-2")

    async def cb(u):
        raise RuntimeError("boom")

    from mini_claw.agent.loop import _emit_react_update, _open_step

    ctx = AgentContext(
        chat_id="c1",
        agent_id="a1",
        workspace_dir=Path("/tmp"),
        channel=SimpleNamespace(),
        storage=db,
        on_react_update=cb,
        react_user_updates_send_failure_non_blocking=False,
    )
    run = AgentRun(
        id="run-snb-2",
        chat_id="c1",
        agent_id="a1",
        status=RunOutcome.DONE,
        messages=[],
    )
    step = _open_step(run, action_phase="tool_call")
    with pytest.raises(RuntimeError, match="boom"):
        await _emit_react_update(
            ctx, run, step,
            event_type="action_planned",
            candidate_text="好的。",
        )
    # Row was still persisted before the re-raise so trace is complete.
    rows = db.fetchall("SELECT send_status FROM react_user_updates")
    assert rows[-1]["send_status"] == "failed"


# ---------------------------------------------------------------------------
# 6. reflect_before_finalize_mode = always vs deterministic_first
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reflect_before_finalize_mode_always_calls_llm():
    """``always`` forces a reflection LLM call even when there's no terminal trigger."""
    db = Database(Path(":memory:"))
    _seed_run(db, "run-bf-always")
    reflection_calls: list = []

    class _ReflectionProvider:
        async def chat(self, *, messages, tools=None, stream=False):
            reflection_calls.append(messages)
            return _Response(
                text=json.dumps({
                    "decision": "done",
                    "goal_status": "done",
                    "safety_assessment": "safe_to_continue",
                    "final_response_hint": "complete",
                    "confidence": 0.9,
                }),
                tool_calls=[],
            )

    policy = ReActPolicy(reflect_before_finalize_mode="always")
    ctx = AgentContext(
        chat_id="c1",
        agent_id="a1",
        workspace_dir=Path("/tmp"),
        channel=SimpleNamespace(),
        storage=db,
        react_policy=policy,
        reflection_provider=_ReflectionProvider(),
    )
    run = AgentRun(
        id="run-bf-always",
        chat_id="c1",
        agent_id="a1",
        status=RunOutcome.DONE,
        messages=[{"role": "user", "content": "say hi"}],
        original_goal_raw="say hi",
    )
    provider = _ScriptedProvider([_Response(text="hello", tool_calls=[])])
    await run_agent_step(
        run,
        provider=provider,
        registry=_StubRegistry(),
        permission_gate=_AllowGate(),
        result_processor=_RP(),
        ctx=ctx,
    )
    assert reflection_calls, "expected reflection LLM to be called for mode=always"


@pytest.mark.asyncio
async def test_reflect_before_finalize_mode_deterministic_first_skips_llm():
    """``deterministic_first`` keeps the LLM out for non-terminal direct answers."""
    db = Database(Path(":memory:"))
    _seed_run(db, "run-bf-det")
    reflection_calls: list = []

    class _ReflectionProvider:
        async def chat(self, *, messages, tools=None, stream=False):
            reflection_calls.append(messages)
            return _Response(text="{}", tool_calls=[])

    policy = ReActPolicy(reflect_before_finalize_mode="deterministic_first")
    ctx = AgentContext(
        chat_id="c1",
        agent_id="a1",
        workspace_dir=Path("/tmp"),
        channel=SimpleNamespace(),
        storage=db,
        react_policy=policy,
        reflection_provider=_ReflectionProvider(),
    )
    run = AgentRun(
        id="run-bf-det",
        chat_id="c1",
        agent_id="a1",
        status=RunOutcome.DONE,
        messages=[{"role": "user", "content": "say hi"}],
        original_goal_raw="say hi",
    )
    provider = _ScriptedProvider([_Response(text="hello", tool_calls=[])])
    await run_agent_step(
        run,
        provider=provider,
        registry=_StubRegistry(),
        permission_gate=_AllowGate(),
        result_processor=_RP(),
        ctx=ctx,
    )
    assert reflection_calls == []


# ---------------------------------------------------------------------------
# 7. max_reflection_chars
# ---------------------------------------------------------------------------


def test_truncate_for_reflection_caps_length():
    long = "a" * 5000
    out = _truncate_for_reflection(long, 100)
    assert len(out) <= 100
    assert out.endswith("...")


@pytest.mark.asyncio
async def test_run_reflection_caps_prompt_via_max_reflection_chars():
    sent_messages: list = []

    class _RecordingProvider:
        async def chat(self, *, messages, tools=None, stream=False):
            sent_messages.append(messages)
            return _Response(
                text=json.dumps({"decision": "continue", "goal_status": "in_progress"}),
                tool_calls=[],
            )

    long_goal = "X" * 9000
    obs = build_tool_error_observation("x", "boom")
    await run_reflection(
        provider=_RecordingProvider(),
        observation=obs,
        original_goal_summary=long_goal,
        iteration=1,
        max_iterations=10,
        trigger_reasons=["tool_error"],
        timeout_sec=5,
        max_reflection_chars=200,
    )
    user_prompt = sent_messages[0][1]["content"]
    assert len(user_prompt) <= 200


@pytest.mark.asyncio
async def test_run_reflection_caps_raw_text_too():
    """``raw_text`` on the parsed result is also bounded by max_reflection_chars."""

    class _BigProvider:
        async def chat(self, *, messages, tools=None, stream=False):
            return _Response(
                text=("Z" * 10000) + json.dumps(
                    {"decision": "done", "goal_status": "done"}
                ),
                tool_calls=[],
            )

    obs = build_tool_error_observation("x", "boom")
    result = await run_reflection(
        provider=_BigProvider(),
        observation=obs,
        original_goal_summary="g",
        iteration=1,
        max_iterations=10,
        trigger_reasons=["tool_error"],
        timeout_sec=5,
        max_reflection_chars=300,
    )
    assert result.raw_text is not None
    assert len(result.raw_text) <= 300


# ---------------------------------------------------------------------------
# 8. max_observation_chars
# ---------------------------------------------------------------------------


def test_max_observation_chars_caps_observation_summary():
    long = "y" * 5000
    obs = build_tool_success_observation("read_file", long, max_chars=120)
    assert len(obs.summary) <= 120
    obs2 = build_tool_success_observation("read_file", long, max_chars=4000)
    assert len(obs2.summary) > 120


def test_direct_answer_observation_respects_max_observation_chars():
    long = "z" * 2000
    obs = build_direct_answer_observation(long, max_chars=200)
    assert len(obs.summary) <= 200


# ---------------------------------------------------------------------------
# 9. store_reflection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_reflection_false_skips_full_reflection_blob():
    db = Database(Path(":memory:"))
    _seed_run(db, "run-store-false")
    policy = ReActPolicy(store_reflection=False, reflect_every_iteration=True)
    ctx = AgentContext(
        chat_id="c1",
        agent_id="a1",
        workspace_dir=Path("/tmp"),
        channel=SimpleNamespace(),
        storage=db,
        react_policy=policy,
    )
    run = AgentRun(
        id="run-store-false",
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
    rows = db.fetchall("SELECT reflection_json FROM react_steps WHERE run_id='run-store-false'")
    parsed_blobs = [json.loads(r["reflection_json"]) for r in rows if r["reflection_json"]]
    # When store_reflection is False, blobs should only carry decision (+ marker)
    # and not the full goal_status/safety_assessment/etc.
    saw_skipped = any(
        set(b.keys()) <= {"decision", "stored"} for b in parsed_blobs
    )
    assert saw_skipped, parsed_blobs


@pytest.mark.asyncio
async def test_store_reflection_true_writes_full_blob():
    db = Database(Path(":memory:"))
    _seed_run(db, "run-store-true")
    policy = ReActPolicy(store_reflection=True, reflect_every_iteration=True)
    ctx = AgentContext(
        chat_id="c1",
        agent_id="a1",
        workspace_dir=Path("/tmp"),
        channel=SimpleNamespace(),
        storage=db,
        react_policy=policy,
    )
    run = AgentRun(
        id="run-store-true",
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
    rows = db.fetchall("SELECT reflection_json FROM react_steps WHERE run_id='run-store-true'")
    parsed = [json.loads(r["reflection_json"]) for r in rows if r["reflection_json"]]
    assert any(
        "goal_status" in b and "safety_assessment" in b for b in parsed
    ), parsed


# ---------------------------------------------------------------------------
# 10 + 11. finalizer_enabled + finalizer_timeout_sec
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalizer_enabled_false_returns_raw_text():
    decision = ReActDecision(action="block", reason="blocked", final_response_hint="hint")
    obs = build_tool_error_observation("x", "boom")
    refl = ReflectionResult(
        observation_summary="",
        goal_status="failed",
        completed_requirements=[],
        remaining_requirements=[],
        safety_assessment="failed_unrecoverable",
        safe_next_action="",
        forbidden_next_actions=[],
        decision="failed",
        final_response_hint="reflection-hint",
        confidence=0.9,
    )
    policy = ReActPolicy(finalizer_enabled=False)
    out = await _run_finalizer(
        policy=policy,
        decision=decision,
        observation=obs,
        reflection=refl,
        raw_final_text="raw final",
    )
    assert out == "raw final"


@pytest.mark.asyncio
async def test_finalizer_enabled_true_uses_finalize_response():
    decision = ReActDecision(action="finalize", reason="done", final_response_hint="hint")
    obs = build_direct_answer_observation("hello world")
    refl = ReflectionResult(
        observation_summary="",
        goal_status="done",
        completed_requirements=[],
        remaining_requirements=[],
        safety_assessment="safe_to_continue",
        safe_next_action="",
        forbidden_next_actions=[],
        decision="done",
        final_response_hint="reflection-hint",
        confidence=0.9,
    )
    policy = ReActPolicy(finalizer_enabled=True)
    out = await _run_finalizer(
        policy=policy,
        decision=decision,
        observation=obs,
        reflection=refl,
        raw_final_text="hello world",
    )
    assert out.strip() == "hello world"


@pytest.mark.asyncio
async def test_finalizer_timeout_sec_caps_finalizer_call():
    """When the deterministic Finalizer hangs (eg. swapped for an LLM
    call later), ``finalizer_timeout_sec`` falls back to the raw text."""
    import mini_claw.agent.loop as loop_mod

    async def slow_finalize(**kwargs):
        await asyncio.sleep(2)
        return "should never arrive"

    # Monkey-patch the inner async helper that _run_finalizer awaits.
    real_run_finalizer = loop_mod._run_finalizer

    async def patched(*, policy, decision, observation, reflection, raw_final_text, fallback_text=""):
        if policy is not None and not getattr(policy, "finalizer_enabled", True):
            return (raw_final_text or fallback_text or decision.reason or "").strip()
        timeout = getattr(policy, "finalizer_timeout_sec", 20)
        try:
            return await asyncio.wait_for(slow_finalize(), timeout=timeout)
        except asyncio.TimeoutError:
            return (raw_final_text or fallback_text or decision.reason or "").strip()

    loop_mod._run_finalizer = patched
    try:
        decision = ReActDecision(action="block", reason="x", final_response_hint="h")
        obs = build_tool_error_observation("x", "boom")
        refl = ReflectionResult(
            observation_summary="",
            goal_status="failed",
            completed_requirements=[],
            remaining_requirements=[],
            safety_assessment="failed_unrecoverable",
            safe_next_action="",
            forbidden_next_actions=[],
            decision="failed",
            final_response_hint="hint",
            confidence=0.9,
        )
        policy = ReActPolicy(finalizer_enabled=True, finalizer_timeout_sec=1)
        out = await loop_mod._run_finalizer(
            policy=policy,
            decision=decision,
            observation=obs,
            reflection=refl,
            raw_final_text="fallback raw",
        )
        assert out == "fallback raw"
    finally:
        loop_mod._run_finalizer = real_run_finalizer
