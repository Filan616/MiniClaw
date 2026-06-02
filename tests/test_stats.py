"""Tests for Stats Token 聚合 (Phase B.4)."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mini_claw.agent.context import AgentContext
from mini_claw.agent.loop import _persist_tool_call
from mini_claw.storage.db import Database


@pytest.fixture
def storage(tmp_path: Path) -> Database:
    return Database(tmp_path / "stats_test.db")


def test_agent_runs_total_tokens_column_exists(storage: Database):
    """Phase B.4: agent_runs.total_tokens column should exist."""
    cols = storage.fetchall("PRAGMA table_info(agent_runs)")
    col_names = [c["name"] for c in cols]
    assert "total_tokens" in col_names
    assert "total_cost_usd" in col_names


def test_tool_calls_duration_ms_column_exists(storage: Database):
    """Phase B.4: tool_calls.duration_ms column should exist."""
    cols = storage.fetchall("PRAGMA table_info(tool_calls)")
    col_names = [c["name"] for c in cols]
    assert "duration_ms" in col_names


def test_persist_tool_call_records_duration(storage: Database, tmp_path: Path):
    """Phase B.4: _persist_tool_call writes a row with duration_ms."""
    # Create parent agent_runs row to satisfy any FK
    now = int(time.time())
    storage.execute(
        "INSERT INTO agent_runs (id, chat_id, agent_id, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("run_1", "chat_1", "agent_1", "done", now, now),
    )

    ctx = AgentContext(
        chat_id="chat_1",
        agent_id="agent_1",
        workspace_dir=tmp_path,
        storage=storage,
    )

    tc = SimpleNamespace(
        id="tc_1",
        name="run_shell",
        arguments={"command": "ls"},
    )

    _persist_tool_call(ctx, "run_1", tc, "ok output", "ok", duration_ms=42)

    row = storage.fetchone(
        "SELECT id, run_id, tool_name, status, duration_ms FROM tool_calls WHERE id = ?",
        ("tc_1",),
    )
    assert row is not None
    assert row["duration_ms"] == 42
    assert row["status"] == "ok"
    assert row["tool_name"] == "run_shell"


def test_persist_tool_call_no_storage_is_safe(tmp_path: Path):
    """Phase B.4: _persist_tool_call without storage should not crash."""
    ctx = AgentContext(
        chat_id="chat_1",
        agent_id="agent_1",
        workspace_dir=tmp_path,
        storage=None,
    )
    tc = SimpleNamespace(id="tc_2", name="x", arguments={})
    # Should not raise
    _persist_tool_call(ctx, "run_x", tc, "result", "ok", duration_ms=10)


def test_stats_session_query_aggregates_tokens(storage: Database):
    """Phase B.4: SQL aggregation works for stats session command."""
    now = int(time.time())
    # Insert two runs for same chat
    storage.execute(
        "INSERT INTO agent_runs "
        "(id, chat_id, agent_id, status, prompt_tokens, completion_tokens, total_tokens, "
        " created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("r1", "c1", "a1", "done", 100, 50, 150, now, now),
    )
    storage.execute(
        "INSERT INTO agent_runs "
        "(id, chat_id, agent_id, status, prompt_tokens, completion_tokens, total_tokens, "
        " created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("r2", "c1", "a1", "done", 200, 80, 280, now, now),
    )

    rows = storage.fetchall(
        "SELECT SUM(total_tokens) AS total FROM agent_runs WHERE chat_id = ?",
        ("c1",),
    )
    assert rows[0]["total"] == 430


def test_stats_top_tools_query(storage: Database):
    """Phase B.4: top tools query orders by avg duration."""
    now = int(time.time())
    storage.execute(
        "INSERT INTO agent_runs (id, chat_id, agent_id, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("r1", "c1", "a1", "done", now, now),
    )
    # Insert tool calls with different durations
    for i, (tool, dur) in enumerate([("fast", 10), ("slow", 500), ("medium", 100)]):
        storage.execute(
            "INSERT INTO tool_calls "
            "(id, run_id, tool_name, status, duration_ms, created_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"tc_{i}", "r1", tool, "ok", dur, now, now),
        )

    rows = storage.fetchall(
        "SELECT tool_name, AVG(duration_ms) AS avg_ms FROM tool_calls "
        "WHERE duration_ms IS NOT NULL "
        "GROUP BY tool_name ORDER BY avg_ms DESC LIMIT 3"
    )
    assert rows[0]["tool_name"] == "slow"
    assert rows[1]["tool_name"] == "medium"
    assert rows[2]["tool_name"] == "fast"
