"""Session management for the gateway layer."""

from __future__ import annotations

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


class SessionManager:
    """Manages chat sessions and message history."""

    def __init__(self, storage: Database) -> None:
        self._storage = storage

    def get_or_create(self, chat_id: str, agent_id: str) -> dict[str, Any]:
        """Get an existing session or create a new one.

        Returns a dict with session metadata.
        """
        row = self._storage.fetchone(
            "SELECT * FROM sessions WHERE chat_id = ? AND agent_id = ?",
            (chat_id, agent_id),
        )
        if row is not None:
            # Update last activity
            now = int(time.time())
            self._storage.execute(
                "UPDATE sessions SET updated_at = ? WHERE chat_id = ? AND agent_id = ?",
                (now, chat_id, agent_id),
            )
            row["updated_at"] = now
            return row

        now = int(time.time())
        self._storage.execute(
            "INSERT INTO sessions (chat_id, agent_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, agent_id, now, now),
        )
        return {
            "chat_id": chat_id,
            "agent_id": agent_id,
            "created_at": now,
            "updated_at": now,
            "sandbox_mode_override": None,
        }

    def set_sandbox_mode(self, chat_id: str, agent_id: str, mode: str) -> None:
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
            "WHERE chat_id = ? AND agent_id = ?",
            (mode, int(time.time()), chat_id, agent_id),
        )

    def get_sandbox_mode(self, chat_id: str, agent_id: str) -> str | None:
        """Get sandbox_mode_override for a session, or None if not set."""
        row = self._storage.fetchone(
            "SELECT sandbox_mode_override FROM sessions WHERE chat_id = ? AND agent_id = ?",
            (chat_id, agent_id),
        )
        return row["sandbox_mode_override"] if row else None

    def set_bypass_mode(
        self,
        chat_id: str,
        agent_id: str,
        mode: str,
        expires_at: int | None,
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
            "WHERE chat_id = ? AND agent_id = ?",
            (mode, expires_at, int(time.time()), chat_id, agent_id),
        )

    def clear_single_use_bypass(self, chat_id: str, agent_id: str) -> None:
        """Clear single-use bypass after consumption.

        Reads the current sandbox-mode override row and, if it represents a
        single-use grant (``sandbox_mode_single_use=1`` or the legacy
        ``sandbox_mode_expires_at=0`` sentinel), wipes the override, the
        expiry, and the single-use flag in one update. Any other state
        (persistent overrides, future-dated TTL, no override at all) is left
        untouched.
        """
        row = self._storage.fetchone(
            "SELECT sandbox_mode_single_use, sandbox_mode_expires_at "
            "FROM sessions WHERE chat_id = ? AND agent_id = ?",
            (chat_id, agent_id),
        )
        if row and (
            row.get("sandbox_mode_single_use")
            or row.get("sandbox_mode_expires_at") == 0
        ):
            self._storage.execute(
                "UPDATE sessions SET sandbox_mode_override = NULL, "
                "sandbox_mode_expires_at = NULL, sandbox_mode_single_use = 0, "
                "updated_at = ? "
                "WHERE chat_id = ? AND agent_id = ?",
                (int(time.time()), chat_id, agent_id),
            )

    def get_effective_sandbox_mode(self, chat_id: str, agent_id: str) -> str:
        """Return the effective sandbox mode, applying TTL semantics.

        Rules:
            * If no override is set, returns "safe" (default).
            * If ``expires_at == 0`` (single-use sentinel), returns the
              stored mode unchanged. Caller is responsible for clearing
              after consumption.
            * If ``expires_at`` is NULL, returns the stored mode unchanged
              (persistent override).
            * If ``expires_at`` is in the future, returns "bypass".
            * If ``expires_at`` has elapsed, resets the row to "safe" and
              clears the expiry, then returns "safe".

        Returns:
            "safe" or "bypass".
        """
        row = self._storage.fetchone(
            "SELECT sandbox_mode_override, sandbox_mode_expires_at "
            "FROM sessions WHERE chat_id = ? AND agent_id = ?",
            (chat_id, agent_id),
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
            "WHERE chat_id = ? AND agent_id = ?",
            (now, chat_id, agent_id),
        )
        return "safe"

    def get_history(
        self, chat_id: str, agent_id: str
    ) -> list[dict[str, Any]]:
        """Get recent messages for context window construction.

        Returns a list of message dicts with role and content keys,
        ordered chronologically with compaction summaries first.

        Logic:
        1. Get all uncompacted messages (compacted=0) ordered by id
        2. Separate into compaction_summaries and normal_messages
        3. Assemble: [All summaries oldest first] + [All normal messages chronological]

        This ensures the LLM sees: [Summary] -> [Recent messages]
        """
        # Get all uncompacted messages ordered by id
        rows = self._storage.fetchall(
            "SELECT role, content, tool_calls, tool_call_id, is_compaction_summary "
            "FROM messages "
            "WHERE chat_id = ? AND agent_id = ? "
            "AND COALESCE(compacted, 0) = 0 "
            "ORDER BY id ASC",
            (chat_id, agent_id),
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
    ) -> None:
        """Persist a message to the history store."""
        now = int(time.time())
        self._storage.execute(
            "INSERT INTO messages (chat_id, agent_id, run_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, agent_id, run_id, role, content, now),
        )

    def count_messages(self, chat_id: str, agent_id: str) -> int:
        """Return the number of non-compacted messages for ``(chat_id, agent_id)``.

        Compaction summaries are still counted because they remain
        ``compacted=0`` in the schema; callers comparing against a threshold
        should be aware that summaries contribute to the total.
        """
        row = self._storage.fetchone(
            "SELECT COUNT(*) AS cnt FROM messages "
            "WHERE chat_id = ? AND agent_id = ? "
            "AND COALESCE(compacted, 0) = 0",
            (chat_id, agent_id),
        )
        if not row:
            return 0
        return int(row["cnt"] or 0)

    # ------------------------------------------------------------------
    # History compaction
    # ------------------------------------------------------------------

    def compact_history(
        self,
        chat_id: str,
        agent_id: str,
        keep_recent: int = DEFAULT_KEEP_RECENT,
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
        active_rows = self._storage.fetchall(
            "SELECT id, role, content, tool_calls, tool_call_id, run_id, "
            "is_compaction_summary "
            "FROM messages "
            "WHERE chat_id = ? AND agent_id = ? "
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
        state = TaskState.load(self._storage, chat_id, agent_id)

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
        self._storage.execute(
            "INSERT INTO messages "
            "(chat_id, agent_id, run_id, role, content, created_at, "
            "compacted, is_compaction_summary) "
            "VALUES (?, ?, ?, 'system', ?, ?, 0, 1)",
            (chat_id, agent_id, None, summary_text, now),
        )

        # ------------------------------------------------------------------
        # 4) Persist TaskState and optionally collapse old summaries.
        # ------------------------------------------------------------------
        state.save(self._storage, chat_id, agent_id)
        self._merge_old_summaries_if_needed(chat_id, agent_id)

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

    def _merge_old_summaries_if_needed(self, chat_id: str, agent_id: str) -> None:
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
        self._storage.execute(
            "INSERT INTO messages "
            "(chat_id, agent_id, run_id, role, content, created_at, "
            "compacted, is_compaction_summary) "
            "VALUES (?, ?, ?, 'system', ?, ?, 0, 1)",
            (chat_id, agent_id, None, merged_text, now),
        )
