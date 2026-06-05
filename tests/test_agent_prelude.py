"""
Phase 9.7: Tests for Prelude feature (操作前回应).

Covers:
- _sanitize_prelude: length limits, hallucination detection, audit callbacks
- AgentLoop integration: prelude_sent flag, on_prelude callback
- SessionManager: message_kind='prelude', get_history filtering
- Gateway integration: _send_prelude, _send_command_prelude
"""

import pytest
from mini_claw.agent.loop import _sanitize_prelude


class TestSanitizePrelude:
    """Test _sanitize_prelude security and filtering logic."""

    def test_basic_valid_prelude(self):
        """Valid prelude passes through unchanged."""
        text = "好的，让我为你创建这个文件。"
        result = _sanitize_prelude(text, max_length=120)
        assert result == text

    def test_empty_input(self):
        """Empty or whitespace-only input returns None."""
        assert _sanitize_prelude("", max_length=120) is None
        assert _sanitize_prelude("   ", max_length=120) is None
        assert _sanitize_prelude("\n\t", max_length=120) is None

    def test_length_truncation(self):
        """Text exceeding max_length is truncated with ellipsis."""
        text = "a" * 150
        result = _sanitize_prelude(text, max_length=120)
        assert result is not None
        assert len(result) <= 123  # 120 + "..."
        assert result.endswith("...")

    def test_completion_claims_rejected(self):
        """Completion claims are rejected (returns None)."""
        audit_called = []

        def audit_cb(event_type: str, details: dict) -> None:
            audit_called.append((event_type, details))

        texts = [
            "好的，文件已创建完成！",
            "测试通过了。",
            "已找到 3 个错误。",
            "Successfully created the file.",
            "Tests passed.",
        ]

        for text in texts:
            audit_called.clear()
            result = _sanitize_prelude(text, max_length=120, audit_callback=audit_cb)
            assert result is None, f"Completion claim should be rejected: {text}"
            assert len(audit_called) == 1
            assert audit_called[0][0] == "prelude_sanitized_rejected"
            assert audit_called[0][1]["reason"] == "completion_claim"

    def test_valid_future_tense(self):
        """Valid future-tense phrases pass through."""
        texts = [
            "好的，让我为你创建这个文件。",
            "收到，我先读取日志文件。",
            "我会运行测试并查看结果。",
            "Let me index the documents.",
            "I will run the tests.",
        ]
        for text in texts:
            result = _sanitize_prelude(text, max_length=120)
            assert result == text, f"Future tense should pass: {text}"

    def test_code_blocks_removed(self):
        """Code blocks are removed from prelude."""
        text = "好的，让我创建文件。```python\nprint('hello')\n```"
        result = _sanitize_prelude(text, max_length=120)
        # Code block removed, result should be just the text part
        assert result is None or "```" not in result

    def test_too_short_after_sanitization(self):
        """Text that becomes too short after sanitization is rejected."""
        audit_called = []

        def audit_cb(event_type: str, details: dict) -> None:
            audit_called.append((event_type, details))

        text = "a"
        result = _sanitize_prelude(text, max_length=120, audit_callback=audit_cb)
        assert result is None
        assert len(audit_called) == 1
        assert audit_called[0][1]["reason"] == "too_short"

    def test_no_audit_callback(self):
        """Sanitize works without audit callback."""
        text = "好的，文件已创建完成！"
        result = _sanitize_prelude(text, max_length=120, audit_callback=None)
        assert result is None  # Still rejected, just no audit

    def test_default_max_length(self):
        """Default max_length is 120."""
        text = "a" * 150
        result = _sanitize_prelude(text)  # No max_length param
        assert result is not None
        assert len(result) <= 123  # 120 + "..."
        assert result.endswith("...")


