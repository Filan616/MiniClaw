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
        self._conn.execute("PRAGMA busy_timeout=5000")  # Wait up to 5s for lock
        self.init_tables()

    # ------------------------------------------------------------------
    # Transaction helper
    # ------------------------------------------------------------------

    @contextmanager
    def transaction(self, immediate: bool = False) -> Generator[sqlite3.Cursor, None, None]:
        """Context manager that commits on success, rolls back on error.

        Args:
            immediate: If True, use BEGIN IMMEDIATE for write transactions (default False).
                      Use this for operations that will write to avoid lock upgrade deadlocks.
        """
        cur = self._conn.cursor()
        try:
            if immediate:
                cur.execute("BEGIN IMMEDIATE")
            else:
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
        self._pre_migrate_processed_events()
        self._conn.executescript(_SCHEMA_SQL)
        self._migrate_schema()

    def _pre_migrate_processed_events(self) -> None:
        """Upgrade old processed_events before schema indexes are created."""
        table = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='processed_events'"
        ).fetchone()
        if table is None:
            return

        columns = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(processed_events)").fetchall()
        }
        if "started_at" in columns and "status" in columns and "heartbeat_at" in columns:
            return

        rows = self._conn.execute("SELECT * FROM processed_events").fetchall()
        self._conn.executescript(
            """
            ALTER TABLE processed_events RENAME TO processed_events_old;
            CREATE TABLE processed_events (
                event_id TEXT PRIMARY KEY,
                channel_name TEXT DEFAULT 'feishu',
                chat_id TEXT,
                status TEXT NOT NULL,
                run_id TEXT,
                started_at INTEGER NOT NULL,
                heartbeat_at INTEGER NOT NULL,
                finished_at INTEGER,
                error TEXT,
                attempt_count INTEGER DEFAULT 1
            );
            """
        )
        now = int(time.time())
        for row in rows:
            data = dict(row)
            ts = data.get("processed_at") or data.get("started_at") or now
            self._conn.execute(
                "INSERT OR IGNORE INTO processed_events "
                "(event_id, channel_name, chat_id, status, run_id, started_at, heartbeat_at, finished_at, error, attempt_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    data.get("event_id"),
                    data.get("channel_name") or "feishu",
                    data.get("chat_id"),
                    data.get("status") or "handled",
                    data.get("run_id"),
                    ts,
                    data.get("heartbeat_at") or ts,
                    data.get("finished_at") or ts,
                    data.get("error"),
                    data.get("attempt_count") or 1,
                ),
            )
        self._conn.execute("DROP TABLE processed_events_old")
        self._conn.commit()

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

        # Migration 2: Upgrade processed_events table with heartbeat and status fields
        try:
            # Check if old schema exists
            cursor = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='processed_events'"
            )
            row = cursor.fetchone()
            if row and "status" not in row[0]:
                # Old schema detected, migrate to new schema
                self._conn.executescript("""
                    -- Rename old table
                    ALTER TABLE processed_events RENAME TO processed_events_old;

                    -- Create new table with full schema
                    CREATE TABLE processed_events (
                        event_id TEXT PRIMARY KEY,
                        channel_name TEXT DEFAULT 'feishu',
                        chat_id TEXT,
                        status TEXT NOT NULL,
                        run_id TEXT,
                        started_at INTEGER NOT NULL,
                        heartbeat_at INTEGER NOT NULL,
                        finished_at INTEGER,
                        error TEXT,
                        attempt_count INTEGER DEFAULT 1
                    );

                    CREATE INDEX idx_processed_events_started_at ON processed_events(started_at);
                    CREATE INDEX idx_processed_events_status ON processed_events(status);
                    CREATE INDEX idx_processed_events_heartbeat ON processed_events(heartbeat_at);

                    -- Migrate old data
                    INSERT INTO processed_events (event_id, channel_name, chat_id, status, started_at, heartbeat_at, finished_at)
                    SELECT event_id, 'feishu', NULL, 'handled', processed_at, processed_at, processed_at
                    FROM processed_events_old;

                    -- Drop old table
                    DROP TABLE processed_events_old;
                """)
                self._conn.commit()
        except sqlite3.OperationalError:
            pass

        # Migration 3: Add bypass TTL fields to sessions
        migrations = [
            "ALTER TABLE sessions ADD COLUMN sandbox_mode_expires_at INTEGER",
            "ALTER TABLE sessions ADD COLUMN sandbox_mode_persistent INTEGER DEFAULT 0",
            "ALTER TABLE sessions ADD COLUMN sandbox_mode_single_use INTEGER DEFAULT 0",
        ]
        for migration in migrations:
            try:
                self._conn.execute(migration)
                self._conn.commit()
            except sqlite3.OperationalError:
                pass

        # Migration 4: Add compaction fields to messages
        try:
            self._conn.execute("ALTER TABLE messages ADD COLUMN compacted INTEGER DEFAULT 0")
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

        try:
            self._conn.execute("ALTER TABLE messages ADD COLUMN is_compaction_summary INTEGER DEFAULT 0")
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

        # Migration 5: Phase 2 channel/session dimensions.
        channel_migrations = [
            "ALTER TABLE sessions ADD COLUMN channel_name TEXT DEFAULT 'feishu'",
            "ALTER TABLE sessions ADD COLUMN thread_id TEXT DEFAULT NULL",
            "ALTER TABLE processed_events ADD COLUMN channel_name TEXT DEFAULT 'feishu'",
        ]
        for migration in channel_migrations:
            try:
                self._conn.execute(migration)
                self._conn.commit()
            except sqlite3.OperationalError:
                pass

        # Migration 6: Workflow approvals and workflow observability.
        approval_migrations = [
            "ALTER TABLE pending_approvals ADD COLUMN approval_type TEXT DEFAULT 'tool'",
        ]
        for migration in approval_migrations:
            try:
                self._conn.execute(migration)
                self._conn.commit()
            except sqlite3.OperationalError:
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
    channel_name TEXT DEFAULT 'feishu',
    thread_id    TEXT DEFAULT NULL,
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

