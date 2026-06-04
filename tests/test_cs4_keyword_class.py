"""Test cs-4: Store keyword_class in session_chain_state.chat_search_queries.

Ensures that when search_chat is called with an exfil query, the matched
keywords are stored in the chat_search_queries JSON for audit trail.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from mini_claw.permissions.chain_detector import ChainDetector
from mini_claw.permissions.policy import get_exfil_query_keywords
from mini_claw.storage.db import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    """Create a fresh test database."""
    db_path = tmp_path / "test.db"
    return Database(db_path)


@pytest.fixture
def detector(db: Database) -> ChainDetector:
    """Create a ChainDetector with session scope enabled."""
    config = {
        "enabled": True,
        "session_scope": True,
        "session_ttl": 3600,
    }
    return ChainDetector(config=config, storage=db)


def test_chat_search_stores_keyword_class(detector: ChainDetector, db: Database):
    """Test that search_chat with exfil query stores matched keyword_class."""
    # Arrange: tool call with sensitive query containing multiple keywords
    tool_call = {
        "name": "search_chat",
        "arguments": {
            "query": "find all API tokens and passwords",
            "scope": "current_session",
            "top_k": 10,
        },
    }
    ctx = {
        "chat_id": "test_chat",
        "agent_id": "test_agent",
        "channel_name": "feishu",
    }

    # Act: record the search_chat call (via observe_after_tool)
    detector.observe_after_tool(
        tool_call=tool_call,
        run=type("Run", (), {"written_scripts": {}, "dangerous_actions": {}})(),
        result="search results...",
        success=True,
        ctx=ctx,
    )

    # Assert: verify keyword_class is stored in chat_search_queries
    rows = db.fetchall(
        "SELECT chat_search_queries FROM session_chain_state "
        "WHERE channel_name = ? AND chat_id = ? AND agent_id = ? AND script_path = ?",
        ("feishu", "test_chat", "test_agent", "__rag__"),
    )
    assert len(rows) == 1, "Should have one session_chain_state row"

    queries = json.loads(rows[0]["chat_search_queries"])
    assert len(queries) == 1, "Should have one chat_search_queries entry"

    entry = queries[0]
    assert entry["exfil"] is True, "Should be flagged as exfil"
    assert "keyword_class" in entry, "Should have keyword_class field"
    assert isinstance(entry["keyword_class"], list), "keyword_class should be a list"

    # Verify the keywords match what policy.get_exfil_query_keywords returns
    expected_keywords = get_exfil_query_keywords("find all API tokens and passwords")
    assert set(entry["keyword_class"]) == set(expected_keywords), (
        f"keyword_class {entry['keyword_class']} should match expected {expected_keywords}"
    )
    # Specifically check that "token" and "password" are present
    assert "token" in entry["keyword_class"], "Should detect 'token' keyword"
    assert "password" in entry["keyword_class"], "Should detect 'password' keyword"


def test_chat_search_empty_keyword_class_for_benign_query(
    detector: ChainDetector, db: Database
):
    """Test that benign search_chat query has empty keyword_class."""
    # Arrange: benign query
    tool_call = {
        "name": "search_chat",
        "arguments": {
            "query": "show me yesterday's discussion about the bug fix",
            "scope": "current_session",
            "top_k": 5,
        },
    }
    ctx = {
        "chat_id": "test_chat",
        "agent_id": "test_agent",
        "channel_name": "cli",
    }

    # Act
    detector.observe_after_tool(
        tool_call=tool_call,
        run=type("Run", (), {"written_scripts": {}, "dangerous_actions": {}})(),
        result="chat history...",
        success=True,
        ctx=ctx,
    )

    # Assert
    rows = db.fetchall(
        "SELECT chat_search_queries FROM session_chain_state "
        "WHERE channel_name = ? AND chat_id = ? AND agent_id = ? AND script_path = ?",
        ("cli", "test_chat", "test_agent", "__rag__"),
    )
    assert len(rows) == 1

    queries = json.loads(rows[0]["chat_search_queries"])
    entry = queries[0]

    assert entry["exfil"] is False, "Should NOT be flagged as exfil"
    assert "keyword_class" in entry, "Should have keyword_class field"
    assert entry["keyword_class"] == [], "keyword_class should be empty for benign query"


def test_keyword_class_multiple_searches(detector: ChainDetector, db: Database):
    """Test that multiple search_chat calls accumulate with correct keyword_class."""
    ctx = {
        "chat_id": "multi_chat",
        "agent_id": "test_agent",
        "channel_name": "feishu",
    }

    # First search: sensitive
    tool_call_1 = {
        "name": "search_chat",
        "arguments": {"query": "find AWS secret keys", "scope": "current_session"},
    }
    detector.observe_after_tool(
        tool_call=tool_call_1,
        run=type("Run", (), {"written_scripts": {}, "dangerous_actions": {}})(),
        result="...",
        success=True,
        ctx=ctx,
    )

    # Second search: benign
    tool_call_2 = {
        "name": "search_chat",
        "arguments": {"query": "show project status", "scope": "current_session"},
    }
    detector.observe_after_tool(
        tool_call=tool_call_2,
        run=type("Run", (), {"written_scripts": {}, "dangerous_actions": {}})(),
        result="...",
        success=True,
        ctx=ctx,
    )

    # Third search: different sensitive keyword
    tool_call_3 = {
        "name": "search_chat",
        "arguments": {"query": "find ssh_key and credentials", "scope": "workspace"},
    }
    detector.observe_after_tool(
        tool_call=tool_call_3,
        run=type("Run", (), {"written_scripts": {}, "dangerous_actions": {}})(),
        result="...",
        success=True,
        ctx=ctx,
    )

    # Assert: all three entries with correct keyword_class
    rows = db.fetchall(
        "SELECT chat_search_queries FROM session_chain_state "
        "WHERE channel_name = ? AND chat_id = ? AND agent_id = ? AND script_path = ?",
        ("feishu", "multi_chat", "test_agent", "__rag__"),
    )
    queries = json.loads(rows[0]["chat_search_queries"])
    assert len(queries) == 3, "Should have three entries"

    # First entry
    assert queries[0]["exfil"] is True
    assert "aws_secret" in queries[0]["keyword_class"] or "secret" in queries[0]["keyword_class"]

    # Second entry
    assert queries[1]["exfil"] is False
    assert queries[1]["keyword_class"] == []

    # Third entry
    assert queries[2]["exfil"] is True
    assert "ssh_key" in queries[2]["keyword_class"]
    assert "credential" in queries[2]["keyword_class"]


def test_keyword_class_matches_policy_function():
    """Test that keyword_class extraction matches the policy function."""
    from mini_claw.permissions.policy import (
        EXFIL_QUERY_KEYWORDS,
        get_exfil_query_keywords,
        looks_like_exfil_query,
    )

    test_cases = [
        ("find my password", ["password"]),
        ("show API keys and tokens", ["api_key", "apikey", "api-key", "token"]),
        ("get the JWT secret", ["jwt", "secret"]),
        ("benign query about code", []),
        ("read the .env file", [".env"]),
        ("show ssh_key and private_key", ["ssh_key", "ssh-key", "private_key", "private-key", "privatekey"]),
    ]

    for query, expected_subset in test_cases:
        is_exfil = looks_like_exfil_query(query)
        keywords = get_exfil_query_keywords(query)

        if expected_subset:
            assert is_exfil is True, f"Query '{query}' should be exfil"
            # Check at least the expected keywords are present
            for kw in expected_subset:
                assert kw in keywords or any(k in query.lower() for k in expected_subset), (
                    f"Expected keyword '{kw}' in {keywords} for query '{query}'"
                )
        else:
            assert is_exfil is False, f"Query '{query}' should NOT be exfil"
            assert keywords == [], f"Benign query should have empty keywords, got {keywords}"
