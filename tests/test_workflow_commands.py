import asyncio
from pathlib import Path

import pytest

from mini_claw.config import AgentConfig, AppConfig, WorkflowConfig
from mini_claw.gateway.router import Gateway
from mini_claw.permissions.approval_store import ApprovalStore
from mini_claw.permissions.gate import PermissionGate
from mini_claw.permissions.policy import PermissionPolicy
from mini_claw.storage.db import Database
from mini_claw.tools.builtin import BUILTIN_TOOLS
from mini_claw.tools.registry import ToolRegistry
from mini_claw.workflow.planner import WorkflowPlanner
from mini_claw.workflow.prompt_compiler import SubAgentPromptCompiler
from mini_claw.workflow.store import WorkflowStore


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text):
        self.sent.append((chat_id, text))


class FakeMsg:
    chat_id = "chat"
    channel_name = "cli"

    def __init__(self, text):
        self.text = text


def _gateway(tmp_path: Path, *, workflow_enabled: bool = True):
    config = AppConfig(workflow=WorkflowConfig(enabled=workflow_enabled, require_approval=True))
    db = Database(tmp_path / "gw.db")
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
    return gw


@pytest.mark.asyncio
async def test_workflow_plan_command_creates_plan_and_does_not_execute(tmp_path):
    gw = _gateway(tmp_path)
    channel = FakeChannel()
    handled = await gw._handle_workflow_command(
        FakeMsg("/workflow plan 全面检查项目"),
        AgentConfig(tools=["read_file", "write_file", "run_shell", "list_directory"]),
        "default",
        tmp_path,
        "safe",
        channel,
        "cli",
    )
    assert handled is True
    assert "Workflow plan:" in channel.sent[0][1]
    rows = gw._storage.fetchall("SELECT status FROM workflow_runs")
    assert rows == [{"status": "planning"}]


@pytest.mark.asyncio
async def test_workflow_run_command_enters_approval(tmp_path):
    gw = _gateway(tmp_path)
    channel = FakeChannel()
    await gw._handle_workflow_command(
        FakeMsg("/workflow run 全面检查项目"),
        AgentConfig(tools=["read_file", "write_file", "run_shell", "list_directory"]),
        "default",
        tmp_path,
        "safe",
        channel,
        "cli",
    )
    row = gw._storage.fetchone("SELECT status, approval_id FROM workflow_runs")
    approval = gw._storage.fetchone("SELECT approval_type FROM pending_approvals WHERE id=?", (row["approval_id"],))
    assert row["status"] == "awaiting_approval"
    assert approval["approval_type"] == "workflow_plan"
    assert "Approval required" in channel.sent[0][1]


@pytest.mark.asyncio
async def test_workflow_command_disabled_when_config_off(tmp_path):
    gw = _gateway(tmp_path, workflow_enabled=False)
    channel = FakeChannel()
    handled = await gw._handle_workflow_command(
        FakeMsg("/workflow plan 全面检查项目"),
        AgentConfig(tools=["read_file", "list_directory"]),
        "default",
        tmp_path,
        "safe",
        channel,
        "cli",
    )
    assert handled is True
    assert "disabled" in channel.sent[0][1]
