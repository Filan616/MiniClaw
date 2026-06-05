"""Tests for the Agent Loop."""

import hashlib
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from mini_claw.agent.loop import (
    AgentRun,
    RunOutcome,
    _messages_for_provider,
    run_agent_step,
    MAX_ITERATIONS,
)
from mini_claw.agent.context import AgentContext
from mini_claw.providers.base import LLMResponse, ToolCall
from mini_claw.permissions.gate import Decision


@pytest.fixture
def mock_provider():
    provider = AsyncMock()
    return provider


@pytest.fixture
def mock_registry():
    registry = MagicMock()
    registry.schemas_for.return_value = [
        {"name": "run_shell", "description": "Run shell", "parameters": {}}
    ]
    return registry


@pytest.fixture
def mock_gate():
    gate = MagicMock()
    gate.evaluate.return_value = Decision(action="allow")
    return gate


@pytest.fixture
def mock_result_processor():
    proc = MagicMock()
    proc.process.side_effect = lambda r, _: r
    return proc


@pytest.fixture
def agent_run():
    return AgentRun(
        id="run_001",
        chat_id="chat_001",
        agent_id="default",
        status="running",
        messages=[{"role": "user", "content": "list files"}],
        iterations=0,
        seen_calls=set(),
        allowed_tools=["run_shell", "read_file"],
    )


@pytest.fixture
def ctx():
    from pathlib import Path
    return AgentContext(
        chat_id="chat_001",
        agent_id="default",
        workspace_dir=Path("."),
        channel=MagicMock(),
    )


def test_messages_include_current_time_context(agent_run, ctx):
    messages = _messages_for_provider(agent_run, ctx)

    assert messages[0]["role"] == "system"
    assert "[Current Time]" in messages[0]["content"]
    assert "current_time" in messages[0]["content"]
    assert "日报" in messages[0]["content"]


@pytest.mark.asyncio
async def test_agent_loop_converges(
    agent_run, mock_provider, mock_registry, mock_gate, mock_result_processor, ctx
):
    mock_provider.chat.return_value = LLMResponse(
        text="Here are the files in the directory.",
        tool_calls=[],
        finish_reason="stop",
        raw={},
    )
    result = await run_agent_step(
        agent_run, mock_provider, mock_registry, mock_gate, mock_result_processor, ctx
    )
    assert result.status == RunOutcome.DONE
    assert result.final_answer == "Here are the files in the directory."


@pytest.mark.asyncio
async def test_agent_loop_disables_streaming_when_tools_available(
    agent_run, mock_provider, mock_registry, mock_gate, mock_result_processor, ctx
):
    """Tool calls should use non-streaming completions so arguments arrive intact."""
    ctx.channel.send_stream_chunk = AsyncMock()
    mock_provider.chat.return_value = LLMResponse(
        text="Done",
        tool_calls=[],
        finish_reason="stop",
        raw={},
    )

    result = await run_agent_step(
        agent_run, mock_provider, mock_registry, mock_gate, mock_result_processor, ctx
    )

    assert result.status == RunOutcome.DONE
    _, kwargs = mock_provider.chat.call_args
    assert kwargs["tools"] is not None
    assert kwargs["stream"] is False
    assert kwargs["stream_callback"] is None


@pytest.mark.asyncio
async def test_agent_loop_max_iterations(
    agent_run, mock_provider, mock_registry, mock_gate, mock_result_processor, ctx
):
    mock_provider.chat.return_value = LLMResponse(
        text="",
        tool_calls=[ToolCall(id="call_1", name="run_shell", arguments={"cmd": "ls"})],
        finish_reason="tool_calls",
        raw={},
    )
    tool = MagicMock()
    tool.name = "run_shell"
    tool.permission_level = "L2"
    tool.handler = AsyncMock(return_value="file1.py\nfile2.py")
    mock_registry.get.return_value = tool

    result = await run_agent_step(
        agent_run, mock_provider, mock_registry, mock_gate, mock_result_processor, ctx
    )
    assert result.status == RunOutcome.ABORTED
    assert result.iterations == MAX_ITERATIONS
    # Phase 9 fix: ensure ABORTED runs have a meaningful final_answer
    assert result.final_answer is not None
    assert "轮对话" in result.final_answer or "iterations" in result.final_answer.lower()
    assert len(result.final_answer) > 20  # Not empty or trivial