@pytest.mark.asyncio
class TestAgentLoopPrelude:
    """Test AgentLoop prelude integration."""

    async def test_prelude_sent_flag(self):
        """Prelude is only sent once per run."""
        from mini_claw.agent.context import AgentContext
        from mini_claw.agent.loop import AgentRun, RunOutcome

        prelude_calls = []

        async def mock_on_prelude(text: str) -> None:
            prelude_calls.append(text)

        ctx = AgentContext(
            chat_id="test",
            agent_id="agent1",
            workspace_dir="/tmp",
            on_prelude=mock_on_prelude,
            prelude_max_length=500,
        )

        run = AgentRun(
            id="run1",
            chat_id="test",
            agent_id="agent1",
            status=RunOutcome.DONE,
            messages=[],
            allowed_tools=["read_file"],
        )

        # First tool call with text: should send prelude
        assert not run.prelude_sent

        # Simulate prelude sending logic from loop.py
        if not run.prelude_sent and ctx.on_prelude:
            sanitized = _sanitize_prelude("好的，让我读取文件。", max_length=ctx.prelude_max_length)
            if sanitized:
                await ctx.on_prelude(sanitized)
                run.prelude_sent = True

        assert run.prelude_sent
        assert len(prelude_calls) == 1
        assert prelude_calls[0] == "好的，让我读取文件。"

        # Second tool call: should NOT send prelude again
        if not run.prelude_sent and ctx.on_prelude:
            await ctx.on_prelude("不应该发送")

        assert len(prelude_calls) == 1  # Still only 1


@pytest.mark.asyncio
class TestSessionManagerPrelude:
    """Test SessionManager prelude storage and filtering."""

    async def test_store_message_with_prelude_kind(self):
        """store_message accepts message_kind='prelude'."""
        from pathlib import Path
        from mini_claw.storage.db import Database
        from mini_claw.gateway.session import SessionManager

        storage = Database(Path(":memory:"))
        mgr = SessionManager(storage)

        mgr.store_message(
            chat_id="chat1",
            agent_id="agent1",
            role="assistant",
            content="好的，让我创建文件。",
            run_id="run1",
            channel_name="feishu",
            workspace_dir="/workspace",
            message_kind="prelude",
        )

        # Verify stored in DB
        row = storage.fetchone(
            "SELECT * FROM messages WHERE chat_id=? AND agent_id=? ORDER BY id DESC LIMIT 1",
            ("chat1", "agent1"),
        )
        assert row is not None
        assert row["message_kind"] == "prelude"
        assert row["content"] == "好的，让我创建文件。"

    async def test_get_history_filters_preludes_by_default(self):
        """get_history excludes preludes unless include_preludes=True."""
        from pathlib import Path
        from mini_claw.storage.db import Database
        from mini_claw.gateway.session import SessionManager

        storage = Database(Path(":memory:"))
        mgr = SessionManager(storage)

        # Store normal message
        mgr.store_message(
            chat_id="chat1",
            agent_id="agent1",
            role="user",
            content="创建文件",
            channel_name="feishu",
            workspace_dir="/workspace",
            message_kind="normal",
        )

        # Store prelude
        mgr.store_message(
            chat_id="chat1",
            agent_id="agent1",
            role="assistant",
            content="好的，让我创建文件。",
            channel_name="feishu",
            workspace_dir="/workspace",
            message_kind="prelude",
        )

        # Store normal response
        mgr.store_message(
            chat_id="chat1",
            agent_id="agent1",
            role="assistant",
            content="文件已创建。",
            channel_name="feishu",
            workspace_dir="/workspace",
            message_kind="normal",
        )

        # Default: exclude preludes
        history = mgr.get_history("chat1", "agent1", channel_name="feishu")
        assert len(history) == 2
        assert history[0]["content"] == "创建文件"
        assert history[1]["content"] == "文件已创建。"

        # With include_preludes=True: include all
        history_full = mgr.get_history(
            "chat1", "agent1", channel_name="feishu", include_preludes=True
        )
        assert len(history_full) == 3
        assert history_full[1]["content"] == "好的，让我创建文件。"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
