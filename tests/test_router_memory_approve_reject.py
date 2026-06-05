"""Test /memory approve and /memory reject commands dispatch correctly."""
import pytest
from unittest.mock import MagicMock, AsyncMock
from mini_claw.channels.base import InboundMessage


@pytest.fixture
def gateway_for_memory(tmp_path):
    """Construct a minimal Gateway for testing _handle_memory_command."""
    from mini_claw.config import AppConfig, AgentConfig
    from mini_claw.gateway.router import Gateway

    cfg = AppConfig()
    cfg.rag.enabled = True
    cfg.rag.namespaces.memory_enabled = True
    cfg.agents = [AgentConfig(id="default", workspace=str(tmp_path))]

    gw = Gateway.__new__(Gateway)  # type: ignore[call-arg]
    gw._config = cfg
    gw._rag_manager = MagicMock()
    gw._storage = MagicMock()
    gw._workspace_manager = MagicMock()
    gw._workspace_manager.get_workspace = MagicMock(return_value=tmp_path)
    gw._outbound_channels = {}
    return gw


def _make_msg(text: str) -> InboundMessage:
    return InboundMessage(
        chat_id="chat-1",
        text=text,
        event_id=f"evt-{abs(hash(text))}",
        channel_name="feishu",
        sender_id="user-1",
    )


@pytest.mark.asyncio
async def test_memory_approve_dispatches(gateway_for_memory):
    """Test /memory approve <cand_id> calls approve_memory()."""
    mock_channel = MagicMock()
    mock_channel.send = AsyncMock()

    gateway_for_memory._rag_manager.approve_memory = MagicMock(return_value=("mem-abc123", None))

    msg = _make_msg("/memory approve cand-488dfd3e25b7")
    handled = await gateway_for_memory._handle_memory_command(
        msg, "default", mock_channel, "feishu"
    )

    assert handled is True
    gateway_for_memory._rag_manager.approve_memory.assert_called_once_with("cand-488dfd3e25b7")
    # Check that success message was sent
    sent_texts = [c.args[1] for c in mock_channel.send.call_args_list]
    combined = " ".join(sent_texts)
    assert "approved" in combined.lower()
    assert "mem-abc123" in combined


@pytest.mark.asyncio
async def test_memory_reject_dispatches(gateway_for_memory):
    """Test /memory reject <cand_id> calls reject_memory()."""
    mock_channel = MagicMock()
    mock_channel.send = AsyncMock()

    gateway_for_memory._rag_manager.reject_memory = MagicMock(return_value=True)

    msg = _make_msg("/memory reject cand-488dfd3e25b7")
    handled = await gateway_for_memory._handle_memory_command(
        msg, "default", mock_channel, "feishu"
    )

    assert handled is True
    gateway_for_memory._rag_manager.reject_memory.assert_called_once_with("cand-488dfd3e25b7")
    sent_texts = [c.args[1] for c in mock_channel.send.call_args_list]
    combined = " ".join(sent_texts)
    assert "rejected" in combined.lower()


@pytest.mark.asyncio
async def test_memory_approve_without_arg_shows_usage(gateway_for_memory):
    """Test /memory approve without argument shows usage."""
    mock_channel = MagicMock()
    mock_channel.send = AsyncMock()

    msg = _make_msg("/memory approve")
    handled = await gateway_for_memory._handle_memory_command(
        msg, "default", mock_channel, "feishu"
    )

    assert handled is True
    sent_texts = [c.args[1] for c in mock_channel.send.call_args_list]
    combined = " ".join(sent_texts)
    assert "usage" in combined.lower()
    assert "approve" in combined.lower()

