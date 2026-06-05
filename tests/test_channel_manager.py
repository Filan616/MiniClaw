from pathlib import Path

import pytest

from mini_claw.agent.manager import AgentManager
from mini_claw.agent.workspace import WorkspaceManager
from mini_claw.channels.base import Channel, InboundMessage
from mini_claw.channels.manager import ChannelManager
from mini_claw.config import AgentConfig, AppConfig, ChannelConfig, load_config
from mini_claw.gateway.router import Gateway
from mini_claw.permissions.approval_store import ApprovalStore
from mini_claw.permissions.gate import PermissionGate
from mini_claw.permissions.policy import PermissionPolicy
from mini_claw.providers.base import LLMResponse, Provider
from mini_claw.providers.manager import ProviderManager
from mini_claw.storage.db import Database
from mini_claw.tools.result_processor import ToolResultProcessor
from mini_claw.tools.registry import ToolRegistry


class DummyProvider(Provider):
    async def chat(self, messages, tools=None, stream=False, stream_callback=None):
        return LLMResponse(text="hello from cli")

    def format_tools(self, tools):
        return tools


class CaptureChannel(Channel):
    channel_type = "capture"

    def __init__(self, name: str = "capture") -> None:
        super().__init__(name=name)
        self.sent: list[tuple[str, str]] = []
        self.cards: list[tuple[str, str, str, dict, str]] = []

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))

    async def send_approval_card(
        self,
        chat_id: str,
        approval_id: str,
        tool_name: str,
        tool_args: dict,
        level: str,
    ) -> None:
        self.cards.append((chat_id, approval_id, tool_name, tool_args, level))


class CaptureFeishuChannel(CaptureChannel):
    channel_type = "feishu"

    def health_status(self) -> dict:
        return {
            "channel_name": self.name,
            "channel_type": "feishu",
            "app_id": "cli_test",
            "started_at": 1000,
            "uptime_seconds": 25,
            "ws_thread_alive": True,
            "main_loop_alive": True,
            "received_count": 3,
            "malformed_count": 0,
            "last_event_at": 1010,
            "idle_seconds": 5,
            "last_event_id": "evt_last",
            "last_chat_id": "chat_last",
            "last_sender_id": "sender_last",
            "last_message_type": "text",
            "ws_exited_at": None,
            "ws_exception": "",
        }


def test_load_config_expands_legacy_feishu_channels(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
channels_feishu:
  enabled: true
  app_id: app
  app_secret: secret
""",
        encoding="utf-8",
    )

    cfg = load_config(config_path)
    assert cfg.channels[0].name == "feishu"
    assert cfg.channels[0].type == "feishu"
    assert cfg.channels[0].enabled is True
    assert cfg.channels[0].options["app_id"] == "app"


def test_new_channels_config_takes_priority(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
channels_feishu:
  enabled: true
  app_id: old
  app_secret: old
channels:
  - name: cli
    type: cli
    enabled: true
""",
        encoding="utf-8",
    )

    cfg = load_config(config_path)
    assert [channel.name for channel in cfg.channels] == ["cli"]


def test_channel_manager_registers_enabled_cli_channel():
    cfg = AppConfig(
        channels=[ChannelConfig(name="cli", type="cli", enabled=True)]
    )
    manager = ChannelManager(cfg)

    manager.load_enabled()

    assert manager.has_channel("cli")
    assert manager.get_channel("cli").name == "cli"


def test_session_rows_store_channel_name(tmp_path: Path):
    from mini_claw.gateway.session import SessionManager

    db = Database(tmp_path / "session.db")
    session = SessionManager(db)
    session.get_or_create("chat", "default", channel_name="cli")

    row = db.fetchone("SELECT channel_name FROM sessions WHERE chat_id=?", ("chat",))
    assert row["channel_name"] == "cli"


@pytest.mark.asyncio
async def test_gateway_replies_on_inbound_channel(tmp_path: Path):
    cfg = AppConfig(
        agents=[AgentConfig(id="default", tools=[])],
        channels=[ChannelConfig(name="cli", type="cli", enabled=False)],
    )
    db = Database(tmp_path / "gateway.db")
    workspace_manager = WorkspaceManager(tmp_path / "workspaces")
    workspace_manager.load_workspaces(cfg.agents)
    agent_manager = AgentManager(db, cfg, workspace_manager)
    provider = DummyProvider()
    provider_manager = ProviderManager(cfg, default_provider=provider)
    channel_manager = ChannelManager(cfg)
    registry = ToolRegistry()
    permission_gate = PermissionGate(
        PermissionPolicy(cfg.permissions),
        ApprovalStore(db),
    )

    gateway = Gateway(
        config=cfg,
        storage=db,
        provider=provider,
        provider_manager=provider_manager,
        registry=registry,
        permission_gate=permission_gate,
        result_processor=ToolResultProcessor(),
        workspace_manager=workspace_manager,
        agent_manager=agent_manager,
        channel_manager=channel_manager,
    )
    channel_manager.set_gateway(gateway)
    cli_channel = CaptureChannel(name="cli")
    channel_manager.register_instance(cli_channel)

    await gateway.handle_message(
        InboundMessage(
            channel_name="cli",
            chat_id="cli_local",
            sender_id="tester",
            text="hello",
            event_id="evt_cli_1",
            timestamp=1,
        )
    )

    assert cli_channel.sent == [("cli_local", "hello from cli")]
    event = db.fetchone(
        "SELECT channel_name FROM processed_events WHERE event_id=?",
        ("evt_cli_1",),
    )
    assert event["channel_name"] == "cli"


@pytest.mark.asyncio
async def test_gateway_handles_feishu_status_command(tmp_path: Path):
    cfg = AppConfig(
        agents=[AgentConfig(id="default", tools=[])],
        channels=[ChannelConfig(name="feishu", type="feishu", enabled=False)],
    )
    db = Database(tmp_path / "gateway.db")
    workspace_manager = WorkspaceManager(tmp_path / "workspaces")
    workspace_manager.load_workspaces(cfg.agents)
    agent_manager = AgentManager(db, cfg, workspace_manager)
    provider = DummyProvider()
    provider_manager = ProviderManager(cfg, default_provider=provider)
    channel_manager = ChannelManager(cfg)
    registry = ToolRegistry()
    permission_gate = PermissionGate(
        PermissionPolicy(cfg.permissions),
        ApprovalStore(db),
    )

    gateway = Gateway(
        config=cfg,
        storage=db,
        provider=provider,
        provider_manager=provider_manager,
        registry=registry,
        permission_gate=permission_gate,
        result_processor=ToolResultProcessor(),
        workspace_manager=workspace_manager,
        agent_manager=agent_manager,
        channel_manager=channel_manager,
    )
    channel_manager.set_gateway(gateway)
    feishu_channel = CaptureFeishuChannel(name="feishu")
    channel_manager.register_instance(feishu_channel)

    await gateway.handle_message(
        InboundMessage(
            channel_name="feishu",
            chat_id="oc_status",
            sender_id="tester",
            text="/feishu status",
            event_id="evt_feishu_status",
            timestamp=1,
        )
    )

    assert len(feishu_channel.sent) == 1
    response = feishu_channel.sent[0][1]
    assert "Feishu status:" in response
    assert "ws_thread_alive: True" in response
    assert "received_count: 3" in response
    assert "last_event_id: evt_last" in response
    event = db.fetchone(
        "SELECT status FROM processed_events WHERE event_id=?",
        ("evt_feishu_status",),
    )
    assert event["status"] == "handled"
