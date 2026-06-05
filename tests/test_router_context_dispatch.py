"""Tests for /context subcommand dispatch in Gateway._handle_rag_command.

Regression test for the bug where /context use|archive|delete|reindex|rebind|cleanup
fell through to a fallback that incorrectly said "Unknown /memory subcommand: use".

The router previously declared these subcommands in usage text but never implemented
their handlers, so they leaked into the /memory fallback path.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from mini_claw.channels.base import InboundMessage


@pytest.fixture
def mock_channel():
    ch = MagicMock()
    ch.send = AsyncMock()
    return ch


@pytest.fixture
def mock_rag_manager():
    """RagManager with all context operations mocked to (True, '') / item / counts."""
    mgr = MagicMock()
    mgr.use_context = MagicMock(return_value=(True, ""))
    mgr.archive_context = MagicMock(return_value=(True, ""))
    mgr.delete_context = MagicMock(return_value=(True, ""))
    mgr.reindex_context = MagicMock(return_value=(True, ""))
    mgr.rebind_context = MagicMock(return_value=(True, ""))
    mgr.cleanup_lifecycle = MagicMock(return_value={"archived": 0, "deleted": 0})

    # Inspect returns a mock item
    item = MagicMock()
    item.item_id = "abc123"
    item.source_path = "docs/test.md"
    item.source_type = "doc"
    item.status = "active"
    item.sensitivity_level = "low"
    item.content_hash = "h"
    item.active_version = 1
    mgr.inspect_context = MagicMock(return_value=(item, ""))
    return mgr


@pytest.fixture
def gateway_for_dispatch(tmp_path, mock_rag_manager):
    """Construct a minimal Gateway sufficient to call _handle_rag_command directly."""
    from mini_claw.config import AppConfig, AgentConfig
    from mini_claw.gateway.router import Gateway

    cfg = AppConfig()
    cfg.rag.enabled = True
    cfg.rag.namespaces.context_enabled = True
    cfg.agents = [AgentConfig(id="default", workspace=str(tmp_path))]

    # Build Gateway with bare-minimum mocks; we only exercise _handle_rag_command
    gw = Gateway.__new__(Gateway)  # type: ignore[call-arg]
    gw._config = cfg
    gw._rag_manager = mock_rag_manager
    gw._chat_search_manager = None
    gw._audit_logger = None
    gw._storage = MagicMock()
    gw._workspace_manager = MagicMock()
    gw._workspace_manager.get_workspace = MagicMock(return_value=tmp_path)
    gw._session_mgr = MagicMock()
    gw._session_mgr.get_sandbox_mode = MagicMock(return_value="safe")
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
async def test_context_use_dispatches_to_use_context(
    gateway_for_dispatch, mock_channel, mock_rag_manager, tmp_path
):
    """/context use <id> should call use_context, not return memory fallback."""
    msg = _make_msg("/context use abc123")
    agent_cfg = MagicMock()
    handled = await gateway_for_dispatch._handle_rag_command(
        msg, agent_cfg, "default", tmp_path, "safe", mock_channel, "feishu"
    )
    assert handled is True
    mock_rag_manager.use_context.assert_called_once()
    # The first positional arg is the context_id
    assert mock_rag_manager.use_context.call_args[0][0] == "abc123"
    # The reply must NOT mention /memory
    sent_texts = [c.args[1] for c in mock_channel.send.call_args_list]
    combined = " ".join(sent_texts)
    assert "/memory" not in combined
    assert "Unknown" not in combined


@pytest.mark.asyncio
async def test_context_use_strips_trailing_text(
    gateway_for_dispatch, mock_channel, mock_rag_manager, tmp_path
):
    """/context use <id> with trailing natural-language text should still dispatch."""
    msg = _make_msg("/context use abc123 它里面关于 reindex 是怎么说的？")
    agent_cfg = MagicMock()
    handled = await gateway_for_dispatch._handle_rag_command(
        msg, agent_cfg, "default", tmp_path, "safe", mock_channel, "feishu"
    )
    assert handled is True
    mock_rag_manager.use_context.assert_called_once()
    # Only the first token should be used as the id
    assert mock_rag_manager.use_context.call_args[0][0] == "abc123"


@pytest.mark.asyncio
async def test_context_archive_dispatches(
    gateway_for_dispatch, mock_channel, mock_rag_manager, tmp_path
):
    msg = _make_msg("/context archive abc123")
    handled = await gateway_for_dispatch._handle_rag_command(
        msg, MagicMock(), "default", tmp_path, "safe", mock_channel, "feishu"
    )
    assert handled is True
    mock_rag_manager.archive_context.assert_called_once()


@pytest.mark.asyncio
async def test_context_delete_dispatches(
    gateway_for_dispatch, mock_channel, mock_rag_manager, tmp_path
):
    msg = _make_msg("/context delete abc123")
    handled = await gateway_for_dispatch._handle_rag_command(
        msg, MagicMock(), "default", tmp_path, "safe", mock_channel, "feishu"
    )
    assert handled is True
    mock_rag_manager.delete_context.assert_called_once()


@pytest.mark.asyncio
async def test_context_reindex_dispatches(
    gateway_for_dispatch, mock_channel, mock_rag_manager, tmp_path
):
    msg = _make_msg("/context reindex abc123")
    handled = await gateway_for_dispatch._handle_rag_command(
        msg, MagicMock(), "default", tmp_path, "safe", mock_channel, "feishu"
    )
    assert handled is True
    mock_rag_manager.reindex_context.assert_called_once()


@pytest.mark.asyncio
async def test_context_rebind_dispatches(
    gateway_for_dispatch, mock_channel, mock_rag_manager, tmp_path
):
    msg = _make_msg("/context rebind abc123 docs/new.md")
    handled = await gateway_for_dispatch._handle_rag_command(
        msg, MagicMock(), "default", tmp_path, "safe", mock_channel, "feishu"
    )
    assert handled is True
    mock_rag_manager.rebind_context.assert_called_once()
    args = mock_rag_manager.rebind_context.call_args
    assert args[0][0] == "abc123"
    assert args[0][1] == "docs/new.md"


@pytest.mark.asyncio
async def test_context_cleanup_dispatches(
    gateway_for_dispatch, mock_channel, mock_rag_manager, tmp_path
):
    msg = _make_msg("/context cleanup")
    handled = await gateway_for_dispatch._handle_rag_command(
        msg, MagicMock(), "default", tmp_path, "safe", mock_channel, "feishu"
    )
    assert handled is True
    mock_rag_manager.cleanup_lifecycle.assert_called_once()


@pytest.mark.asyncio
async def test_unknown_context_subcommand_says_context_not_memory(
    gateway_for_dispatch, mock_channel, tmp_path
):
    """Regression: unknown /context subcommand must NOT say 'Unknown /memory subcommand'."""
    msg = _make_msg("/context bogusverb foo")
    handled = await gateway_for_dispatch._handle_rag_command(
        msg, MagicMock(), "default", tmp_path, "safe", mock_channel, "feishu"
    )
    assert handled is True
    sent_texts = [c.args[1] for c in mock_channel.send.call_args_list]
    combined = " ".join(sent_texts)
    assert "/memory" not in combined, (
        f"Unknown /context subcommand must not mention /memory; got: {combined!r}"
    )
    assert "/context" in combined
    assert "bogusverb" in combined


@pytest.mark.asyncio
async def test_context_use_passes_real_session_id_not_none(
    gateway_for_dispatch, mock_channel, mock_rag_manager, tmp_path
):
    """Regression: ctx_dict must contain a real session_id, not None.

    Bug: router previously used getattr(msg, 'session_id', None), but
    InboundMessage has no session_id field, so the value was always None.
    RagManager.use_context() then returned 'missing session_id or agent_id'
    even though derive_session_id() could have produced a stable hash.
    """
    msg = _make_msg("/context use abc123")
    await gateway_for_dispatch._handle_rag_command(
        msg, MagicMock(), "default", tmp_path, "safe", mock_channel, "feishu"
    )

    # use_context is called as use_context(context_id, ctx=ctx_dict)
    call_kwargs = mock_rag_manager.use_context.call_args.kwargs
    ctx_passed = call_kwargs.get("ctx") or mock_rag_manager.use_context.call_args.args[1]

    # The session_id must be a non-empty string (derive_session_id output)
    assert ctx_passed.get("session_id"), (
        f"ctx['session_id'] must be a real string, not None/empty; got: {ctx_passed.get('session_id')!r}"
    )
    assert isinstance(ctx_passed["session_id"], str)
    assert len(ctx_passed["session_id"]) > 0
    # And the agent_id should also be present
    assert ctx_passed.get("agent_id") == "default"
    # And channel_name
    assert ctx_passed.get("channel_name") == "feishu"