-- Event deduplication with crash recovery support
CREATE TABLE IF NOT EXISTS processed_events (
    event_id TEXT PRIMARY KEY,
    channel_name TEXT DEFAULT 'feishu',
    chat_id TEXT,
    status TEXT NOT NULL,        -- "processing" / "handled" / "failed"
    run_id TEXT,
    started_at INTEGER NOT NULL,
    heartbeat_at INTEGER NOT NULL,  -- Heartbeat timestamp for long-running tasks
    finished_at INTEGER,
    error TEXT,
    attempt_count INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_processed_events_started_at ON processed_events(started_at);
CREATE INDEX IF NOT EXISTS idx_processed_events_status ON processed_events(status);
CREATE INDEX IF NOT EXISTS idx_processed_events_heartbeat ON processed_events(heartbeat_at);

-- Security audit log
CREATE TABLE IF NOT EXISTS security_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    debug_id TEXT UNIQUE NOT NULL,
    event_type TEXT NOT NULL,
    details TEXT,
    chat_id TEXT,
    agent_id TEXT,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_security_audit_debug_id ON security_audit(debug_id);
CREATE INDEX IF NOT EXISTS idx_security_audit_created_at ON security_audit(created_at);

-- Pending confirmations (for persistent bypass, etc.)
CREATE TABLE IF NOT EXISTS pending_confirmations (
    chat_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    type TEXT NOT NULL,        -- "bypass_persistent" / future types
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, agent_id, type)
);

CREATE INDEX IF NOT EXISTS idx_pending_confirmations_expires_at ON pending_confirmations(expires_at);

-- Task state for context preservation
CREATE TABLE IF NOT EXISTS task_state (
    chat_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    goal TEXT,
    test_command TEXT,
    facts_json TEXT,        -- JSON serialized facts list
    updated_at INTEGER,
    PRIMARY KEY (chat_id, agent_id)
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

CREATE TABLE IF NOT EXISTS agents (
    id           TEXT PRIMARY KEY,
    name         TEXT,
    config_json  TEXT NOT NULL,
    source       TEXT NOT NULL,
    enabled      INTEGER DEFAULT 1,
    created_at   INTEGER,
    updated_at   INTEGER
);

CREATE TABLE IF NOT EXISTS channel_bindings (
    channel_name TEXT NOT NULL,
    chat_id      TEXT NOT NULL,
    agent_id     TEXT NOT NULL,
    created_at   INTEGER,
    PRIMARY KEY (channel_name, chat_id)
);

CREATE TABLE IF NOT EXISTS skill_bindings (
    agent_id     TEXT NOT NULL,
    skill_name   TEXT NOT NULL,
    enabled      INTEGER DEFAULT 1,
    created_at   INTEGER,
    PRIMARY KEY (agent_id, skill_name)
);

CREATE TABLE IF NOT EXISTS plugins (
    name                 TEXT PRIMARY KEY,
    version              TEXT,
    enabled              INTEGER DEFAULT 0,
    manifest_json        TEXT,
    manifest_hash        TEXT,
    declared_permissions TEXT,
    error_msg            TEXT,
    last_loaded_at       INTEGER,
    installed_at         INTEGER,
    enabled_at           INTEGER
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
    approval_type TEXT DEFAULT 'tool',
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

-- Workflow group
CREATE TABLE IF NOT EXISTS workflow_runs (
    workflow_id     TEXT PRIMARY KEY,
    chat_id         TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    status          TEXT NOT NULL,
    spec_json       TEXT NOT NULL,
    approval_id     TEXT,
    approval_reason TEXT,
    approved_at     INTEGER,
    rejected_at     INTEGER,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_workflow_runs_chat_agent ON workflow_runs(chat_id, agent_id);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_status ON workflow_runs(status);

CREATE TABLE IF NOT EXISTS workflow_nodes (
    workflow_id  TEXT NOT NULL,
    node_id      TEXT NOT NULL,
    status       TEXT NOT NULL,
    agent_run_id TEXT,
    result_json  TEXT,
    started_at   INTEGER,
    finished_at  INTEGER,
    error        TEXT,
    PRIMARY KEY (workflow_id, node_id),
    FOREIGN KEY (workflow_id) REFERENCES workflow_runs(workflow_id)
);

CREATE TABLE IF NOT EXISTS workflow_node_prompts (
    workflow_id        TEXT NOT NULL,
    node_id            TEXT NOT NULL,
    system_prompt      TEXT NOT NULL,
    user_prompt        TEXT NOT NULL,
    output_schema_json TEXT,
    compiled_at        INTEGER NOT NULL,
    redacted           INTEGER DEFAULT 0,
    PRIMARY KEY (workflow_id, node_id),
    FOREIGN KEY (workflow_id) REFERENCES workflow_runs(workflow_id)
);
"""
