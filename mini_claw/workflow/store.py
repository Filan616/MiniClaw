"""Persistence helpers for workflow runs, nodes, and compiled prompts."""

from __future__ import annotations

import json
import time
from typing import Any

from mini_claw.storage.db import Database
from mini_claw.workflow.spec import SubAgentPrompt, WorkflowNodeResult, WorkflowSpec


class WorkflowStore:
    def __init__(self, storage: Database) -> None:
        self._storage = storage

    @property
    def storage(self) -> Database:
        return self._storage

    def create_run(self, workflow_id: str, chat_id: str, agent_id: str, spec: WorkflowSpec, status: str = "planning") -> None:
        now = int(time.time())
        self._storage.execute(
            "INSERT INTO workflow_runs "
            "(workflow_id, chat_id, agent_id, status, spec_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (workflow_id, chat_id, agent_id, status, spec.to_json(), now, now),
        )
        for node in spec.nodes:
            self._storage.execute(
                "INSERT INTO workflow_nodes (workflow_id, node_id, status) VALUES (?, ?, 'pending')",
                (workflow_id, node.id),
            )

    def update_run_status(
        self,
        workflow_id: str,
        status: str,
        *,
        error: str | None = None,
        approval_id: str | None = None,
        approval_reason: str | None = None,
    ) -> None:
        now = int(time.time())
        self._storage.execute(
            "UPDATE workflow_runs SET status=?, error=COALESCE(?, error), "
            "approval_id=COALESCE(?, approval_id), approval_reason=COALESCE(?, approval_reason), updated_at=? "
            "WHERE workflow_id=?",
            (status, error, approval_id, approval_reason, now, workflow_id),
        )

    def mark_approved(self, workflow_id: str) -> None:
        now = int(time.time())
        self._storage.execute(
            "UPDATE workflow_runs SET status='running', approved_at=?, updated_at=? WHERE workflow_id=?",
            (now, now, workflow_id),
        )

    def mark_rejected(self, workflow_id: str) -> None:
        now = int(time.time())
        self._storage.execute(
            "UPDATE workflow_runs SET status='rejected', rejected_at=?, updated_at=? WHERE workflow_id=?",
            (now, now, workflow_id),
        )

    def save_prompt(self, workflow_id: str, node_id: str, prompt: SubAgentPrompt) -> None:
        self._storage.execute(
            "INSERT OR REPLACE INTO workflow_node_prompts "
            "(workflow_id, node_id, system_prompt, user_prompt, output_schema_json, compiled_at, redacted) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                workflow_id,
                node_id,
                prompt.system_prompt,
                prompt.user_prompt,
                json.dumps(prompt.output_schema, ensure_ascii=False),
                int(time.time()),
                1 if prompt.redacted else 0,
            ),
        )

    def update_node(self, workflow_id: str, result: WorkflowNodeResult) -> None:
        now = int(time.time())
        self._storage.execute(
            "UPDATE workflow_nodes SET status=?, agent_run_id=?, result_json=?, "
            "finished_at=CASE WHEN ? IN ('done', 'failed', 'skipped') THEN ? ELSE finished_at END, "
            "error=? WHERE workflow_id=? AND node_id=?",
            (
                result.status,
                result.agent_run_id,
                json.dumps(result.to_dict(), ensure_ascii=False),
                result.status,
                now,
                result.error,
                workflow_id,
                result.node_id,
            ),
        )

    def mark_node_running(self, workflow_id: str, node_id: str, agent_run_id: str | None = None) -> None:
        self._storage.execute(
            "UPDATE workflow_nodes SET status='running', agent_run_id=?, started_at=? WHERE workflow_id=? AND node_id=?",
            (agent_run_id, int(time.time()), workflow_id, node_id),
        )

    def get_run(self, workflow_id: str) -> dict[str, Any] | None:
        return self._storage.fetchone("SELECT * FROM workflow_runs WHERE workflow_id=?", (workflow_id,))

    def get_spec(self, workflow_id: str) -> WorkflowSpec | None:
        row = self.get_run(workflow_id)
        if not row:
            return None
        return WorkflowSpec.from_json(row["spec_json"])

    def list_nodes(self, workflow_id: str) -> list[dict[str, Any]]:
        return self._storage.fetchall(
            "SELECT * FROM workflow_nodes WHERE workflow_id=? ORDER BY rowid",
            (workflow_id,),
        )

    def list_prompts(self, workflow_id: str) -> list[dict[str, Any]]:
        return self._storage.fetchall(
            "SELECT node_id, system_prompt, user_prompt, output_schema_json, compiled_at, redacted "
            "FROM workflow_node_prompts WHERE workflow_id=? ORDER BY node_id",
            (workflow_id,),
        )
