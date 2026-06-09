"""Session management for the gateway layer."""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any

from mini_claw.storage.db import Database


# Default number of recent messages to leave uncompacted on each compaction pass.
DEFAULT_KEEP_RECENT = 20

# Cap on how many active (non-compacted) compaction summaries we let pile up
# before merging the oldest ones into a single rolled-up summary.
_MAX_ACTIVE_SUMMARIES = 3

# Pattern for harvesting error lines from compacted message bodies.
_RE_ERROR_LINE = re.compile(r"\[ERROR\][^\n]*")


def derive_session_id(
    channel_name: str,
    chat_id: str,
    agent_id: str,
    thread_id: str | None = None,
) -> str:
    """Derive a stable session_id from the (channel, chat, thread, agent) tuple.

    Phase 9 P0.1: ``session_id`` MUST be stable across runs and compactions, so
    do NOT pass ``run_id`` as a substitute. Same logical conversation yields the
    same id forever, which is what /chat search --session current and active
    contexts depend on.
    """
    raw = f"{channel_name}|{chat_id}|{thread_id or ''}|{agent_id}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class SessionManager:
    """Manages chat sessions and message history."""

    def __init__(self, storage: Database) -> None:
        self._storage = storage

    def get_or_create(
        self, chat_id: str, agent_id: str, channel_name: str = "feishu"
    ) -> dict[str, Any]:
        """Get an existing session or create a new one.

        Sessions are scoped by (channel_name, chat_id, agent_id) to support
        the same chat_id appearing on different channels independently.

        Returns a dict with session metadata.
        """
        row = self._storage.fetchone(
            "SELECT * FROM sessions WHERE channel_name = ? AND chat_id = ? AND agent_id = ?",
            (channel_name, chat_id, agent_id),
        )
        if row is not None:
            # Update last activity
            now = int(time.time())
            self._storage.execute(
                "UPDATE sessions SET updated_at = ? "
                "WHERE channel_name = ? AND chat_id = ? AND agent_id = ?",
                (now, channel_name, chat_id, agent_id),
            )
            row["updated_at"] = now
            return row

        now = int(time.time())
        self._storage.execute(
            "INSERT INTO sessions (chat_id, agent_id, channel_name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, agent_id, channel_name, now, now),
        )
        return {
            "chat_id": chat_id,
            "agent_id": agent_id,
            "channel_name": channel_name,
            "thread_id": None,
            "created_at": now,
            "updated_at": now,
            "sandbox_mode_override": None,
        }

    def set_sandbox_mode(
        self, chat_id: str, agent_id: str, mode: str, channel_name: str = "feishu"
    ) -> None:
        """Set sandbox_mode_override for a session ("safe", "bypass", or None).

        Also clears any previously-set TTL state (``sandbox_mode_expires_at``,
        ``sandbox_mode_single_use``, ``sandbox_mode_persistent``) so a fresh
        override never inherits stale TTL flags.
        """
        self._storage.execute(
            "UPDATE sessions SET sandbox_mode_override = ?, "
            "sandbox_mode_expires_at = NULL, "
            "sandbox_mode_single_use = 0, "
            "sandbox_mode_persistent = 0, "
            "updated_at = ? "
            "WHERE channel_name = ? AND chat_id = ? AND agent_id = ?",
            (mode, int(time.time()), channel_name, chat_id, agent_id),
        )

    def get_sandbox_mode(
        self, chat_id: str, agent_id: str, channel_name: str = "feishu"
    ) -> str | None:
        """Get sandbox_mode_override for a session, or None if not set."""
        row = self._storage.fetchone(
            "SELECT sandbox_mode_override FROM sessions "
            "WHERE channel_name = ? AND chat_id = ? AND agent_id = ?",
            (channel_name, chat_id, agent_id),
        )
        return row["sandbox_mode_override"] if row else None

    def set_bypass_mode(
        self,
        chat_id: str,
        agent_id: str,
        mode: str,
        expires_at: int | None,
        channel_name: str = "feishu",
    ) -> None:
        """Set sandbox mode together with its expiry timestamp.

        Args:
            chat_id: Session chat identifier.
            agent_id: Session agent identifier.
            mode: "safe" or "bypass".
            expires_at: Unix timestamp when bypass expires; ``None`` for
                persistent (no expiry); ``0`` is the sentinel for single-use
                ("next message only").
        """
        self._storage.execute(
            "UPDATE sessions SET sandbox_mode_override = ?, "
            "sandbox_mode_expires_at = ?, updated_at = ? "
            "WHERE channel_name = ? AND chat_id = ? AND agent_id = ?",
            (mode, expires_at, int(time.time()), channel_name, chat_id, agent_id),
        )

    def clear_single_use_bypass(
        self, chat_id: str, agent_id: str, channel_name: str = "feishu"
    ) -> None:
        """Clear single-use bypass after consumption."""
        row = self._storage.fetchone(
            "SELECT sandbox_mode_single_use, sandbox_mode_expires_at "
            "FROM sessions WHERE channel_name = ? AND chat_id = ? AND agent_id = ?",
            (channel_name, chat_id, agent_id),
        )
        if row and (
            row.get("sandbox_mode_single_use")
            or row.get("sandbox_mode_expires_at") == 0
        ):
            self._storage.execute(
                "UPDATE sessions SET sandbox_mode_override = NULL, "
                "sandbox_mode_expires_at = NULL, sandbox_mode_single_use = 0, "
                "updated_at = ? "
                "WHERE channel_name = ? AND chat_id = ? AND agent_id = ?",
                (int(time.time()), channel_name, chat_id, agent_id),
            )

    def get_effective_sandbox_mode(
        self, chat_id: str, agent_id: str, channel_name: str = "feishu"
    ) -> str:
        """Return the effective sandbox mode, applying TTL semantics."""
        row = self._storage.fetchone(
            "SELECT sandbox_mode_override, sandbox_mode_expires_at "
            "FROM sessions WHERE channel_name = ? AND chat_id = ? AND agent_id = ?",
            (channel_name, chat_id, agent_id),
        )
        if row is None:
            return "safe"

        mode = row["sandbox_mode_override"]
        expires_at = row["sandbox_mode_expires_at"]

        if not mode:
            return "safe"

        # Single-use sentinel: leave untouched, return current mode.
        if expires_at == 0:
            return mode if mode in ("safe", "bypass") else "safe"

        # Persistent override (no TTL).
        if expires_at is None:
            return mode if mode in ("safe", "bypass") else "safe"

        now = int(time.time())
        if expires_at > now:
            return "bypass"

        # Expired: roll back to safe and persist the reset.
        self._storage.execute(
            "UPDATE sessions SET sandbox_mode_override = 'safe', "
            "sandbox_mode_expires_at = NULL, updated_at = ? "
            "WHERE channel_name = ? AND chat_id = ? AND agent_id = ?",
            (now, channel_name, chat_id, agent_id),
        )
        return "safe"

    def get_history(
        self,
        chat_id: str,
        agent_id: str,
        channel_name: str = "feishu",
        include_preludes: bool = False,
        include_react_updates: bool = False,
    ) -> list[dict[str, Any]]:
        """Get recent messages for context window construction.

        Returns a list of message dicts with role and content keys,
        ordered chronologically with compaction summaries first.

        Logic:
        1. Get all uncompacted messages (compacted=0) ordered by id
        2. Separate into compaction_summaries and normal_messages
        3. Assemble: [All summaries oldest first] + [All normal messages chronological]

        This ensures the LLM sees: [Summary] -> [Recent messages]

        Phase 9.7: By default, filters out preludes (message_kind='prelude').
        Set include_preludes=True to include them.
        Phase 10 M10.1: ``react_update`` rows are also filtered by default —
        they are user-visible process messages, not part of the LLM context.
        """
        # Get all uncompacted messages ordered by id
        # Phase 9 P0.6: Strict channel_name matching enforced (legacy compatibility removed)
        # Phase 9.7 + Phase 10: Filter out preludes and react_updates by default
        excluded_kinds: list[str] = []
        if not include_preludes:
            excluded_kinds.append("prelude")
        if not include_react_updates:
            excluded_kinds.append("react_update")
        if excluded_kinds:
            placeholders = ",".join("?" for _ in excluded_kinds)
            kind_filter = (
                f"AND (message_kind IS NULL OR message_kind NOT IN ({placeholders})) "
            )
            kind_params: tuple = tuple(excluded_kinds)
        else:
            kind_filter = ""
            kind_params = ()
        rows = self._storage.fetchall(
            "SELECT role, content, tool_calls, tool_call_id, is_compaction_summary "
            "FROM messages "
            "WHERE chat_id = ? AND agent_id = ? "
            "AND channel_name = ? "
            f"{kind_filter}"
            "AND COALESCE(compacted, 0) = 0 "
            "ORDER BY id ASC",
            (chat_id, agent_id, channel_name, *kind_params),
        )

        # Separate into compaction summaries and normal messages
        compaction_summaries = []
        normal_messages = []

        for row in rows:
            if row.get("is_compaction_summary") == 1:
                compaction_summaries.append(row)
            else:
                normal_messages.append(row)

        # Manually assemble order: summaries first (oldest first), then normal messages
        ordered_rows = compaction_summaries + normal_messages

        return self._rows_to_messages(ordered_rows)

    def _rows_to_messages(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert database rows to message dicts."""
        messages: list[dict[str, Any]] = []
        for row in rows:
            msg: dict[str, Any] = {"role": row["role"]}
            if row["content"]:
                msg["content"] = row["content"]
            if row["tool_calls"]:
                msg["tool_calls"] = row["tool_calls"]
            if row["tool_call_id"]:
                msg["tool_call_id"] = row["tool_call_id"]
            messages.append(msg)
        return messages

    def store_message(
        self,
        chat_id: str,
        agent_id: str,
        role: str,
        content: str | None,
        run_id: str | None = None,
        channel_name: str = "feishu",
        workspace_dir: str | None = None,
        message_kind: str = "normal",
        metadata: dict[str, Any] | None = None,
    ) -> int | None:
        """Persist a message to the history store.

        Phase 9 P0.6: ``channel_name`` and ``workspace_dir`` must be provided
        for all new messages. Strict channel_name matching is now enforced in
        get_history / count_messages (legacy compatibility removed).

        Phase 9 M9.1: Also mirrors to messages_fts for chat_search.
        Phase 9.7: ``message_kind`` in ('normal', 'prelude') marks preludes for UI filtering.
        Phase 10 M10.1: ``message_kind='react_update'`` is the new process-message
        kind that replaces prelude in the new flow; ``metadata`` carries the
        ReActUserUpdate ID + step ID + event type for the trace layer.

        Returns the inserted ``messages.id`` for callers that need to wire the
        new row into other tables (e.g. tool_calls, react_user_updates).
        """
        workspace_dir_str = str(workspace_dir) if workspace_dir is not None else None

        now = int(time.time())
        import json as _json

        metadata_json = _json.dumps(metadata, ensure_ascii=False) if metadata else None
        cursor = self._storage.execute(
            "INSERT INTO messages (chat_id, agent_id, run_id, role, content, created_at, "
            "channel_name, workspace_dir, workspace_dir_inferred, message_kind, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chat_id,
                agent_id,
                run_id,
                role,
                content,
                now,
                channel_name,
                workspace_dir_str,
                0,
                message_kind,
                metadata_json,
            ),
        )
        message_id = cursor.lastrowid

        # Phase 9 M9.1: mirror to messages_fts (silent failure if FTS5 unavailable).
        # Phase 10: do NOT mirror process-only kinds (prelude / react_update) —
        # they must not surface in /chat search results.
        if message_id and content and message_kind not in {"prelude", "react_update"}:
            from mini_claw.chat_search.indexer import index_message_row
            try:
                session_id = derive_session_id(channel_name, chat_id, agent_id)
                index_message_row(
                    self._storage,
                    message_id,
                    session_id=session_id,
                    agent_id=agent_id,
                    chat_id=chat_id,
                    channel_name=channel_name,
                    workspace_dir=workspace_dir_str,
                    role=role,
                    content=content,
                    created_at=now,
                )
            except Exception:
                # Mirror failures must not block message persistence
                pass
        return message_id

    def count_messages(
        self, chat_id: str, agent_id: str, channel_name: str = "feishu"
    ) -> int:
        """Return the number of non-compacted messages for ``(chat_id, agent_id)``.

        Compaction summaries are still counted because they remain
        ``compacted=0`` in the schema; callers comparing against a threshold
        should be aware that summaries contribute to the total.

        Phase 9 P0.6: Strict channel_name matching enforced (legacy compatibility removed).
        Phase 10 M10.1: process-only message_kind values (prelude / react_update)
        do not contribute toward the auto-compaction threshold.
        """
        row = self._storage.fetchone(
            "SELECT COUNT(*) AS cnt FROM messages "
            "WHERE chat_id = ? AND agent_id = ? "
            "AND channel_name = ? "
            "AND COALESCE(message_kind, 'normal') NOT IN ('prelude', 'react_update') "
            "AND COALESCE(compacted, 0) = 0",
            (chat_id, agent_id, channel_name),
        )
        if not row:
            return 0
        return int(row["cnt"] or 0)

    def clear_history(
        self, chat_id: str, agent_id: str, channel_name: str = "feishu"
    ) -> int:
        """Hide the active conversation history for one chat/agent/channel.

        This is a logical clear, not a physical delete: old rows stay in the
        database for audit/search, but future ``get_history`` calls will not
        replay them into the model context.
        """
        row = self._storage.fetchone(
            "SELECT COUNT(*) AS cnt FROM messages "
            "WHERE chat_id = ? AND agent_id = ? "
            "AND channel_name = ? "
            "AND COALESCE(compacted, 0) = 0",
            (chat_id, agent_id, channel_name),
        )
        count = int(row["cnt"] or 0) if row else 0
        if count <= 0:
            return 0
        self._storage.execute(
            "UPDATE messages SET compacted = 1 "
            "WHERE chat_id = ? AND agent_id = ? "
            "AND channel_name = ? "
            "AND COALESCE(compacted, 0) = 0",
            (chat_id, agent_id, channel_name),
        )
        return count

    # ------------------------------------------------------------------
    # History compaction
    # ------------------------------------------------------------------

    def compact_history(
        self,
        chat_id: str,
        agent_id: str,
        keep_recent: int = DEFAULT_KEEP_RECENT,
        channel_name: str = "feishu",
        workspace_dir: str | None = None,
    ) -> int:
        """Compact older messages into a system summary backed by TaskState.

        Marks every non-compacted message older than the most-recent
        ``keep_recent`` window as ``compacted=1``, harvests key facts and
        recent errors out of those messages, refreshes the persisted
        :class:`TaskState`, and writes a new system message tagged with
        ``is_compaction_summary=1`` so future :meth:`get_history` calls can
        replay the summary in place of the truncated tail.

        Args:
            chat_id: Session chat identifier.
            agent_id: Session agent identifier.
            keep_recent: Number of most recent non-compacted messages to keep
                untouched. Existing summary rows always count toward this
                window so we never re-compact our own summaries.

        Returns:
            Number of messages newly marked ``compacted=1`` (zero when there
            was nothing eligible to compact).
        """
        # Lazy imports keep the gateway module free of agent-layer cycles.
        from mini_claw.agent.extractor import extract_facts_from_messages
        from mini_claw.agent.task_state import TaskState

        if keep_recent < 0:
            keep_recent = 0

        # Identify candidates: non-compacted, ordered newest-first so we can
        # carve off the recent window cleanly.
        # Phase 10 M10.1: process-only kinds never enter compaction —
        # they are user-visible mirrors, not facts.
        active_rows = self._storage.fetchall(
            "SELECT id, role, content, tool_calls, tool_call_id, run_id, "
            "is_compaction_summary "
            "FROM messages "
            "WHERE chat_id = ? AND agent_id = ? "
            "AND COALESCE(message_kind, 'normal') NOT IN ('prelude', 'react_update') "
            "AND COALESCE(compacted, 0) = 0 "
            "ORDER BY created_at DESC, id DESC",
            (chat_id, agent_id),
        )

        if len(active_rows) <= keep_recent:
            # Nothing falls outside the keep window — still merge old summaries
            # if too many have piled up, then bail.
            self._merge_old_summaries_if_needed(chat_id, agent_id)
            return 0

        # Split: rows[0:keep_recent] stay live; the tail gets compacted.
        to_compact_rows = active_rows[keep_recent:]
        to_compact_ids = [int(r["id"]) for r in to_compact_rows]
        if not to_compact_ids:
            self._merge_old_summaries_if_needed(chat_id, agent_id)
            return 0

        # Walk in chronological order for fact extraction so older context
        # reads first inside the resulting summary.
        chrono_rows = list(reversed(to_compact_rows))
        compacted_messages = self._rows_to_messages(chrono_rows)

        # ------------------------------------------------------------------
        # 1) Refresh TaskState with new facts + errors.
        # ------------------------------------------------------------------
        # Phase 9 P0.2: pass channel_name to TaskState
        state = TaskState.load(self._storage, chat_id, agent_id, channel_name)

        new_facts = extract_facts_from_messages(compacted_messages)
        for fact in new_facts:
            state.add_fact(fact)

        recent_errors = self._extract_recent_errors(
            chat_id, agent_id, to_compact_rows
        )
        for err in recent_errors:
            state.add_error(err.get("error_msg", ""), err.get("run_id", ""))

        state.compaction_count += 1

        # ------------------------------------------------------------------
        # 2) Mark the source rows compacted in a single statement.
        # ------------------------------------------------------------------
        placeholders = ",".join("?" for _ in to_compact_ids)
        self._storage.execute(
            f"UPDATE messages SET compacted = 1 WHERE id IN ({placeholders})",
            tuple(to_compact_ids),
        )

        # ------------------------------------------------------------------
        # 3) Build and insert the summary system message.
        # ------------------------------------------------------------------
        summary_text = self._build_summary_text(state, recent_errors)
        now = int(time.time())
        cursor = self._storage.execute(
            "INSERT INTO messages "
            "(chat_id, agent_id, run_id, role, content, created_at, "
            "compacted, is_compaction_summary, channel_name, workspace_dir, workspace_dir_inferred) "
            "VALUES (?, ?, ?, 'system', ?, ?, 0, 1, ?, ?, ?)",
            (chat_id, agent_id, None, summary_text, now, channel_name, workspace_dir, 0),
        )
        message_id = cursor.lastrowid

        # Phase 9 M9.1: mirror summary to messages_fts
        if message_id and summary_text:
            from mini_claw.chat_search.indexer import index_message_row
            try:
                session_id = derive_session_id(channel_name, chat_id, agent_id)
                index_message_row(
                    self._storage,
                    message_id,
                    session_id=session_id,
                    agent_id=agent_id,
                    chat_id=chat_id,
                    channel_name=channel_name,
                    workspace_dir=workspace_dir,
                    role="system",
                    content=summary_text,
                    created_at=now,
                )
            except Exception:
                pass

        # ------------------------------------------------------------------
        # 4) Persist TaskState and optionally collapse old summaries.
        # ------------------------------------------------------------------
        # Phase 9 P0.2: pass channel_name to TaskState.save
        state.save(self._storage, chat_id, agent_id, channel_name)
        self._merge_old_summaries_if_needed(chat_id, agent_id, channel_name, workspace_dir)

        return len(to_compact_ids)

    # ------------------------------------------------------------------
    # Internal helpers for compaction
    # ------------------------------------------------------------------

    def _extract_recent_errors(
        self,
        chat_id: str,
        agent_id: str,
        compacted_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Pull recent ``[ERROR]`` lines out of the compacted slice.

        Scans the ``content`` field of the compacted messages directly.
        Returns a list of ``{"error_msg", "run_id"}`` dicts, newest first,
        capped to a reasonable size.
        """
        results: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        # Scan the compacted message bodies first.
        for row in reversed(compacted_rows):  # newest first
            content = row.get("content") or ""
            run_id = row.get("run_id") or ""
            if not isinstance(content, str) or "[ERROR]" not in content:
                continue
            for match in _RE_ERROR_LINE.finditer(content):
                msg = match.group(0).strip()
                key = (msg, run_id)
                if msg and key not in seen:
                    seen.add(key)
                    results.append({"error_msg": msg, "run_id": run_id})

        # Bound the list — TaskState already caps its own log, but we want
        # the summary text to stay short too.
        return results[:10]

    def _build_summary_text(
        self,
        state: "Any",  # TaskState, kept loose to avoid forward-decl noise.
        recent_errors: list[dict[str, Any]],
    ) -> str:
        """Render the summary system message body."""
        lines: list[str] = ["[Previous session summary]"]

        task_desc = (state.task_description or "").strip()
        lines.append(f"Task: {task_desc or '(unspecified)'}")

        if state.key_facts:
            lines.append("Key facts:")
            for fact in state.key_facts:
                lines.append(f"- {fact}")
        else:
            lines.append("Key facts: (none captured)")

        if recent_errors:
            err_previews: list[str] = []
            for err in recent_errors[:5]:
                msg = err.get("error_msg", "").strip()
                if msg:
                    err_previews.append(msg)
            if err_previews:
                lines.append("Recent errors:")
                for err in err_previews:
                    lines.append(f"- {err}")
            else:
                lines.append("Recent errors: none")
        else:
            lines.append("Recent errors: none")

        return "\n".join(lines)

    def _merge_old_summaries_if_needed(
        self,
        chat_id: str,
        agent_id: str,
        channel_name: str = "feishu",
        workspace_dir: str | None = None,
    ) -> None:
        """Roll up older summaries when more than ``_MAX_ACTIVE_SUMMARIES`` exist.

        We keep the most recent summary intact (callers rely on it for the
        live context) and fold every earlier active summary into a single
        merged ``system`` message. The originals are flipped to
        ``compacted=1`` so :meth:`get_history` continues to filter them out.
        """
        summaries = self._storage.fetchall(
            "SELECT id, content, created_at FROM messages "
            "WHERE chat_id = ? AND agent_id = ? "
            "AND COALESCE(is_compaction_summary, 0) = 1 "
            "AND COALESCE(compacted, 0) = 0 "
            "ORDER BY created_at ASC, id ASC",
            (chat_id, agent_id),
        )
        if len(summaries) <= _MAX_ACTIVE_SUMMARIES:
            return

        # All summaries except the newest become merge candidates.
        to_merge = summaries[:-1]
        merge_ids = [int(s["id"]) for s in to_merge]
        if not merge_ids:
            return

        merged_body_parts = ["[Merged earlier summaries]"]
        for s in to_merge:
            body = (s.get("content") or "").strip()
            if body:
                merged_body_parts.append(body)
        merged_text = "\n\n".join(merged_body_parts)

        placeholders = ",".join("?" for _ in merge_ids)
        self._storage.execute(
            f"UPDATE messages SET compacted = 1 WHERE id IN ({placeholders})",
            tuple(merge_ids),
        )
        now = int(time.time())
        cursor = self._storage.execute(
            "INSERT INTO messages "
            "(chat_id, agent_id, run_id, role, content, created_at, "
            "compacted, is_compaction_summary, channel_name, workspace_dir, workspace_dir_inferred) "
            "VALUES (?, ?, ?, 'system', ?, ?, 0, 1, ?, ?, ?)",
            (chat_id, agent_id, None, merged_text, now, channel_name, workspace_dir, 0),
        )
        message_id = cursor.lastrowid

        # Phase 9 M9.1: mirror merged summary to messages_fts
        if message_id and merged_text:
            from mini_claw.chat_search.indexer import index_message_row
            try:
                session_id = derive_session_id(channel_name, chat_id, agent_id)
                index_message_row(
                    self._storage,
                    message_id,
                    session_id=session_id,
                    agent_id=agent_id,
                    chat_id=chat_id,
                    channel_name=channel_name,
                    workspace_dir=workspace_dir,
                    role="system",
                    content=merged_text,
                    created_at=now,
                )
            except Exception:
                pass
