"""Tests for the Feishu long-connection channel.

The WS client itself is not started here (that requires real network
access). We unit-test the message-dispatch glue: when lark calls our
sync handler, it should marshal the event onto the main asyncio loop
and invoke ``on_message``.
"""

import asyncio
import json

import pytest

from mini_claw.channels.base import InboundMessage
from mini_claw.channels.feishu import FeishuChannel


class _FakeSenderId:
    def __init__(self, open_id: str) -> None:
        self.open_id = open_id


class _FakeSender:
    def __init__(self, open_id: str) -> None:
        self.sender_id = _FakeSenderId(open_id)


class _FakeMessage:
    def __init__(self, chat_id: str, content: str, create_time: int = 0) -> None:
        self.chat_id = chat_id
        self.content = content
        self.create_time = create_time
        self.message_type = "text"


class _FakeEventData:
    def __init__(self, msg: _FakeMessage, sender: _FakeSender) -> None:
        self.message = msg
        self.sender = sender


class _FakeHeader:
    def __init__(self, event_id: str) -> None:
        self.event_id = event_id


class _FakeMessageEvent:
    def __init__(self, data: _FakeEventData, event_id: str) -> None:
        self.event = data
        self.header = _FakeHeader(event_id)


class _FakeAction:
    def __init__(self, value: dict) -> None:
        self.value = value


class _FakeCardEventData:
    def __init__(self, value: dict) -> None:
        self.action = _FakeAction(value)


class _FakeCardEvent:
    def __init__(self, value: dict) -> None:
        self.event = _FakeCardEventData(value)


@pytest.mark.asyncio
async def test_on_message_dispatches_to_main_loop():
    channel = FeishuChannel(app_id="cli_test", app_secret="secret")
    received: list[InboundMessage] = []
    done = asyncio.Event()

    async def on_message(msg: InboundMessage) -> None:
        received.append(msg)
        done.set()

    channel.on_message = on_message
    channel._main_loop = asyncio.get_running_loop()

    fake = _FakeMessageEvent(
        data=_FakeEventData(
            msg=_FakeMessage(
                chat_id="oc_123",
                content=json.dumps({"text": "hello"}),
                create_time=12345,
            ),
            sender=_FakeSender(open_id="ou_abc"),
        ),
        event_id="evt_001",
    )

    channel._on_message_event(fake)  # sync entrypoint, schedules coroutine
    await asyncio.wait_for(done.wait(), timeout=1.0)

    assert len(received) == 1
    msg = received[0]
    assert msg.chat_id == "oc_123"
    assert msg.sender_id == "ou_abc"
    assert msg.text == "hello"
    assert msg.event_id == "evt_001"

    status = channel.health_status()
    assert status["received_count"] == 1
    assert status["last_event_id"] == "evt_001"
    assert status["last_chat_id"] == "oc_123"
    assert status["last_sender_id"] == "ou_abc"
    assert status["last_message_type"] == "text"
    assert status["idle_seconds"] is not None


@pytest.mark.asyncio
async def test_on_card_action_dispatches_to_main_loop():
    channel = FeishuChannel(app_id="cli_test", app_secret="secret")
    captured: list[dict] = []
    done = asyncio.Event()

    async def on_action(value: dict) -> None:
        captured.append(value)
        done.set()

    channel.on_card_action = on_action
    channel._main_loop = asyncio.get_running_loop()

    response = channel._on_card_action_event(
        _FakeCardEvent({"action": "approve", "approval_id": "ap_001"})
    )

    await asyncio.wait_for(done.wait(), timeout=1.0)
    assert captured == [{"action": "approve", "approval_id": "ap_001"}]
    # Lark expects a P2CardActionTriggerResponse to ack the trigger.
    assert response is not None


@pytest.mark.asyncio
async def test_health_check_restarts_when_ws_thread_is_not_alive(monkeypatch):
    channel = FeishuChannel(
        app_id="cli_test",
        app_secret="secret",
        health_check_interval_sec=60,
        restart_on_disconnect=True,
    )
    starts: list[str] = []

    def fake_start_ws_thread() -> None:
        starts.append("started")

    monkeypatch.setattr(channel, "_start_ws_thread", fake_start_ws_thread)

    restarted = await channel._restart_if_needed(
        {
            "ws_thread_alive": False,
            "ws_exited_at": None,
            "idle_seconds": None,
        }
    )

    assert restarted is True
    assert starts == ["started"]
    status = channel.health_status()
    assert status["restart_count"] == 1
    assert status["last_restart_reason"] == "ws_thread_not_alive"


@pytest.mark.asyncio
async def test_health_check_does_not_restart_when_disabled(monkeypatch):
    channel = FeishuChannel(
        app_id="cli_test",
        app_secret="secret",
        restart_on_disconnect=False,
    )
    starts: list[str] = []
    monkeypatch.setattr(channel, "_start_ws_thread", lambda: starts.append("started"))

    restarted = await channel._restart_if_needed(
        {
            "ws_thread_alive": False,
            "ws_exited_at": None,
            "idle_seconds": None,
        }
    )

    assert restarted is False
    assert starts == []
