"""Tests for the Agent Loop."""

import hashlib
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from mini_claw.agent.loop import AgentRun, RunOutcome, run_agent_step, MAX_ITERATIONS
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


@pytest.mark.asyncio
async def test_agent_loop_duplicate_detection(
    agent_run, mock_provider, mock_registry, mock_gate, mock_result_processor, ctx
):
    call = ToolCall(id="call_1", name="run_shell", arguments={"cmd": "ls"})
    sig = hashlib.md5(
        f"{call.name}:{json.dumps(call.arguments, sort_keys=True)}".encode()
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
