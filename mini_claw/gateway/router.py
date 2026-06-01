"""Gateway router: central orchestrator for message handling and agent dispatch."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Optional, Set

from mini_claw.agent.context import AgentContext
from mini_claw.agent.loop import AgentRun, RunOutcome, run_agent_step
from mini_claw.channels.base import Channel, InboundMessage
from mini_claw.config import AgentConfig, AppConfig
from mini_claw.gateway.session import SessionManager
from mini_claw.providers.base import Provider
from mini_claw.storage.db import Database
from mini_claw.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class Gateway:
    """Central gateway that routes inbound messages to the correct agent."""

    def __init__(
        self,
        config: AppConfig,
        storage: Database,
        provider: Provider,
        registry: ToolRegistry,
        permission_gate: Any,
        result_processor: Any,
        workspace_manager: Any,
    ) -> None:
        self._config = config
        self._storage = storage
        self._provider = provider
        self._registry = registry
        self._permission_gate = permission_gate
        self._result_processor = result_processor
        self._workspace_manager = workspace_manager
        self._session_mgr = SessionManager(storage)
        self._processed_events: Set[str] = set()
        self._max_dedup_size = 10000

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def handle_message(self, msg: InboundMessage, channel: Channel) -> None:
        """Route an inbound message through the full processing pipeline."""
        # Event dedup
        if msg.event_id in self._processed_events:
            logger.debug("Duplicate event %s, skipping", msg.event_id)
            return
        self._processed_events.add(msg.event_id)
        if len(self._processed_events) > self._max_dedup_size:
            # Evict oldest half (set is unordered, but keeps memory bounded)
            to_remove = list(self._processed_events)[: self._max_dedup_size // 2]
            self._processed_events -= set(to_remove)

        # Resolve agent
        agent_cfg = self._resolve_agent(msg.chat_id)
        agent_id = agent_cfg.id

        # Get or create session
        session = self._session_mgr.get_or_create(msg.chat_id, agent_id)

        # Workspace
        workspace_dir = self._workspace_manager.get_workspace(
            msg.chat_id, agent_id
        )

        # Create agent_run record
        run_id = str(uuid.uuid4())
        now = int(time.time())
        self._storage.execute(
            "INSERT INTO agent_runs (id, chat_id, agent_id, status, user_message, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, msg.chat_id, agent_id, "running", msg.text, now, now),
        )

        # Create job record
        job_id = str(uuid.uuid4())
        self._storage.execute(
            "INSERT INTO jobs (id, chat_id, agent_id, type, status, instruction, run_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, msg.chat_id, agent_id, "interactive", "queued", msg.text, run_id, now, now),
        )

        # Build context and run agent loop
        ctx = AgentContext(
            chat_id=msg.chat_id,
            agent_id=agent_id,
            workspace_dir=workspace_dir,
            channel=channel,
        )

        # Load conversation history for context
        history = self._session_mgr.get_history(msg.chat_id, agent_id)
        messages = history + [{"role": "user", "content": msg.text}]

        run = AgentRun(
            id=run_id,
            chat_id=msg.chat_id,
            agent_id=agent_id,
            status=RunOutcome.DONE,
            messages=messages,
            allowed_tools=agent_cfg.tools,
        )

        # Update job to running
        self._storage.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
            ("running", int(time.time()), job_id),
        )

        try:
            run = await run_agent_step(
                run=run,
                provider=self._provider,
                registry=self._registry,
                permission_gate=self._permission_gate,
                result_processor=self._result_processor,
                ctx=ctx,
            )
        except Exception as exc:
            logger.exception("Agent run %s failed: %s", run_id, exc)
            run.status = RunOutcome.ABORTED
            run.final_answer = f"Internal error: {exc}"

        # Send result back via channel
        if run.final_answer:
            await channel.send(msg.chat_id, run.final_answer)

        # Persist run state
        final_status = run.status
        self._storage.execute(
            "UPDATE agent_runs SET status = ?, final_answer = ?, iterations = ?, updated_at = ? WHERE id = ?",
            (final_status, run.final_answer, run.iterations, int(time.time()), run_id),
        )

        # Update job status
        job_status = "completed" if final_status == RunOutcome.DONE else "failed"
        self._storage.execute(
            "UPDATE jobs SET status = ?, result = ?, updated_at = ? WHERE id = ?",
            (job_status, run.final_answer, int(time.time()), job_id),
        )

        # Store assistant message in history
        self._session_mgr.store_message(
            msg.chat_id, agent_id, "user", msg.text, run_id
        )
        if run.final_answer:
            self._session_mgr.store_message(
                msg.chat_id, agent_id, "assistant", run.final_answer, run_id
            )

    # ------------------------------------------------------------------
    # Approval handling
    # ------------------------------------------------------------------

    async def handle_approval(
        self, approval_id: str, decision: str, channel: Channel
    ) -> None:
        """Resolve a pending approval and resume the suspended agent run."""
        # Resolve approval via permission_gate
        approval = self._permission_gate.resolve_approval(approval_id, decision)
        if approval is None:
            logger.warning("Approval %s not found or already resolved", approval_id)
            return

        # Load suspended run
        run_row = self._storage.fetchone(
            "SELECT * FROM agent_runs WHERE id = ?", (approval.run_id,)
        )
        if run_row is None:
            logger.error("Run %s not found for approval %s", approval.run_id, approval_id)
            return

        agent_cfg = self._resolve_agent(run_row["chat_id"])
        workspace_dir = self._workspace_manager.get_workspace(
            run_row["chat_id"], run_row["agent_id"]
        )

        ctx = AgentContext(
            chat_id=run_row["chat_id"],
            agent_id=run_row["agent_id"],
            workspace_dir=workspace_dir,
            channel=channel,
        )

        # Rebuild run state
        history = self._session_mgr.get_history(
            run_row["chat_id"], run_row["agent_id"]
        )
        run = AgentRun(
            id=run_row["id"],
            chat_id=run_row["chat_id"],
            agent_id=run_row["agent_id"],
            status=RunOutcome.DONE,
            messages=history,
            iterations=run_row.get("iterations", 0),
            allowed_tools=agent_cfg.tools,
        )

        try:
            run = await run_agent_step(
                run=run,
                provider=self._provider,
                registry=self._registry,
                permission_gate=self._permission_gate,
                result_processor=self._result_processor,
                ctx=ctx,
            )
        except Exception as exc:
            logger.exception("Resumed run %s failed: %s", run.id, exc)
            run.status = RunOutcome.ABORTED
            run.final_answer = f"Internal error: {exc}"

        # Send result
        if run.final_answer:
            await channel.send(run_row["chat_id"], run.final_answer)

        # Update run
        self._storage.execute(
            "UPDATE agent_runs SET status = ?, final_answer = ?, iterations = ?, updated_at = ? WHERE id = ?",
            (run.status, run.final_answer, run.iterations, int(time.time()), run.id),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_agent(self, chat_id: str) -> AgentConfig:
        """Determine which agent config handles a given chat_id."""
        for agent_cfg in self._config.agents:
            if chat_id in agent_cfg.route_chat_ids:
                return agent_cfg
        # Fallback to first (default) agent
        return self._config.agents[0]
