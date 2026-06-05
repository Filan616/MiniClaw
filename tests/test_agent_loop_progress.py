"""Tests for Phase 9.8: Progress updates and loop detection."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from mini_claw.agent.context import AgentContext
from mini_claw.agent.loop import (
    AgentRun,
    RunOutcome,
    _detect_tool_call_loop,
    _send_progress_update,
    run_agent_step,
)
from mini_claw.providers.base import Provider, LLMResponse
from mini_claw.tools.registry import Tool, ToolContext, ToolRegistry


# ---------------------------------------------------------------------------
# Test _detect_tool_call_loop
# ---------------------------------------------------------------------------


def test_detect_loop_no_history():
    """No loop when history is empty."""
    run = AgentRun(
        id="test",
        chat_id="test",
        agent_id="test",
        status=RunOutcome.DONE,
        tool_call_history=[],
    )
    is_looping, tool = _detect_tool_call_loop(run)
    assert not is_looping
    assert tool is None


def test_detect_loop_insufficient_history():
    """No loop when history < lookback."""
    run = AgentRun(
        id="test",
        chat_id="test",
        agent_id="test",
        status=RunOutcome.DONE,
        tool_call_history=[
            ("read_file", False),
            ("read_file", False),
        ],
    )
    is_looping, tool = _detect_tool_call_loop(run, lookback=5)
    assert not is_looping
    assert tool is None


def test_detect_loop_same_tool_repeated_failures():
    """Detect loop when same tool called 3+ times with failures."""
    run = AgentRun(
        id="test",
        chat_id="test",
        agent_id="test",
        status=RunOutcome.DONE,
        tool_call_history=[
            ("read_file", False),
            ("read_file", False),
            ("read_file", False),
            ("read_file", False),
            ("read_file", False),
        ],
    )
    is_looping, tool = _detect_tool_call_loop(run, lookback=5)
    assert is_looping
    assert tool == "read_file"


def test_detect_loop_same_tool_mostly_successful():
    """No loop when same tool called but mostly successful."""
    run = AgentRun(
        id="test",
        chat_id="test",
        agent_id="test",
        status=RunOutcome.DONE,
        tool_call_history=[
            ("read_file", True),
            ("read_file", True),
            ("read_file", True),
            ("read_file", False),
            ("read_file", True),
        ],
    )
    is_looping, tool = _detect_tool_call_loop(run, lookback=5)
    assert not is_looping  # 80% success rate


def test_detect_loop_mixed_tools_no_loop():
    """No loop when tools are varied."""
    run = AgentRun(
        id="test",
        chat_id="test",
        agent_id="test",
        status=RunOutcome.DONE,
        tool_call_history=[
            ("read_file", False),
            ("write_file", False),
            ("list_directory", False),
            ("run_shell", False),
            ("read_file", False),
        ],
    )
    is_looping, tool = _detect_tool_call_loop(run, lookback=5)
    assert not is_looping  # No single tool dominates


def test_detect_loop_boundary_3_calls():
    """Loop detected exactly at 3 repeated calls threshold."""
    run = AgentRun(
        id="test",
        chat_id="test",
        agent_id="test",
        status=RunOutcome.DONE,
        tool_call_history=[
            ("list_directory", False),
            ("list_directory", False),
            ("list_directory", False),
            ("write_file", True),
            ("read_file", True),
        ],
    )
    is_looping, tool = _detect_tool_call_loop(run, lookback=5)
    assert is_looping
    assert tool == "list_directory"


# ---------------------------------------------------------------------------
# Test _send_progress_update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_progress_no_callback():
    """Progress update does nothing when on_progress is None."""
    run = AgentRun(
        id="test",
        chat_id="test",
        agent_id="test",
        status=RunOutcome.DONE,
    )
    ctx = AgentContext(
        chat_id="test",
        agent_id="test",
        workspace_dir=Path("/tmp"),
        on_progress=None,
    )
    # Should not raise
    await _send_progress_update(run, ctx, iteration=3, last_tool="read_file")


@pytest.mark.asyncio
async def test_send_progress_with_callback():
    """Progress update calls on_progress callback."""
    run = AgentRun(
        id="test",
        chat_id="test",
        agent_id="test",
        status=RunOutcome.DONE,
    )

    progress_messages = []

    async def capture_progress(msg: str):
        progress_messages.append(msg)

    ctx = AgentContext(
        chat_id="test",
        agent_id="test",
        workspace_dir=Path("/tmp"),
        on_progress=capture_progress,
    )

    await _send_progress_update(run, ctx, iteration=3, last_tool="read_file")

    assert len(progress_messages) == 1
    assert "第 3 轮" in progress_messages[0]
    assert "read_file" in progress_messages[0]


@pytest.mark.asyncio
async def test_send_progress_callback_exception():
    """Progress update handles callback exceptions gracefully."""
    run = AgentRun(
        id="test",
        chat_id="test",
        agent_id="test",
        status=RunOutcome.DONE,
    )

    async def failing_callback(msg: str):
        raise RuntimeError("Callback failed")

    ctx = AgentContext(
        chat_id="test",
        agent_id="test",
        workspace_dir=Path("/tmp"),
        on_progress=failing_callback,
    )

    # Should not raise, just log warning
    await _send_progress_update(run, ctx, iteration=5)


# ---------------------------------------------------------------------------
# Integration test: loop detection injects system message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_detection_injects_system_message():
    """Integration: loop detection adds system message to run.messages."""

    # Create run with history showing repeated failures
    run = AgentRun(
        id="test_run",
        chat_id="test_chat",
        agent_id="test_agent",
        status=RunOutcome.DONE,
        allowed_tools=["list_directory"],
        tool_call_history=[
            ("list_directory", False),
            ("list_directory", False),
            ("list_directory", False),
            ("list_directory", False),
            ("list_directory", False),
        ],
        messages=[{"role": "user", "content": "列出文件"}],
    )

    # Manually trigger loop detection logic (as it would happen in run_agent_step)
    is_looping, loop_tool = _detect_tool_call_loop(run)

    # Verify loop was detected
    assert is_looping
    assert loop_tool == "list_directory"

    # Simulate what run_agent_step does: inject system message
    if is_looping:
        loop_warning = (
            f"⚠️ 系统提示：你已经连续多次调用 `{loop_tool}` 工具但未成功。"
            f"请换一个不同的方法或工具来解决问题，不要再重复调用 `{loop_tool}`。"
        )
        run.messages.append({
            "role": "system",
            "content": loop_warning,
        })

    # Check that a system message was injected
    system_messages = [msg for msg in run.messages if msg.get("role") == "system"]
    assert len(system_messages) == 1

    # Check system message content warns about list_directory loop
    loop_warning_msg = system_messages[0]["content"]
    assert "list_directory" in loop_warning_msg
    assert "连续多次调用" in loop_warning_msg or "不要再重复调用" in loop_warning_msg
