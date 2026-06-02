"""SQLite storage layer for Mini-Claw."""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator


class Database:
    """Thin wrapper around sqlite3 for Mini-Claw persistence."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.init_tables()

    # ------------------------------------------------------------------
    # Transaction helper
    # ------------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Cursor, None, None]:
        """Context manager that commits on success, rolls back on error."""
        cur = self._conn.cursor()
        try:
            cur.execute("BEGIN")
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ------------------------------------------------------------------
    # Table initialization
    # ------------------------------------------------------------------

    def init_tables(self) -> None:
        """Create all tables and indexes if they do not exist."""
        self._conn.executescript(_SCHEMA_SQL)
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """Apply schema migrations for existing databases."""
        # Migration 1: Add sandbox_mode_override to sessions
        try:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN sandbox_mode_override TEXT"
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            # Column already exists, safe to ignore
            pass

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        cursor = self._conn.execute(sql, params)
        self._conn.commit()
        return cursor

    def executemany(self, sql: str, seq: list[tuple]) -> sqlite3.Cursor:
        cursor = self._conn.executemany(sql, seq)
        self._conn.commit()
        return cursor

    def fetchone(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def fetchall(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def exists(self, sql: str, params: tuple = ()) -> bool:
        return self._conn.execute(sql, params).fetchone() is not None

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # Scheduled tasks
    # ------------------------------------------------------------------

    def list_scheduled_tasks(self) -> list[dict[str, Any]]:
        """Return all scheduled tasks."""
        return self.fetchall(
            "SELECT id, chat_id, agent_id, cron, instruction AS description, "
            "CASE WHEN enabled = 1 THEN 'active' ELSE 'paused' END AS status "
            "FROM scheduled_tasks ORDER BY created_at"
        )

    def remove_scheduled_task(self, task_id: str) -> bool:
        """Remove a scheduled task by ID. Returns True if deleted."""
        cur = self._conn.execute(
            "DELETE FROM scheduled_tasks WHERE id = ?", (task_id,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Agent runs
    # ------------------------------------------------------------------

    def get_agent_run(self, run_id: str) -> dict[str, Any] | None:
        """Get an agent run with its tool calls."""
        run = self.fetchone(
            "SELECT id, chat_id, agent_id, status, user_message, "
            "final_answer, iterations, prompt_tokens AS input_tokens, "
            "completion_tokens AS output_tokens, created_at, updated_at "
            "FROM agent_runs WHERE id = ?",
            (run_id,),
        )
        if run is None:
            return None

        # Calculate duration
        created = run.get("created_at", 0) or 0
        updated = run.get("updated_at", 0) or 0
        run["duration_ms"] = (updated - created) if updated > created else 0

        # Attach tool calls
        tool_calls = self.fetchall(
            "SELECT tool_name AS tool, status FROM tool_calls "
            "WHERE run_id = ? ORDER BY created_at",
            (run_id,),
        )
        run["tool_calls"] = tool_calls
        return run


# ======================================================================
# Schema SQL
# ======================================================================

_SCHEMA_SQL = """\
-- Basic group
CREATE TABLE IF NOT EXISTS sessions (
    chat_id      TEXT PRIMARY KEY,
    agent_id     TEXT NOT NULL,
    created_at   INTEGER,
    updated_at   INTEGER,
    sandbox_mode_override TEXT  -- "safe", "bypass", or NULL (use config default)
);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      TEXT NOT NULL,
    agent_id     TEXT NOT NULL,
    run_id       TEXT,
    role         TEXT NOT NULL,
    content      TEXT,
    tool_calls   TEXT,
    tool_call_id TEXT,
    created_at   INTEGER,
    FOREIGN KEY (chat_id) REFERENCES sessions(chat_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_run ON messages(run_id, id);

CREATE TABLE IF NOT EXISTS processed_events (
    event_id     TEXT PRIMARY KEY,
    processed_at INTEGER
);

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id           TEXT PRIMARY KEY,
    chat_id      TEXT NOT NULL,
    agent_id     TEXT NOT NULL,
    cron         TEXT NOT NULL,
    instruction  TEXT NOT NULL,
    enabled      INTEGER DEFAULT 1,
    created_at   INTEGER
);

CREATE TABLE IF NOT EXISTS user_memory (
    agent_id     TEXT NOT NULL,
    key          TEXT NOT NULL,
    value        TEXT,
    PRIMARY KEY (agent_id, key)
);
-- Observability group
CREATE TABLE IF NOT EXISTS agent_runs (
    id                  TEXT PRIMARY KEY,
    chat_id             TEXT NOT NULL,
    agent_id            TEXT NOT NULL,
    status              TEXT NOT NULL,
    user_message        TEXT,
    final_answer        TEXT,
    iterations          INTEGER DEFAULT 0,
    seen_calls          TEXT,
    pending_approval_id TEXT,
    pending_tool_call   TEXT,
    prompt_tokens       INTEGER DEFAULT 0,
    completion_tokens   INTEGER DEFAULT 0,
    created_at          INTEGER,
    updated_at          INTEGER
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    arguments    TEXT,
    result       TEXT,
    status       TEXT NOT NULL,
    created_at   INTEGER,
    finished_at  INTEGER,
    FOREIGN KEY (run_id) REFERENCES agent_runs(id)
);

CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    chat_id      TEXT NOT NULL,
    agent_id     TEXT NOT NULL,
    type         TEXT NOT NULL,
    status       TEXT NOT NULL,
    instruction  TEXT NOT NULL,
    run_id       TEXT,
    result       TEXT,
    created_at   INTEGER,
    updated_at   INTEGER
);

-- Approval group
CREATE TABLE IF NOT EXISTS pending_approvals (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    chat_id      TEXT NOT NULL,
    agent_id     TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    tool_args    TEXT NOT NULL,
    status       TEXT NOT NULL,
    created_at   INTEGER,
    expires_at   INTEGER
);

CREATE TABLE IF NOT EXISTS session_grants (
    chat_id      TEXT NOT NULL,
    agent_id     TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    expires_at   INTEGER,
    PRIMARY KEY (chat_id, agent_id, tool_name)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id           TEXT PRIMARY KEY,
    content      TEXT NOT NULL,
    created_at   INTEGER
);
"""
