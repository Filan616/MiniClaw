"""Tests for the current_time built-in tool."""

import json
from pathlib import Path

import pytest

from mini_claw.tools.builtin import TOOL_CURRENT_TIME
from mini_claw.tools.registry import ToolContext


@pytest.mark.asyncio
async def test_current_time_tool_returns_structured_asia_shanghai_time():
    result = await TOOL_CURRENT_TIME.handler(
        timezone="Asia/Shanghai",
        ctx=ToolContext(workspace_dir=Path(".")),
    )

    data = json.loads(result)
    assert data["timezone"] == "Asia/Shanghai"
    assert data["utc_offset"] == "+0800"
    assert data["date"]
    assert data["time"]
    assert data["weekday_zh"].startswith("星期")


@pytest.mark.asyncio
async def test_current_time_tool_rejects_unknown_timezone():
    result = await TOOL_CURRENT_TIME.handler(
        timezone="No/SuchZone",
        ctx=ToolContext(workspace_dir=Path(".")),
    )

    assert result.startswith("[ERROR] Unknown timezone")
