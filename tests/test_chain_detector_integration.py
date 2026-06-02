"""Integration test for ChainDetector in the agent loop (Phase 0.1)."""

import pytest
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path

from mini_claw.agent.loop import AgentRun, RunOutcome, run_agent_step
from mini_claw.agent.context import AgentContext
from mini_claw.permissions.chain_detector import ChainDetector
from mini_claw.providers.base import LLMResponse, ToolCall
from mini_claw.permissions.gate import Decision


@pytest.mark.asyncio
async def test_chain_detector_blocks_shell_after_write_and_chmod(tmp_path):
    """Phase 0.1: ChainDetector must block write_file(*.sh) → run_shell(chmod) → run_shell(exec).

    The third step should be denied with a chain_attack_blocked audit event.
    """
    # Setup: 3-step chain
    call1 = ToolCall(id="call_1", name="write_file", arguments={"path": "script.sh", "content": "echo hi"})
    call2 = ToolCall(id="call_2", name="run_shell", arguments={"command": "chmod +x script.sh"})
    call3 = ToolCall(id="call_3", name="run_shell", arguments={"command": "./script.sh"})

    responses = [
        LLMResponse(text="", tool_calls=[call1], finish_reason="tool_calls", raw={}),
        LLMResponse(text="", tool_calls=[call2], finish_reason="tool_calls", raw={}),
        LLMResponse(text="", tool_calls=[call3], finish_reason="tool_calls", raw={}),
        LLMResponse(text="Done", tool_calls=[], finish_reason="stop", raw={}),
    ]

    mock_provider = AsyncMock()
    mock_provider.chat.side_effect = responses

    # Registry with write_file (L1) and run_shell (L2)
    write_tool = MagicMock()
    write_tool.name = "write_file"
    write_tool.permission_level = "L1"
    write_tool.handler = AsyncMock(return_value="Written 8 chars to script.sh")

    shell_tool = MagicMock()
    shell_tool.name = "run_shell"
    shell_tool.permission_level = "L2"
    shell_tool.handler = AsyncMock(side_effect=["", "hi"])  # chmod returns empty, exec returns hi

    mock_registry = MagicMock()
    mock_registry.schemas_for.return_value = [
        {"name": "write_file", "description": "Write file", "parameters": {}},
        {"name": "run_shell", "description": "Run shell", "parameters": {}},
    ]

    def registry_get(name):
        if name == "write_file":
            return write_tool
        elif name == "run_shell":
            return shell_tool
        return None

    mock_registry.get.side_effect = registry_get

    # PermissionGate: allow all
    mock_gate = MagicMock()
    mock_gate.evaluate.return_value = Decision(action="allow")

    mock_result_processor = MagicMock()
    mock_result_processor.process.side_effect = lambda r, _: r

    # Context with ChainDetector and audit logger
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    audit_events = []

    mock_audit_logger = MagicMock()
    mock_audit_logger.log_security_event.side_effect = lambda event_type, details, chat_id, agent_id: audit_events.append(
        {"event_type": event_type, "details": details}
    ) or f"debug_{len(audit_events)}"

    chain_detector = ChainDetector()

    ctx = AgentContext(
        chat_id="chat_test",
        agent_id="agent_test",
        workspace_dir=workspace,
        channel=MagicMock(),
        sandbox_mode="safe",
        audit_logger=mock_audit_logger,
        chain_detector=chain_detector,
    )

    run = AgentRun(
        id="run_001",
        chat_id="chat_test",
        agent_id="agent_test",
        status="running",
        messages=[{"role": "user", "content": "run script"}],
        iterations=0,
        seen_calls=set(),
        allowed_tools=["write_file", "run_shell"],
    )

    result = await run_agent_step(
        run, mock_provider, mock_registry, mock_gate, mock_result_processor, ctx
    )

    # Verify: call3 should be denied by ChainDetector
    tool_results = [m for m in result.messages if m.get("role") == "tool"]
    assert len(tool_results) == 3

    call1_result = next(m for m in tool_results if m["tool_call_id"] == "call_1")
    assert "Written" in call1_result["content"]

    call2_result = next(m for m in tool_results if m["tool_call_id"] == "call_2")
    # chmod might succeed or be blocked depending on detector state; just check it ran
    assert "tool_call_id" in call2_result

    call3_result = next(m for m in tool_results if m["tool_call_id"] == "call_3")
    assert "[denied]" in call3_result["content"]
    assert "Chain attack" in call3_result["content"] or "chain" in call3_result["content"].lower()

    # Verify: audit event was logged
    chain_events = [e for e in audit_events if e["event_type"] == "chain_attack_blocked"]
    assert len(chain_events) >= 1
