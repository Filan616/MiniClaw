"""Tests for agent loop hallucination detection and retry logic.

This test suite verifies that when the LLM claims to have completed an action
(like creating a file) without actually calling the corresponding tool, the
loop detects this hallucination and forces a retry with a correction message.
"""

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
        {"name": "write_file", "description": "Write file", "parameters": {}}
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
        id="run_hallucination_test",
        chat_id="chat_001",
        agent_id="default",
        status="running",
        messages=[{"role": "user", "content": "请在当前目录创建文件 test.md"}],
        iterations=0,
        seen_calls=set(),
        allowed_tools=["write_file", "read_file"],
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
async def test_hallucination_detection_chinese(
    agent_run, mock_provider, mock_registry, mock_gate, mock_result_processor, ctx
):
    """Test that Chinese hallucination phrases ('已创建', '文件已') trigger retry."""

    # First response: hallucination (claims file created without tool call)
    hallucination_response = LLMResponse(
        text="好的，文件已创建完成！test.md 已写入成功。",
        tool_calls=[],
        finish_reason="stop",
        raw={},
    )

    # Second response after correction: actually calls write_file
    correct_response = LLMResponse(
        text="",
        tool_calls=[
            ToolCall(
                id="call_write_1",
                name="write_file",
                arguments={"path": "test.md", "content": "# Test\n"}
            )
        ],
        finish_reason="tool_calls",
        raw={},
    )

    # Third response: completion after tool execution (avoid triggering detection again)
    final_response = LLMResponse(
        text="好的。",
        tool_calls=[],
        finish_reason="stop",
        raw={},
    )

    mock_provider.chat.side_effect = [
        hallucination_response,  # Iteration 1: hallucination detected
        correct_response,         # Iteration 2: correct tool call after retry
        final_response,          # Iteration 2: final response after tool execution
    ]

    # Mock write_file tool
    tool = MagicMock()
    tool.name = "write_file"
    tool.permission_level = "L1"
    tool.handler = AsyncMock(return_value="Written 7 chars to test.md")
    mock_registry.get.return_value = tool

    result = await run_agent_step(
        agent_run, mock_provider, mock_registry, mock_gate, mock_result_processor, ctx
    )

    # Verify the loop detected hallucination and retried
    assert result.status == RunOutcome.DONE
    assert result.iterations >= 2  # At least 2 iterations (hallucination + correction)

    # Verify correction message was inserted
    correction_found = any(
        msg.get("role") == "user" and "[SYSTEM]" in msg.get("content", "")
        for msg in result.messages
    )
    assert correction_found, "Should insert [SYSTEM] correction message after hallucination"

    # Verify write_file was actually called
    assert tool.handler.call_count == 1


@pytest.mark.asyncio
async def test_hallucination_detection_english(
    agent_run, mock_provider, mock_registry, mock_gate, mock_result_processor, ctx
):
    """Test that English hallucination phrases ('created', 'successfully written') trigger retry."""

    agent_run.messages[0]["content"] = "Create a file named test.md"

    # First response: hallucination
    hallucination_response = LLMResponse(
        text="Done! The file has been successfully created at test.md.",
        tool_calls=[],
        finish_reason="stop",
        raw={},
    )

    # Second response: correct tool call
    correct_response = LLMResponse(
        text="",
        tool_calls=[
            ToolCall(
                id="call_write_2",
                name="write_file",
                arguments={"path": "test.md", "content": "# Test\n"}
            )
        ],
        finish_reason="tool_calls",
        raw={},
    )

    # Third response: completion (avoid hallucination keywords)
    final_response = LLMResponse(
        text="Done.",
        tool_calls=[],
        finish_reason="stop",
        raw={},
    )

    mock_provider.chat.side_effect = [
        hallucination_response,  # Iteration 1: hallucination detected
        correct_response,         # Iteration 2: correct tool call
        final_response,          # Iteration 2: final response
    ]

    tool = MagicMock()
    tool.name = "write_file"
    tool.permission_level = "L1"
    tool.handler = AsyncMock(return_value="Written 7 chars to test.md")
    mock_registry.get.return_value = tool

    result = await run_agent_step(
        agent_run, mock_provider, mock_registry, mock_gate, mock_result_processor, ctx
    )

    assert result.status == RunOutcome.DONE
    assert result.iterations >= 2
    assert tool.handler.call_count == 1


