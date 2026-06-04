"""Tests for Phase 7: WorkflowPlanner auto-detect (普通消息自动触发)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mini_claw.config import AgentConfig, AppConfig, WorkflowConfig
from mini_claw.gateway.router import Gateway
from mini_claw.gateway.session import SessionManager
from mini_claw.permissions.approval_store import ApprovalStore
from mini_claw.permissions.gate import PermissionGate
from mini_claw.permissions.policy import PermissionPolicy
from mini_claw.providers.base import LLMResponse, Provider
from mini_claw.providers.manager import ProviderManager
from mini_claw.storage.db import Database
from mini_claw.tools.builtin import BUILTIN_TOOLS
from mini_claw.tools.registry import ToolRegistry
from mini_claw.workflow.planner import WorkflowPlanner
from mini_claw.workflow.prompt_compiler import SubAgentPromptCompiler
from mini_claw.workflow.store import WorkflowStore


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))


class FakeMsg:
    chat_id = "auto-chat"
    channel_name = "cli"
    event_id = "evt-1"

    def __init__(self, text: str) -> None:
        self.text = text


class _StubProvider(Provider):
    """Counts calls and returns a configurable LLMResponse."""

    def __init__(self, response_text: str = "{}") -> None:
        self.call_count = 0
        self.response_text = response_text

    async def chat(self, messages, tools=None, stream=False, stream_callback=None):
        self.call_count += 1
        return LLMResponse(text=self.response_text)

    def format_tools(self, tools):
        return tools


def _gateway(
    tmp_path: Path,
    *,
    auto_detect: bool,
    provider: Provider | None = None,
):
    config = AppConfig(
        workflow=WorkflowConfig(
            enabled=True,
            auto_detect=auto_detect,
            require_approval=True,
        ),
    )
    db = Database(tmp_path / "auto.db")
    registry = ToolRegistry()
    for tool in BUILTIN_TOOLS:
        registry.register(tool)
    gate = PermissionGate(PermissionPolicy(config.permissions), ApprovalStore(db))

    gw = Gateway.__new__(Gateway)
    gw._config = config
    gw._storage = db
    gw._registry = registry
    gw._permission_gate = gate
    gw._workflow_store = WorkflowStore(db)
    gw._workflow_planner = WorkflowPlanner(config.workflow)
    gw._workflow_prompt_compiler = SubAgentPromptCompiler(config.workflow)
    gw._session_mgr = SessionManager(db)
    gw._audit_logger = None
    gw._provider_manager = ProviderManager(config, default_provider=provider)
    return gw


@pytest.mark.asyncio
async def test_auto_detect_disabled_skips_workflow(tmp_path):
    """When auto_detect=False, normal text never triggers workflow."""
    gw = _gateway(tmp_path, auto_detect=False, provider=_StubProvider())
    channel = FakeChannel()
    handled = await gw._maybe_auto_dispatch_workflow(
        FakeMsg("请帮我看看这段代码哪里写错了，详细分析一下"),
        AgentConfig(tools=["read_file", "list_directory"]),
        "default",
        tmp_path,
        "safe",
        channel,
        "cli",
    )
    assert handled is False
    rows = gw._storage.fetchall("SELECT workflow_id FROM workflow_runs")
    assert rows == []


@pytest.mark.asyncio
async def test_auto_detect_keyword_match_skips_llm(tmp_path):
    """Keyword pre-filter hit ('全面审计') triggers without calling LLM."""
    provider = _StubProvider()
    gw = _gateway(tmp_path, auto_detect=True, provider=provider)
    channel = FakeChannel()

    handled = await gw._maybe_auto_dispatch_workflow(
        FakeMsg("请对项目做一次全面审计，重点关注权限边界"),
        AgentConfig(tools=["read_file", "write_file", "run_shell", "list_directory"]),
        "default",
        tmp_path,
        "safe",
        channel,
        "cli",
    )
    assert handled is True
    assert provider.call_count == 0  # zero LLM cost on keyword hit
    row = gw._storage.fetchone("SELECT status FROM workflow_runs")
    assert row["status"] == "awaiting_approval"
    assert any("Auto-detected" in text for _, text in channel.sent)


@pytest.mark.asyncio
async def test_auto_detect_llm_fallback_triggers(tmp_path):
    """Mid-length non-keyword text triggers LLM, which approves use_workflow."""
    llm_text = (
        '{"use_workflow": true, "template": "debug_fix", '
        '"reason": "stack trace pattern detected"}'
    )
    provider = _StubProvider(response_text=llm_text)
    gw = _gateway(tmp_path, auto_detect=True, provider=provider)
    channel = FakeChannel()

    # 100+ chars, no keyword in should_use_workflow allowlist (avoid 失败/error/全面/迁移/upgrade etc.)
    text = (
        "刚才在调用支付接口的时候服务端突然返回了一个空对象，前端把它当成成功处理了，"
        "导致用户看到付款成功但订单状态还是未支付，需要排查整条链路的问题，希望你能给出可靠的排查方向。"
    )
    handled = await gw._maybe_auto_dispatch_workflow(
        FakeMsg(text),
        AgentConfig(tools=["read_file", "write_file", "run_shell", "list_directory"]),
        "default",
        tmp_path,
        "safe",
        channel,
        "cli",
    )
    assert handled is True
    assert provider.call_count == 1
    row = gw._storage.fetchone(
        "SELECT status, spec_json FROM workflow_runs"
    )
    assert row["status"] == "awaiting_approval"
    assert "debug_fix" in row["spec_json"]


@pytest.mark.asyncio
async def test_auto_detect_llm_invalid_json_falls_back(tmp_path):
    """Non-JSON LLM response → fallback to use_workflow=False (no trigger)."""
    provider = _StubProvider(response_text="sorry I can not classify")
    gw = _gateway(tmp_path, auto_detect=True, provider=provider)
    channel = FakeChannel()

    text = (
        "今天的任务是让登录页的输入框边距和设计稿一致，并且修复在 Safari 上的对齐问题，"
        "测试要覆盖三个分辨率，再加上一个移动端的兼容性核对，确保按钮可以正常点击。"
    )
    handled = await gw._maybe_auto_dispatch_workflow(
        FakeMsg(text),
        AgentConfig(tools=["read_file", "write_file", "list_directory"]),
        "default",
        tmp_path,
        "safe",
        channel,
        "cli",
    )
    assert handled is False
    rows = gw._storage.fetchall("SELECT workflow_id FROM workflow_runs")
    assert rows == []


@pytest.mark.asyncio
async def test_auto_detect_slash_prefix_never_triggers(tmp_path):
    """Slash-prefixed text always returns False (commands routed elsewhere)."""
    provider = _StubProvider()
    gw = _gateway(tmp_path, auto_detect=True, provider=provider)
    channel = FakeChannel()

    handled = await gw._maybe_auto_dispatch_workflow(
        FakeMsg("/somecommand 全面审计"),
        AgentConfig(tools=["read_file", "list_directory"]),
        "default",
        tmp_path,
        "safe",
        channel,
        "cli",
    )
    assert handled is False
    assert provider.call_count == 0
