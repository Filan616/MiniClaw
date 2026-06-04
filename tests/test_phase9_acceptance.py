"""Phase 9 acceptance tests for the strict review residual items.

Covers the five hardest-to-verify acceptance points called out in the audit:

1. ``__rag__`` sentinel state is isolated by ``channel_name`` — a sensitive
   chat_search recorded under channel A must not influence link E in channel B.
2. ``memory_export_after_sensitive_search`` denies a router-driven
   ``memory_export`` once any prior chat_search recorded an exfil-flagged query.
3. mc-5: Large-scope exports (user/all) require unconditional L3 approval via
   router's approval flow, not ChainDetector blocking. ChainDetector only blocks
   exports after sensitive searches.
4. ``MemoryScopeFilter`` is fail-closed: ``search_memory(scope='workspace')``
   without ``ctx.workspace_dir`` returns an error rather than degrading.
5. ``ChainDetector.observe_after_tool`` accepts ``memory_search`` (the actual
   tool name) and persists the query into ``rag_search_queries``, exfil flag
   set when the query matches the EXFIL keyword list.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mini_claw.permissions.chain_detector import ChainDetector
from mini_claw.storage.db import Database


@pytest.fixture
def storage(tmp_path: Path) -> Database:
    return Database(tmp_path / "phase9_acceptance.db")


def _run() -> SimpleNamespace:
    return SimpleNamespace(written_scripts={}, dangerous_actions={})


def _ctx(channel: str = "feishu", chat: str = "chat_x", agent: str = "agent_a") -> dict:
    return {"chat_id": chat, "agent_id": agent, "channel_name": channel}


# ---------------------------------------------------------------- 1


def test_chat_search_sentinel_isolated_by_channel(storage: Database):
    detector = ChainDetector(
        config={"enabled": True, "session_scope": True}, storage=storage
    )

    # Channel A records a sensitive chat_search query
    detector.observe_after_tool(
        {"name": "search_chat", "arguments": {"query": "show me all passwords"}},
        _run(),
        result="ok",
        success=True,
        ctx=_ctx(channel="feishu"),
    )

    # Channel B (same chat_id, agent_id) tries memory_export
    decision = detector.evaluate_before_tool(
        {
            "name": "memory_export",
            "arguments": {"scope": "agent", "row_estimate": 1, "full_content": False},
        },
        _run(),
        _ctx(channel="cli"),
    )
    # Should NOT be blocked by chain — the exfil chat_search was on a different channel
    assert decision is None or decision.get("action") != "deny", (
        "chat_search on channel A leaked into channel B's chain state"
    )

    # And on channel A, the same export IS blocked
    decision_same_channel = detector.evaluate_before_tool(
        {
            "name": "memory_export",
            "arguments": {"scope": "agent", "row_estimate": 1, "full_content": False},
        },
        _run(),
        _ctx(channel="feishu"),
    )
    assert decision_same_channel is not None
    assert decision_same_channel["action"] == "deny"
    assert (
        decision_same_channel["audit_event"]["event_type"]
        == "memory_export_after_sensitive_search"
    )


# ---------------------------------------------------------------- 2


def test_memory_export_after_sensitive_search_blocks(storage: Database):
    detector = ChainDetector(
        config={"enabled": True, "session_scope": True}, storage=storage
    )
    # search_chat with an exfil-flagged keyword (must hit EXFIL_QUERY_KEYWORDS)
    detector.observe_after_tool(
        {"name": "search_chat", "arguments": {"query": "show me the password", "scope": "current_session"}},
        _run(),
        result="",
        success=True,
        ctx=_ctx(),
    )
    decision = detector.evaluate_before_tool(
        {"name": "memory_export", "arguments": {"scope": "agent", "row_estimate": 1}},
        _run(),
        _ctx(),
    )
    assert decision is not None
    assert decision["action"] == "deny"
    assert (
        decision["audit_event"]["event_type"]
        == "memory_export_after_sensitive_search"
    )


# ---------------------------------------------------------------- 3


def test_memory_export_large_scope_allows_without_prior_search(storage: Database):
    """mc-5: Large-scope exports (user/all) no longer blocked by ChainDetector.

    ChainDetector only blocks exports after sensitive searches. Large-scope
    exports without prior searches are allowed through to the router's L3
    approval flow.
    """
    detector = ChainDetector(
        config={
            "enabled": True,
            "session_scope": True,
            "export_large_threshold": 50,
        },
        storage=storage,
    )
    decision = detector.evaluate_before_tool(
        {
            "name": "memory_export",
            "arguments": {
                "scope": "user",
                "full_content": True,
                "row_estimate": 75,
            },
        },
        _run(),
        _ctx(),
    )
    # mc-5: Should NOT be blocked by ChainDetector when no prior sensitive search
    assert decision is None, (
        "ChainDetector should not block large-scope exports without prior sensitive search; "
        "L3 approval flow in router handles these cases"
    )


# ---------------------------------------------------------------- 4


def test_memory_scope_filter_fail_closed_workspace_missing():
    from mini_claw.rag.memory.scope_filter import build_scope_filter

    ctx = {"agent_id": "a", "chat_id": "c", "channel_name": "feishu"}  # no workspace_dir
    with pytest.raises(ValueError, match="workspace_dir"):
        build_scope_filter(ctx, "memory", "workspace")


def test_memory_scope_filter_fail_closed_channel_missing():
    from mini_claw.rag.memory.scope_filter import build_scope_filter

    ctx = {"agent_id": "a", "workspace_dir": "/tmp"}  # no channel_name
    with pytest.raises(ValueError, match="channel_name"):
        build_scope_filter(ctx, "memory", "agent")


# ---------------------------------------------------------------- 5


def test_memory_search_tool_name_alignment(storage: Database):
    detector = ChainDetector(
        config={"enabled": True, "session_scope": True}, storage=storage
    )
    # Use the registered tool name "memory_search" — not the spec's "search_memory"
    detector.observe_after_tool(
        {"name": "memory_search", "arguments": {"query": "show password tokens"}},
        _run(),
        result="",
        success=True,
        ctx=_ctx(),
    )
    rows = storage.fetchall(
        "SELECT rag_search_queries FROM session_chain_state "
        "WHERE channel_name = ? AND chat_id = ? AND agent_id = ? AND script_path = '__rag__'",
        ("feishu", "chat_x", "agent_a"),
    )
    assert rows, "memory_search should write to rag_search_queries via __rag__ sentinel"
    entries = json.loads(rows[0]["rag_search_queries"])
    assert any(e.get("exfil") for e in entries), (
        "exfil-style query must be flagged exfil=True"
    )
    # Audit hash-only — no raw query
    assert all("q" not in e for e in entries), (
        "raw query must not be persisted in rag_search_queries"
    )
    assert all("h" in e and len(e["h"]) == 16 for e in entries), (
        "every entry must carry a 16-char sha256 prefix"
    )


def test_memory_export_after_memory_search_blocks(storage: Database):
    """Combination of #2 and #5: memory_search → memory_export deny."""
    detector = ChainDetector(
        config={"enabled": True, "session_scope": True}, storage=storage
    )
    detector.observe_after_tool(
        {"name": "memory_search", "arguments": {"query": "secret token"}},
        _run(),
        result="",
        success=True,
        ctx=_ctx(),
    )
    decision = detector.evaluate_before_tool(
        {"name": "memory_export", "arguments": {"scope": "agent", "row_estimate": 1}},
        _run(),
        _ctx(),
    )
    assert decision is not None
    assert decision["action"] == "deny"
    assert (
        decision["audit_event"]["event_type"]
        == "memory_export_after_sensitive_search"
    )