@pytest.mark.asyncio
async def test_agent_loop_duplicate_detection(
    agent_run, mock_provider, mock_registry, mock_gate, mock_result_processor, ctx
):
    call = ToolCall(id="call_1", name="run_shell", arguments={"cmd": "ls"})
    # Match the loop's _call_signature scheme: md5(json.dumps({name, args}, sort_keys=True))
    sig = hashlib.md5(
        json.dumps({"name": call.name, "args": call.arguments}, sort_keys=True).encode()
    ).hexdigest()
    agent_run.seen_calls.add(sig)

    responses = [
        LLMResponse(text="", tool_calls=[call], finish_reason="tool_calls", raw={}),
        LLMResponse(text="Done", tool_calls=[], finish_reason="stop", raw={}),
    ]
    mock_provider.chat.side_effect = responses

    tool = MagicMock()
    tool.name = "run_shell"
    tool.permission_level = "L2"
    mock_registry.get.return_value = tool

    result = await run_agent_step(
        agent_run, mock_provider, mock_registry, mock_gate, mock_result_processor, ctx
    )
    assert result.status == RunOutcome.DONE


@pytest.mark.asyncio
async def test_parallel_precheck_splits_by_evaluate_not_metadata(
    agent_run, mock_provider, mock_registry, mock_result_processor, ctx
):
    """Phase 0.7: parallel/sequential split must call evaluate upfront.

    Two L0 calls are issued: list_directory(".") and list_directory(".ssh").
    Both are L0 metadata, but the .ssh one is denied by evaluate (sensitive
    path). Only the first should go parallel; the second must be rejected in
    the sequential path with proper obfuscation.
    """
    from pathlib import Path

    # Two L0 calls
    call_ok = ToolCall(id="call_1", name="list_directory", arguments={"path": "."})
    call_ssh = ToolCall(id="call_2", name="list_directory", arguments={"path": ".ssh"})

    responses = [
        LLMResponse(text="", tool_calls=[call_ok, call_ssh], finish_reason="tool_calls", raw={}),
        LLMResponse(text="Listed", tool_calls=[], finish_reason="stop", raw={}),
    ]
    mock_provider.chat.side_effect = responses

    tool_list_dir = MagicMock()
    tool_list_dir.name = "list_directory"
    tool_list_dir.permission_level = "L0"
    tool_list_dir.handler = AsyncMock(return_value="file1\nfile2")
    mock_registry.get.return_value = tool_list_dir

    # Mock gate: allow for ".", deny for ".ssh"
    def evaluate_side_effect(**kwargs):
        args = kwargs.get("args", {})
        if args.get("path") == ".ssh":
            return Decision(action="deny", reason="Access denied", internal_reason="sensitive")
        return Decision(action="allow")

    mock_gate = MagicMock()
    mock_gate.evaluate.side_effect = evaluate_side_effect

    # Workspace with .ssh dir
    ctx.workspace_dir = Path(".")
    ctx.sandbox_mode = "safe"

    result = await run_agent_step(
        agent_run, mock_provider, mock_registry, mock_gate, mock_result_processor, ctx
    )
    assert result.status == RunOutcome.DONE

    # Check messages: call_1 (.) should succeed, call_2 (.ssh) should be denied
    tool_results = [m for m in result.messages if m.get("role") == "tool"]
    assert len(tool_results) == 2

    ok_result = next(m for m in tool_results if m["tool_call_id"] == "call_1")
    assert "file1" in ok_result["content"] or "[denied]" not in ok_result["content"]

    ssh_result = next(m for m in tool_results if m["tool_call_id"] == "call_2")
    assert "[denied]" in ssh_result["content"]
