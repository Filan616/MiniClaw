"""Task state for context preservation across history compactions.

A TaskState captures information that must survive when the message history
is compacted away: the original user request, extracted key facts, and a
rolling log of recent errors. It is persisted in the ``task_state`` table
keyed by ``(chat_id, agent_id)``.

Persistence layout
------------------
The ``task_state`` table has columns ``goal``, ``test_command``, and
``facts_json``. We map our richer dataclass onto it as follows:

* ``goal``        -> ``task_description`` (kept as a column for ad-hoc SQL).
* ``facts_json``  -> JSON blob carrying ``key_facts``, ``error_log``, and
                    ``compaction_count`` (the schema does not have dedicated
                    columns for these, so we co-locate them here).
* ``test_command`` is left untouched by this class (other components may
  populate it).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

# Cap for the rolling error log so the JSON blob stays bounded.
_MAX_ERROR_LOG = 20


@dataclass
class TaskState:
    """Durable task-level memory that outlives history compactions."""

    task_description: str = ""
    key_facts: list[str] = field(default_factory=list)
    error_log: list[dict] = field(default_factory=list)
    compaction_count: int = 0

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "task_description": self.task_description,
            "key_facts": list(self.key_facts),
            "error_log": [dict(e) for e in self.error_log],
            "compaction_count": self.compaction_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "TaskState":
        """Deserialize from a dict produced by :meth:`to_dict`.

        Tolerates ``None`` and missing keys so callers can feed in partial
        rows without extra guarding.
        """
        if not data:
            return cls()
        return cls(
            task_description=data.get("task_description", "") or "",
            key_facts=list(data.get("key_facts") or []),
            error_log=[dict(e) for e in (data.get("error_log") or [])],
            compaction_count=int(data.get("compaction_count") or 0),
        )

    # ------------------------------------------------------------------
    # Mutation helpers
    # ------------------------------------------------------------------

    def add_fact(self, fact: str) -> None:
        """Append a key fact, deduplicating against existing entries."""
        if not fact:
            return
        normalized = fact.strip()
        if not normalized:
            return
        if normalized in self.key_facts:
            return
        self.key_facts.append(normalized)

    def add_error(self, error_msg: str, run_id: str) -> None:
        """Append an error to the rolling log, capped at ``_MAX_ERROR_LOG``."""
        if not error_msg:
            return
        self.error_log.append(
            {
                "error_msg": str(error_msg),
                "run_id": run_id or "",
                "ts": int(time.time()),
            }
        )
        if len(self.error_log) > _MAX_ERROR_LOG:
            # Drop the oldest entries, keep the most recent window.
            self.error_log = self.error_log[-_MAX_ERROR_LOG:]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, storage: Any, chat_id: str, agent_id: str) -> None:
        """Upsert this state into the ``task_state`` table."""
        payload = {
            "key_facts": list(self.key_facts),
            "error_log": [dict(e) for e in self.error_log],
            "compaction_count": self.compaction_count,
        }
        storage.execute(
            "INSERT INTO task_state "
            "(chat_id, agent_id, goal, facts_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id, agent_id) DO UPDATE SET "
            "goal=excluded.goal, "
            "facts_json=excluded.facts_json, "
            "updated_at=excluded.updated_at",
            (
                chat_id,
                agent_id,
                self.task_description,
                json.dumps(payload, ensure_ascii=False),
                int(time.time()),
            ),
        )

    @classmethod
    def load(cls, storage: Any, chat_id: str, agent_id: str) -> "TaskState":
        """Load state for ``(chat_id, agent_id)``, or return a fresh instance."""
        row = storage.fetchone(
            "SELECT goal, facts_json FROM task_state "
            "WHERE chat_id = ? AND agent_id = ?",
            (chat_id, agent_id),
        )
        if not row:
            return cls()

        facts_blob = row.get("facts_json") or ""
        parsed: dict[str, Any] = {}
        if facts_blob:
            try:
                loaded = json.loads(facts_blob)
                if isinstance(loaded, dict):
                    parsed = loaded
            except (json.JSONDecodeError, TypeError):
                parsed = {}

        return cls(
            task_description=row.get("goal") or "",
            key_facts=list(parsed.get("key_facts") or []),
            error_log=[dict(e) for e in (parsed.get("error_log") or [])],
            compaction_count=int(parsed.get("compaction_count") or 0),
        )
