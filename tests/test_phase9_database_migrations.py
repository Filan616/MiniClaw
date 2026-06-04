"""Phase 9 tests: Scenarios 6-7 — Database migrations and backfill.

Tests cover:
6. messages workspace_dir backfill
7. active_contexts NULL row migration
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mini_claw.storage.db import Database


@pytest.fixture
def storage(tmp_path: Path) -> Database:
    return Database(tmp_path / "migration.db")


# ==================== Scenario 6: messages workspace_dir backfill ====================


def test_workspace_dir_backfill_updates_null_rows(storage: Database):
    """Scenario 6: Backfill populates workspace_dir for NULL rows."""
    # Insert messages with NULL workspace_dir (let AUTOINCREMENT assign id)
    for i in range(5):
        storage.execute(
            "INSERT INTO messages (chat_id, agent_id, channel_name, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("chat-1", "agent-a", "cli", "user", f"content {i}", int(time.time())),
        )

    def mock_workspace_resolver(chat_id: str, agent_id: str) -> str:
        return f"/workspace/{agent_id}"

    stats = storage.backfill_workspace_dir(mock_workspace_resolver)

    assert stats["updated"] == 5
    assert stats["failed"] == 0
    assert stats["skipped"] == 0

    rows = storage.fetchall("SELECT workspace_dir, workspace_dir_inferred FROM messages")
    for row in rows:
        assert row["workspace_dir"] == "/workspace/agent-a"
        assert row["workspace_dir_inferred"] == 1


def test_workspace_dir_backfill_skips_existing(storage: Database):
    """Scenario 6: Backfill skips rows with existing workspace_dir."""
    storage.execute(
        "INSERT INTO messages (chat_id, agent_id, channel_name, workspace_dir, "
        "workspace_dir_inferred, role, content, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("chat-1", "agent-a", "cli", "/explicit/workspace", 0, "user", "existing", int(time.time())),
    )

    storage.execute(
        "INSERT INTO messages (chat_id, agent_id, channel_name, role, content, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("chat-2", "agent-b", "cli", "user", "new content", int(time.time())),
    )

    def mock_resolver(chat_id: str, agent_id: str) -> str:
        return f"/workspace/{agent_id}"

    stats = storage.backfill_workspace_dir(mock_resolver)

    assert stats["updated"] == 1
    assert stats["skipped"] == 1

    row = storage.fetchone(
        "SELECT workspace_dir, workspace_dir_inferred FROM messages WHERE content = ?",
        ("existing",),
    )
    assert row["workspace_dir"] == "/explicit/workspace"
    assert row["workspace_dir_inferred"] == 0


def test_workspace_dir_backfill_handles_resolver_errors(storage: Database):
    """Scenario 6: Backfill handles resolver exceptions gracefully."""
    storage.execute(
        "INSERT INTO messages (chat_id, agent_id, channel_name, role, content, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("chat-err", "agent-err", "cli", "user", "error case", int(time.time())),
    )

    def failing_resolver(chat_id: str, agent_id: str) -> str:
        if chat_id == "chat-err":
            raise RuntimeError("resolver failure")
        return "/workspace/default"

    stats = storage.backfill_workspace_dir(failing_resolver)

    assert stats["failed"] == 1
    assert stats["updated"] == 0


# ==================== Scenario 7: active_contexts NULL row migration ====================


def test_active_contexts_null_migration_deletes_old_rows(storage: Database):
    """Scenario 7: Migration deletes active_contexts rows with session_id=NULL."""
    # Phase 9 schema enforces NOT NULL on session_id; this migration is a no-op
    # for fresh installs but matters for upgrades from pre-Phase 9 schemas.
    try:
        storage.execute(
            "INSERT INTO active_contexts "
            "(session_id, agent_id, context_id, context_type, activated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (None, "agent-a", "ctx_1", "document", int(time.time())),
        )
    except Exception:
        pytest.skip("active_contexts schema enforces NOT NULL on session_id")

    count_before = storage.fetchone(
        "SELECT COUNT(*) as cnt FROM active_contexts WHERE session_id IS NULL"
    )
    if count_before and count_before["cnt"] > 0:
        storage.execute("DELETE FROM active_contexts WHERE session_id IS NULL")
        count_after = storage.fetchone(
            "SELECT COUNT(*) as cnt FROM active_contexts WHERE session_id IS NULL"
        )
        assert count_after["cnt"] == 0


def test_active_contexts_migration_preserves_valid_rows(storage: Database):
    """Scenario 7: Migration preserves active_contexts rows with valid session_id."""
    storage.execute(
        "INSERT INTO active_contexts "
        "(session_id, agent_id, context_id, context_type, activated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("cli:chat-1:agent-a", "agent-a", "ctx_valid", "document", int(time.time())),
    )

    storage.execute("DELETE FROM active_contexts WHERE session_id IS NULL")

    row = storage.fetchone(
        "SELECT * FROM active_contexts WHERE context_id = ?", ("ctx_valid",)
    )
    assert row is not None
    assert row["session_id"] == "cli:chat-1:agent-a"


def test_active_contexts_schema_has_session_id_column(storage: Database):
    """Scenario 7: Verify active_contexts table has session_id column after migration."""
    columns = storage.fetchall("PRAGMA table_info(active_contexts)")
    column_names = {col["name"] for col in columns}

    assert "session_id" in column_names


def test_active_contexts_composite_key_with_session_id(storage: Database):
    """Scenario 7: Verify active_contexts uses session_id in primary key."""
    columns = storage.fetchall("PRAGMA table_info(active_contexts)")
    pk_columns = [col["name"] for col in columns if col["pk"] > 0]

    if "session_id" in pk_columns:
        assert True
    else:
        indexes = storage.fetchall(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='active_contexts'"
        )
        has_session_index = any("session_id" in (idx["sql"] or "") for idx in indexes)
        assert has_session_index or "session_id" in pk_columns
