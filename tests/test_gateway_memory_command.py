from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mini_claw.channels.base import Channel, InboundMessage
from mini_claw.config import AppConfig
from mini_claw.gateway.router import Gateway


class FakeChannel(Channel):
    async def send(self, chat_id: str, text: str) -> None:
        self.last_chat_id = chat_id
        self.last_text = text

    async def send_approval_card(
        self,
        chat_id: str,
        approval_id: str,
        tool_name: str,
        tool_args: dict,
        level: str,
    ) -> None:
        self.last_chat_id = chat_id
        self.last_text = approval_id


class FakeWorkspaceManager:
    def get_workspace(self, chat_id: str, agent_id: str) -> Path:
        return Path("D:/Learning/MiniClaw")


class FakeRagManager:
    def list_memories(self, *, ctx: dict, limit: int = 100, status: str = "active"):
        return [
            SimpleNamespace(
                item_id="mem-1",
                title="Preference",
                source_type="user_preference",
                pinned=0,
                confidence=1.0,
            )
        ]


@pytest.mark.asyncio
async def test_memory_list_command_is_routed():
    cfg = AppConfig()
    cfg.rag.enabled = True
    cfg.rag.namespaces.memory_enabled = True
    gateway = Gateway.__new__(Gateway)
    gateway._config = cfg
    gateway._rag_manager = FakeRagManager()
    gateway._workspace_manager = FakeWorkspaceManager()

    channel = FakeChannel()
    msg = InboundMessage(chat_id="chat-1", text="/memory list", event_id="evt-1")

    handled = await Gateway._handle_memory_command(
        gateway, msg, "default", channel, "feishu"
    )

    assert handled is True
    assert channel.last_chat_id == "chat-1"
    assert "Active memories" in channel.last_text
    assert "mem-1" in channel.last_text
