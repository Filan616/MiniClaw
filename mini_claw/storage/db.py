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

        # Migration 8 (Phase B.4): Stats columns for token aggregation and tool duration.
        stats_migrations = [
            "ALTER TABLE agent_runs ADD COLUMN total_tokens INTEGER DEFAULT 0",
            "ALTER TABLE agent_runs ADD COLUMN total_cost_usd REAL DEFAULT 0.0",
            "ALTER TABLE tool_calls ADD COLUMN duration_ms INTEGER",
        ]
        for migration in stats_migrations:
            try:
                self._conn.execute(migration)
                self._conn.commit()
            except sqlite3.OperationalError:
                pass

        # Migration 9 (Phase B.7): Sessions provider_id binding for fallback consistency.
        try:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN provider_id TEXT")
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

        # Migration 7 (Phase C6): Session composite primary key.
        # Rebuild sessions table with composite PK (channel_name, chat_id, agent_id)
        # to support same chat_id across different channels.
        # Also drop messages FK to sessions(chat_id) since chat_id is no longer PK.
        try:
            # Backfill NULL channel_name first
            self._conn.execute(
                "UPDATE sessions SET channel_name='feishu' WHERE channel_name IS NULL"
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

        # Check if messages table has the FK that references the old sessions(chat_id) PK
        try:
            cursor = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'"
            )
            row = cursor.fetchone()
            if row and "REFERENCES sessions" in (row[0] or ""):
                # Rebuild messages table without the FK
                self._conn.executescript("""
                    ALTER TABLE messages RENAME TO messages_old;

                    CREATE TABLE messages (
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id      TEXT NOT NULL,
                        agent_id     TEXT NOT NULL,
                        run_id       TEXT,
                        role         TEXT NOT NULL,
                        content      TEXT,
                        tool_calls   TEXT,
                        tool_call_id TEXT,
                        created_at   INTEGER,
                        compacted    INTEGER DEFAULT 0,
                        is_compaction_summary INTEGER DEFAULT 0
                    );

                    INSERT INTO messages
                        (id, chat_id, agent_id, run_id, role, content,
                         tool_calls, tool_call_id, created_at, compacted, is_compaction_summary)
                    SELECT id, chat_id, agent_id, run_id, role, content,
                           tool_calls, tool_call_id, created_at,
                           COALESCE(compacted, 0), COALESCE(is_compaction_summary, 0)
                    FROM messages_old;

                    DROP TABLE messages_old;

                    CREATE INDEX IF NOT EXISTS idx_messages_run ON messages(run_id, id);
                """)
                self._conn.commit()
        except sqlite3.OperationalError:
            self._conn.rollback()

        # Check if migration already applied (composite index exists)
        cursor = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_sessions_composite'"
        )
        composite_index_exists = cursor.fetchone() is not None

        if not composite_index_exists:
            # Check if sessions table has chat_id as sole PK (old schema)
            cursor = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='sessions'"
            )
            row = cursor.fetchone()
            if row and "chat_id      TEXT PRIMARY KEY" in row[0]:
                # Rebuild with composite UNIQUE constraint instead of changing PK
                # (changing PK requires full table rebuild; using UNIQUE is safer)
                try:
                    self._conn.executescript("""
                        ALTER TABLE sessions RENAME TO sessions_old;

                        CREATE TABLE sessions (
                            chat_id      TEXT NOT NULL,
                            agent_id     TEXT NOT NULL,
                            created_at   INTEGER,
                            updated_at   INTEGER,
                            channel_name TEXT NOT NULL DEFAULT 'feishu',
                            thread_id    TEXT DEFAULT NULL,
                            sandbox_mode_override TEXT,
                            sandbox_mode_expires_at INTEGER,
                            sandbox_mode_persistent INTEGER DEFAULT 0,
                            sandbox_mode_single_use INTEGER DEFAULT 0,
                            PRIMARY KEY (channel_name, chat_id, agent_id)
                        );

                        INSERT INTO sessions
                            (chat_id, agent_id, created_at, updated_at, channel_name, thread_id,
                             sandbox_mode_override, sandbox_mode_expires_at,
                             sandbox_mode_persistent, sandbox_mode_single_use)
                        SELECT chat_id, agent_id, created_at, updated_at,
                               COALESCE(channel_name, 'feishu'), thread_id,
                               sandbox_mode_override, sandbox_mode_expires_at,
                               COALESCE(sandbox_mode_persistent, 0),
                               COALESCE(sandbox_mode_single_use, 0)
                        FROM sessions_old;

                        DROP TABLE sessions_old;

                        CREATE UNIQUE INDEX idx_sessions_composite
                        ON sessions(channel_name, chat_id, COALESCE(thread_id, ''));
                    """)
                    self._conn.commit()
                except sqlite3.OperationalError:
                    self._conn.rollback()
            else:
                # Fresh table or already migrated, just add the index if missing
                try:
                    self._conn.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_composite "
                        "ON sessions(channel_name, chat_id, COALESCE(thread_id, ''))"
                    )
                    self._conn.commit()
                except sqlite3.OperationalError:
                    pass

        # Phase 8 RAG (M1): FTS5 virtual table for rag_chunks full-text search.
        # FTS5 may be unavailable on some SQLite builds — degrade gracefully.
        try:
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS rag_chunks_fts "
                "USING fts5(chunk_id, item_id, content, section_title, symbol_name, tokenize='unicode61')"
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            # FTS5 not available; M2 indexer will fall back to LIKE search
            pass

        # Phase 9 M9.1: FTS5 virtual table for messages full-text search (chat_search).
        # Same graceful degradation as rag_chunks_fts; retriever falls back to LIKE.
        try:
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts "
                "USING fts5(message_id UNINDEXED, session_id UNINDEXED, agent_id UNINDEXED, "
                "chat_id UNINDEXED, channel_name UNINDEXED, workspace_dir UNINDEXED, "
                "role UNINDEXED, content, created_at UNINDEXED, tokenize='unicode61')"
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            # FTS5 not available; chat_search will fall back to LIKE search
            pass

        # Phase 9 P0.2: Add channel_name to task_state/session_chain_state/pending_approvals
        # for multi-channel isolation (composite PK rebuild)
        p02_migrations = [
            "ALTER TABLE task_state ADD COLUMN channel_name TEXT",
            "ALTER TABLE session_chain_state ADD COLUMN channel_name TEXT",
            "ALTER TABLE pending_approvals ADD COLUMN channel_name TEXT",
        ]
        for migration in p02_migrations:
            try:
                self._conn.execute(migration)
            except sqlite3.OperationalError:
                pass

        # Backfill channel_name='legacy' for existing rows
        try:
            self._conn.execute(
                "UPDATE task_state SET channel_name='legacy' WHERE channel_name IS NULL"
            )
            self._conn.execute(
                "UPDATE session_chain_state SET channel_name='legacy' WHERE channel_name IS NULL"
            )
            self._conn.execute(
                "UPDATE pending_approvals SET channel_name='legacy' WHERE channel_name IS NULL"
            )
        except sqlite3.OperationalError:
            pass

        # Rebuild task_state table with composite PK (channel_name, chat_id, agent_id)
        try:
            cursor = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_state'"
            )
            row = cursor.fetchone()
            if row and "PRIMARY KEY (chat_id, agent_id)" in row["sql"]:
                # Needs rebuild
                self._conn.execute("ALTER TABLE task_state RENAME TO task_state_old")
                self._conn.execute(
                    """
                    CREATE TABLE task_state (
                        channel_name TEXT NOT NULL DEFAULT 'legacy',
                        chat_id TEXT NOT NULL,
                        agent_id TEXT NOT NULL,
                        goal TEXT,
                        test_command TEXT,
                        facts_json TEXT,
                        updated_at INTEGER,
                        PRIMARY KEY (channel_name, chat_id, agent_id)
                    )
                    """
                )
                self._conn.execute(
                    "INSERT INTO task_state SELECT channel_name, chat_id, agent_id, goal, test_command, facts_json, updated_at FROM task_state_old"
                )
                self._conn.execute("DROP TABLE task_state_old")
        except sqlite3.OperationalError:
            pass

        # Rebuild session_chain_state table with composite PK
        try:
            cursor = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='session_chain_state'"
            )
            row = cursor.fetchone()
            if row and "PRIMARY KEY (chat_id, agent_id, script_path)" in row["sql"]:
                self._conn.execute("ALTER TABLE session_chain_state RENAME TO session_chain_state_old")
                self._conn.execute(
                    """
                    CREATE TABLE session_chain_state (
                        channel_name TEXT NOT NULL DEFAULT 'legacy',
                        chat_id TEXT NOT NULL,
                        agent_id TEXT NOT NULL,
                        script_path TEXT NOT NULL,
                        content_hash TEXT NOT NULL DEFAULT '',
                        chmod_applied INTEGER DEFAULT 0,
                        created_at INTEGER NOT NULL DEFAULT 0,
                        expires_at INTEGER NOT NULL DEFAULT 0,
                        status TEXT,
                        observed_at INTEGER,
                        details_json TEXT,
                        rag_indexed_paths TEXT,
                        PRIMARY KEY (channel_name, chat_id, agent_id, script_path)
                    )
                    """
                )
                self._conn.execute(
                    "INSERT INTO session_chain_state "
                    "SELECT channel_name, chat_id, agent_id, script_path, "
                    "COALESCE(content_hash, ''), COALESCE(chmod_applied, 0), "
                    "COALESCE(created_at, 0), COALESCE(expires_at, 0), "
                    "status, observed_at, details_json, rag_indexed_paths "
                    "FROM session_chain_state_old"
                )
                self._conn.execute("DROP TABLE session_chain_state_old")
        except sqlite3.OperationalError:
            pass

        self._conn.commit()

        # Phase 8 RAG (M2.5 prep): extend session_chain_state with RAG tracking columns.
        # Idempotent: catch duplicate-column errors when ALTER runs on already-migrated DB.
        try:
            self._conn.execute(
                "ALTER TABLE session_chain_state ADD COLUMN rag_indexed_paths TEXT"
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

        try:
            self._conn.execute(
                "ALTER TABLE session_chain_state ADD COLUMN rag_search_queries TEXT"
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

        # Phase 9 横切: track search_chat queries (link E exfil base).
        try:
            self._conn.execute(
                "ALTER TABLE session_chain_state ADD COLUMN chat_search_queries TEXT"
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

        # Phase 8.3.5: incremental RAG reindex / active-version mapping.
        rag_item_migrations = [
            "ALTER TABLE rag_items ADD COLUMN chunker_version TEXT",
            "ALTER TABLE rag_items ADD COLUMN anchor_schema_version TEXT",
            "ALTER TABLE rag_items ADD COLUMN embedding_model TEXT",
            "ALTER TABLE rag_items ADD COLUMN last_reindex_diff_id TEXT",
            "ALTER TABLE rag_items ADD COLUMN last_reindex_diff_json TEXT",
        ]
        for migration in rag_item_migrations:
            try:
                self._conn.execute(migration)
                self._conn.commit()
            except sqlite3.OperationalError:
                pass

        rag_chunk_migrations = [
            "ALTER TABLE rag_chunks ADD COLUMN anchor_id TEXT",
            "ALTER TABLE rag_chunks ADD COLUMN chunk_hash TEXT",
            "ALTER TABLE rag_chunks ADD COLUMN chunker_version TEXT",
            "ALTER TABLE rag_chunks ADD COLUMN anchor_schema_version TEXT",
        ]
        for migration in rag_chunk_migrations:
            try:
                self._conn.execute(migration)
                self._conn.commit()
            except sqlite3.OperationalError:
                pass

        try:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS rag_item_chunk_versions (
                    item_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    chunk_id TEXT NOT NULL,
                    chunk_order INTEGER NOT NULL,
                    anchor_id TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    is_reused INTEGER DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (item_id, version, chunk_id)
                );

                CREATE INDEX IF NOT EXISTS idx_rag_item_chunk_versions_active
                ON rag_item_chunk_versions(item_id, version, status, chunk_order);

                CREATE TABLE IF NOT EXISTS rag_reindex_diffs (
                    diff_id TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL,
                    old_version INTEGER NOT NULL,
                    new_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    reason TEXT,
                    added_count INTEGER DEFAULT 0,
                    updated_count INTEGER DEFAULT 0,
                    deleted_count INTEGER DEFAULT 0,
                    reused_count INTEGER DEFAULT 0,
                    uncertain_count INTEGER DEFAULT 0,
                    fallback_reason TEXT,
                    vector_cleanup_status TEXT,
                    started_at INTEGER NOT NULL,
                    finished_at INTEGER,
                    duration_ms INTEGER,
                    metadata_json TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_rag_reindex_diffs_item
                ON rag_reindex_diffs(item_id, started_at);

                CREATE TABLE IF NOT EXISTS rag_reindex_diff_chunks (
                    row_id TEXT PRIMARY KEY,
                    diff_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    old_chunk_id TEXT,
                    new_chunk_id TEXT,
                    chunk_order INTEGER,
                    anchor_id TEXT,
                    change_type TEXT NOT NULL,
                    match_strategy TEXT,
                    match_confidence REAL,
                    rename_detected INTEGER DEFAULT 0,
                    metadata_json TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_rag_reindex_diff_chunks_diff
                ON rag_reindex_diff_chunks(diff_id, change_type);

                CREATE TABLE IF NOT EXISTS rag_locks (
                    item_id TEXT NOT NULL,
                    lock_type TEXT NOT NULL,
                    owner_run_id TEXT NOT NULL,
                    acquired_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    PRIMARY KEY (item_id, lock_type)
                );
                """
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            self._conn.rollback()

        # Phase 9 P0.1: messages table isolation columns (channel_name + workspace_dir + workspace_dir_inferred).
        # Step 1: ALTER TABLE messages ADD COLUMN channel_name TEXT
        try:
            self._conn.execute("ALTER TABLE messages ADD COLUMN channel_name TEXT")
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

        # Step 2: ALTER TABLE messages ADD COLUMN workspace_dir TEXT
        try:
            self._conn.execute("ALTER TABLE messages ADD COLUMN workspace_dir TEXT")
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

        # Step 3: ALTER TABLE messages ADD COLUMN workspace_dir_inferred INTEGER DEFAULT 0
        try:
            self._conn.execute("ALTER TABLE messages ADD COLUMN workspace_dir_inferred INTEGER DEFAULT 0")
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

        # Step 4: 推断回填 channel_name — 优先从 channel_bindings 反查
        # (Best-effort: if channel_bindings empty or chat_id not found, step 5 will backfill 'legacy')
        try:
            # Only backfill where channel_name is NULL
            self._conn.execute(
                """
                UPDATE messages
                SET channel_name = (
                    SELECT channel_name FROM channel_bindings WHERE channel_bindings.chat_id = messages.chat_id LIMIT 1
                )
                WHERE channel_name IS NULL
                  AND EXISTS (SELECT 1 FROM channel_bindings WHERE channel_bindings.chat_id = messages.chat_id)
                """
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

        # Step 5: 剩余无法推断的回填为 'legacy'
        try:
            self._conn.execute(
                "UPDATE messages SET channel_name = 'legacy' WHERE channel_name IS NULL"
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

        # Step 6: workspace_dir best-effort 回填（标记 workspace_dir_inferred=1）
        # Note: WorkspaceManager not available in db.py; router will call a helper post-init if needed.
        # For now, leave workspace_dir NULL for old messages;兼容期查询允许 NULL fallback.
        # 新写入的消息由 store_message 强制写 workspace_dir_inferred=0。

        # Phase 9 P0.3: Migrate active_contexts old session_id=NULL rows.
        # Before Phase 9 P0.1, active_contexts might have rows with session_id=NULL.
        # Strategy: Delete them as stale since we can't reliably reconstruct the original
        # (channel_name, chat_id, thread_id, agent_id) tuple that would be needed to
        # derive the correct session_id. These represent pre-Phase 9 active contexts
        # that are no longer valid under the new stable session_id scheme.
        try:
            cursor = self._conn.execute(
                "SELECT COUNT(*) as count FROM active_contexts WHERE session_id IS NULL"
            )
            row = cursor.fetchone()
            if row and row[0] > 0:
                # Delete stale rows with NULL session_id
                self._conn.execute("DELETE FROM active_contexts WHERE session_id IS NULL")
                self._conn.commit()
        except sqlite3.OperationalError:
            # Table might not exist yet (fresh install), safe to ignore
            pass

    # ------------------------------------------------------------------
    # Phase 9 P0.1: workspace_dir backfill helper
    # ------------------------------------------------------------------

    def backfill_workspace_dir(self, workspace_resolver: Any) -> dict[str, int]:
        """Best-effort backfill of messages.workspace_dir using WorkspaceManager.

        Args:
            workspace_resolver: A callable with signature (agent_id, chat_id) -> str|Path|None
                                that returns the workspace directory for a given agent/chat pair.

        Returns:
            Dictionary with backfill statistics: {"updated": N, "failed": M, "skipped": K}

        This method:
        1. Finds all messages where workspace_dir IS NULL and workspace_dir_inferred=0
        2. Groups by (agent_id, chat_id) to minimize resolver calls
        3. For each group, calls workspace_resolver(agent_id, chat_id)
        4. If successful, updates workspace_dir and sets workspace_dir_inferred=1
        5. Tolerates resolver failures (logs and continues)
        """
        stats = {"updated": 0, "failed": 0, "skipped": 0}

        try:
            # Count rows with existing workspace_dir (already populated — not touched by backfill)
            existing = self.fetchone(
                "SELECT COUNT(*) AS cnt FROM messages WHERE workspace_dir IS NOT NULL"
            )
            stats["skipped"] = int(existing["cnt"]) if existing else 0

            # Find distinct (agent_id, chat_id) pairs needing backfill
            pairs = self.fetchall(
                "SELECT DISTINCT agent_id, chat_id FROM messages "
                "WHERE workspace_dir IS NULL AND workspace_dir_inferred = 0"
            )

            if not pairs:
                return stats

            for pair in pairs:
                agent_id = pair["agent_id"]
                chat_id = pair["chat_id"]

                try:
                    # Call the resolver (WorkspaceManager.get_workspace)
                    workspace_dir = workspace_resolver(chat_id, agent_id)
                    if workspace_dir is None:
                        # Resolver said "no workspace" — leave NULL.
                        # We don't double-count this as skipped because skipped already
                        # represents rows that had workspace_dir at entry.
                        continue

                    # Convert Path to string if needed
                    workspace_dir_str = str(workspace_dir)

                    # Update all messages for this (agent_id, chat_id) pair
                    cursor = self.execute(
                        "UPDATE messages SET workspace_dir = ?, workspace_dir_inferred = 1 "
                        "WHERE agent_id = ? AND chat_id = ? "
                        "AND workspace_dir IS NULL AND workspace_dir_inferred = 0",
                        (workspace_dir_str, agent_id, chat_id),
                    )
                    stats["updated"] += cursor.rowcount

                except Exception:
                    # Resolver failed for this pair — continue with others
                    stats["failed"] += 1
                    continue

        except Exception:
            # Catastrophic failure — return partial stats
            pass

        return stats

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
    chat_id      TEXT NOT NULL,
    agent_id     TEXT NOT NULL,
    created_at   INTEGER,
    updated_at   INTEGER,
    channel_name TEXT NOT NULL DEFAULT 'feishu',
    thread_id    TEXT DEFAULT NULL,
    sandbox_mode_override TEXT,  -- "safe", "bypass", or NULL (use config default)
    provider_id  TEXT,  -- Phase B.7: bound provider for session-consistent fallback
    PRIMARY KEY (channel_name, chat_id, agent_id)
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
    created_at   INTEGER
    -- Phase C6: removed FK to sessions(chat_id) since sessions now has composite PK
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

-- Phase A.3: Cross-message chain attack detection state.
CREATE TABLE IF NOT EXISTS session_chain_state (
    chat_id      TEXT NOT NULL,
    agent_id     TEXT NOT NULL,
    script_path  TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    chmod_applied INTEGER DEFAULT 0,
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    PRIMARY KEY (chat_id, agent_id, script_path)
);

CREATE INDEX IF NOT EXISTS idx_session_chain_expires
ON session_chain_state(expires_at);

-- Phase B.7: Provider health tracking for fallback.
CREATE TABLE IF NOT EXISTS provider_health (
    provider_id           TEXT PRIMARY KEY,
    last_check_at         INTEGER,
    last_ok_at            INTEGER,
    last_error            TEXT,
    consecutive_failures  INTEGER DEFAULT 0,
    healthy               INTEGER DEFAULT 1
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
    total_tokens        INTEGER DEFAULT 0,
    total_cost_usd      REAL DEFAULT 0.0,
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
    duration_ms  INTEGER,
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

-- Phase 8 RAG group (M1)
CREATE TABLE IF NOT EXISTS rag_items (
    item_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    source_type TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    owner_agent_id TEXT NOT NULL,
    session_id TEXT,
    chat_id TEXT,
    channel_name TEXT,
    workspace_dir TEXT,
    source_path TEXT,
    title TEXT,
    content_hash TEXT,
    status TEXT NOT NULL,
    importance INTEGER DEFAULT 3,
    pinned INTEGER DEFAULT 0,
    confidence REAL DEFAULT 1.0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    last_accessed_at INTEGER,
    access_count INTEGER DEFAULT 0,
    expires_at INTEGER,
    indexed_by_agent_id TEXT,
    indexed_by_chat_id TEXT,
    indexed_by_channel TEXT,
    source_chain_json TEXT,
    metadata_json TEXT,
    active_version INTEGER DEFAULT 1,
    sensitivity_level TEXT DEFAULT 'low',
    chunker_version TEXT,
    anchor_schema_version TEXT,
    embedding_model TEXT,
    last_reindex_diff_id TEXT,
    last_reindex_diff_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_rag_items_owner
ON rag_items(owner_agent_id, namespace, status);

CREATE INDEX IF NOT EXISTS idx_rag_items_scope
ON rag_items(scope_type, scope_id, namespace, status);

CREATE INDEX IF NOT EXISTS idx_rag_items_source
ON rag_items(source_path, content_hash);

CREATE INDEX IF NOT EXISTS idx_rag_items_workspace
ON rag_items(workspace_dir, namespace, status);

CREATE TABLE IF NOT EXISTS rag_chunks (
    chunk_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    token_count INTEGER,
    start_line INTEGER,
    end_line INTEGER,
    section_title TEXT,
    symbol_name TEXT,
    language TEXT,
    content_hash TEXT,
    metadata_json TEXT,
    version INTEGER DEFAULT 1,
    anchor_id TEXT,
    chunk_hash TEXT,
    chunker_version TEXT,
    anchor_schema_version TEXT,
    FOREIGN KEY(item_id) REFERENCES rag_items(item_id)
);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_item
ON rag_chunks(item_id, version, chunk_index);

CREATE TABLE IF NOT EXISTS rag_item_chunk_versions (
    item_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    chunk_id TEXT NOT NULL,
    chunk_order INTEGER NOT NULL,
    anchor_id TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    is_reused INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (item_id, version, chunk_id)
);

CREATE INDEX IF NOT EXISTS idx_rag_item_chunk_versions_active
ON rag_item_chunk_versions(item_id, version, status, chunk_order);

CREATE TABLE IF NOT EXISTS rag_reindex_diffs (
    diff_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    old_version INTEGER NOT NULL,
    new_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    reason TEXT,
    added_count INTEGER DEFAULT 0,
    updated_count INTEGER DEFAULT 0,
    deleted_count INTEGER DEFAULT 0,
    reused_count INTEGER DEFAULT 0,
    uncertain_count INTEGER DEFAULT 0,
    fallback_reason TEXT,
    vector_cleanup_status TEXT,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    duration_ms INTEGER,
    metadata_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_rag_reindex_diffs_item
ON rag_reindex_diffs(item_id, started_at);

CREATE TABLE IF NOT EXISTS rag_reindex_diff_chunks (
    row_id TEXT PRIMARY KEY,
    diff_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    old_chunk_id TEXT,
    new_chunk_id TEXT,
    chunk_order INTEGER,
    anchor_id TEXT,
    change_type TEXT NOT NULL,
    match_strategy TEXT,
    match_confidence REAL,
    rename_detected INTEGER DEFAULT 0,
    metadata_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_rag_reindex_diff_chunks_diff
ON rag_reindex_diff_chunks(diff_id, change_type);

CREATE TABLE IF NOT EXISTS rag_locks (
    item_id TEXT NOT NULL,
    lock_type TEXT NOT NULL,
    owner_run_id TEXT NOT NULL,
    acquired_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY (item_id, lock_type)
);

CREATE TABLE IF NOT EXISTS rag_embeddings (
    chunk_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    backend TEXT NOT NULL,
    collection_name TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    dim INTEGER,
    vector_id TEXT,
    created_at INTEGER NOT NULL,
    metadata_json TEXT
);

-- Phase 9 M9.6: Memory maintenance suggestions
CREATE TABLE IF NOT EXISTS memory_maintenance_suggestions (
    suggestion_id TEXT PRIMARY KEY,
    suggestion_type TEXT NOT NULL,  -- dedupe/conflict/stale
    item_id_a TEXT NOT NULL,
    item_id_b TEXT,  -- NULL for stale (single-item suggestion)
    reason TEXT,
    confidence REAL,
    status TEXT DEFAULT 'pending',  -- pending/applied/rejected
    created_at INTEGER NOT NULL,
    resolved_at INTEGER,
    resolved_by TEXT
);

CREATE TABLE IF NOT EXISTS memory_usage_events (
    event_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    accessed_at INTEGER NOT NULL,
    context_json TEXT  -- {chat_id, agent_id, retrieval_type, etc.}
);

CREATE TABLE IF NOT EXISTS active_contexts (
    session_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    context_id TEXT NOT NULL,
    context_type TEXT NOT NULL,
    title TEXT,
    activated_at INTEGER NOT NULL,
    expires_at INTEGER,
    PRIMARY KEY(session_id, agent_id, context_id)
);

CREATE INDEX IF NOT EXISTS idx_active_contexts_session
ON active_contexts(session_id, agent_id);

CREATE TABLE IF NOT EXISTS memory_candidates (
    candidate_id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_chain_json TEXT NOT NULL,
    source_message_ids TEXT,
    source_session_id TEXT,
    source_workflow_id TEXT,
    created_by_agent_id TEXT NOT NULL,
    created_from_chat_id TEXT NOT NULL,
    created_from_channel TEXT,
    stability INTEGER,
    reuse_value INTEGER,
    sensitivity INTEGER,
    confidence REAL,
    status TEXT NOT NULL,
    approval_id TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    metadata_json TEXT
);
"""