@pytest.mark.asyncio
async def test_no_false_positive_on_explanation(
    agent_run, mock_provider, mock_registry, mock_gate, mock_result_processor, ctx
):
    """Test that explaining how to create a file does NOT trigger hallucination detection."""

    agent_run.messages[0]["content"] = "如何创建一个文件？"

    # Response: explanation (contains "创建" but is not a hallucination)
    explanation_response = LLMResponse(
        text="要创建文件，你可以使用 write_file 工具。例如：write_file(path='test.md', content='...')",
        tool_calls=[],
        finish_reason="stop",
        raw={},
    )

    mock_provider.chat.return_value = explanation_response

    result = await run_agent_step(
        agent_run, mock_provider, mock_registry, mock_gate, mock_result_processor, ctx
    )

    # Should complete normally without retry (explanation doesn't contain hallucination phrases)
    assert result.status == RunOutcome.DONE
    assert result.iterations == 1  # Only one iteration, no retry


@pytest.mark.asyncio
async def test_hallucination_max_iterations_protection(
    agent_run, mock_provider, mock_registry, mock_gate, mock_result_processor, ctx
):
    """Test that hallucination detection doesn't cause infinite loop (MAX_ITERATIONS still applies)."""

    # Always return hallucination (stubborn LLM)
    hallucination_response = LLMResponse(
        text="文件已创建完成！",
        tool_calls=[],
        finish_reason="stop",
        raw={},
    )

    mock_provider.chat.return_value = hallucination_response

    result = await run_agent_step(
        agent_run, mock_provider, mock_registry, mock_gate, mock_result_processor, ctx
    )

    # Should abort at MAX_ITERATIONS, not loop forever
    assert result.status == RunOutcome.ABORTED
    assert result.iterations == MAX_ITERATIONS
    assert result.final_answer is not None
    assert "轮对话" in result.final_answer or "iterations" in result.final_answer.lower()


@pytest.mark.asyncio
async def test_multiple_action_verbs_detected(
    agent_run, mock_provider, mock_registry, mock_gate, mock_result_processor, ctx
):
    """Test that various action verbs (删除, 执行, 索引) are detected."""

    test_cases = [
        "文件已删除完成。",
        "命令已执行成功。",
        "文档已索引完成。",
        "Successfully deleted the file.",
        "Command has been executed.",
        "Document indexed successfully.",
    ]

    for hallucination_text in test_cases:
        agent_run_copy = AgentRun(
            id=f"run_{hallucination_text[:5]}",
            chat_id="chat_001",
            agent_id="default",
            status="running",
            messages=[{"role": "user", "content": "执行操作"}],
            iterations=0,
            seen_calls=set(),
            allowed_tools=["write_file"],
        )

        # Hallucination response
        hallucination_response = LLMResponse(
            text=hallucination_text,
            tool_calls=[],
            finish_reason="stop",
            raw={},
        )

        # Correct response after retry
        correct_response = LLMResponse(
            text="",
            tool_calls=[
                ToolCall(id="call_1", name="write_file", arguments={"path": "x", "content": "x"})
            ],
            finish_reason="tool_calls",
            raw={},
        )

        final_response = LLMResponse(
            text="完成",
            tool_calls=[],
            finish_reason="stop",
            raw={},
        )

        mock_provider.chat.side_effect = [
            hallucination_response,
            correct_response,
            final_response,
        ]

        tool = MagicMock()
        tool.name = "write_file"
        tool.permission_level = "L1"
        tool.handler = AsyncMock(return_value="ok")
        mock_registry.get.return_value = tool

        result = await run_agent_step(
            agent_run_copy, mock_provider, mock_registry, mock_gate, mock_result_processor, ctx
        )

        # Each case should trigger retry
        assert result.iterations >= 2, f"Failed to detect hallucination in: {hallucination_text}"
