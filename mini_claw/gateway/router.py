"""Gateway router: central orchestrator for message handling and agent dispatch."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Set

from mini_claw.agent.context import AgentContext
from mini_claw.agent.loop import AgentRun, RunOutcome, resume_after_approval, run_agent_step
from mini_claw.channels.base import Channel, InboundMessage
from mini_claw.config import AgentConfig, AppConfig
from mini_claw.gateway.session import SessionManager
from mini_claw.providers.base import Provider
from mini_claw.storage.db import Database
from mini_claw.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def _status_value(status: Any) -> str:
    """Coerce a status (RunOutcome enum or plain str) to a string for DB storage."""
    if hasattr(status, "value"):
        return status.value
    return str(status)


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
        self._channel: Channel | None = None

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def set_channel(self, channel: Channel) -> None:
        """Inject the outbound channel used to deliver replies and approval cards."""
        self._channel = channel

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def handle_message(self, msg: InboundMessage) -> None:
        """Route an inbound message through the full processing pipeline."""
        channel = self._channel
        if channel is None:
            logger.error("Gateway has no channel attached; dropping message %s", msg.event_id)
            return

        # Event dedup
        if msg.event_id in self._processed_events:
            logger.debug("Duplicate event %s, skipping", msg.event_id)
            return
        self._processed_events.add(msg.event_id)
        if len(self._processed_events) > self._max_dedup_size:
            to_remove = list(self._processed_events)[: self._max_dedup_size // 2]
            self._processed_events -= set(to_remove)

        # Resolve agent
        agent_cfg = self._resolve_agent(msg.chat_id)
        agent_id = agent_cfg.id

        # Get or create session
        self._session_mgr.get_or_create(msg.chat_id, agent_id)

        # Handle special commands: /bypass, /safe
        if msg.text.strip() == "/bypass":
            self._session_mgr.set_sandbox_mode(msg.chat_id, agent_id, "bypass")
            await channel.send(
                msg.chat_id,
                "✅ 已切换到 **bypass 模式**\n\n"
                "当前会话中，agent 可以读写整台电脑的任意文件。\n"
                "bash 黑名单仍然生效（`rm -rf /`、`curl|sh` 等仍会被拦截）。\n\n"
                "发送 `/safe` 可切回安全模式。"
            )
            return

        if msg.text.strip() == "/safe":
            self._session_mgr.set_sandbox_mode(msg.chat_id, agent_id, "safe")
            await channel.send(
                msg.chat_id,
                "✅ 已切换到 **safe 模式**\n\n"
                "路径限制在 workspace 内，敏感文件（.env、id_rsa 等）会被拦截。\n\n"
                "发送 `/bypass` 可临时获取整台电脑的权限。"
            )
            return

        # Workspace
        workspace_dir = self._workspace_manager.get_workspace(msg.chat_id, agent_id)

        # Determine sandbox_mode: session override > config default
        sandbox_mode = (
            self._session_mgr.get_sandbox_mode(msg.chat_id, agent_id)
            or self._config.permissions.sandbox_mode
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
            sandbox_mode=sandbox_mode,
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

        # If suspended, send approval card
        if run.status == RunOutcome.SUSPENDED and run.pending_approval_id and run.pending_tool_call:
            tool_call_data = json.loads(run.pending_tool_call)
            await channel.send_approval_card(
                chat_id=msg.chat_id,
                approval_id=run.pending_approval_id,
                tool_name=tool_call_data["name"],
                tool_args=tool_call_data["arguments"],
                level="high",  # Default to high for approval required
            )

        # Persist run state
        final_status = _status_value(run.status)
        self._storage.execute(
            "UPDATE agent_runs SET status = ?, final_answer = ?, iterations = ?, "
            "pending_approval_id = ?, pending_tool_call = ?, updated_at = ? WHERE id = ?",
            (final_status, run.final_answer, run.iterations,
             run.pending_approval_id, run.pending_tool_call, int(time.time()), run_id),
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

    async def handle_card_action(self, payload: dict) -> None:
        """Channel-facing entrypoint for card button clicks.

        ``payload`` is the dict the channel adapter forwards from the card
        button's ``value`` field — ``{"action": "approve"|"reject", "approval_id": ...}``.
        """
        approval_id = payload.get("approval_id")
        action = payload.get("action")
        if not approval_id or not action:
            logger.warning("Malformed card action payload: %r", payload)
            return
        decision = "approved" if action == "approve" else "rejected"
        await self.handle_approval(approval_id, decision)

    async def handle_approval(self, approval_id: str, decision: str) -> None:
        """Resolve a pending approval and resume the suspended agent run."""
        channel = self._channel
        if channel is None:
            logger.error("Gateway has no channel attached; dropping approval %s", approval_id)
            return

        approval = self._permission_gate.resolve(approval_id, decision)
        if approval is None:
            logger.warning("Approval %s not found or already resolved", approval_id)
            return

        run_row = self._storage.fetchone(
            "SELECT * FROM agent_runs WHERE id = ?", (approval["run_id"],)
        )
        if run_row is None:
            logger.error(
                "Run %s not found for approval %s", approval["run_id"], approval_id
            )
            return

        # Load pending_tool_call from DB
        pending_tool_call = run_row.get("pending_tool_call")
        if not pending_tool_call:
            logger.error(
                "Run %s has no pending_tool_call for approval %s", run_row["id"], approval_id
            )
            return

        agent_cfg = self._resolve_agent(run_row["chat_id"])
        workspace_dir = self._workspace_manager.get_workspace(
            run_row["chat_id"], run_row["agent_id"]
        )

        # Determine sandbox_mode: session override > config default
        sandbox_mode = (
            self._session_mgr.get_sandbox_mode(run_row["chat_id"], run_row["agent_id"])
            or self._config.permissions.sandbox_mode
        )

        ctx = AgentContext(
            chat_id=run_row["chat_id"],
            agent_id=run_row["agent_id"],
            workspace_dir=workspace_dir,
            channel=channel,
            sandbox_mode=sandbox_mode,
        )

        history = self._session_mgr.get_history(
            run_row["chat_id"], run_row["agent_id"]
        )
        run = AgentRun(
            id=run_row["id"],
            chat_id=run_row["chat_id"],
            agent_id=run_row["agent_id"],
            status=RunOutcome.SUSPENDED,
            messages=history,
            iterations=run_row.get("iterations", 0),
            allowed_tools=agent_cfg.tools,
            pending_approval_id=approval_id,
            pending_tool_call=pending_tool_call,
        )

        try:
            run = await resume_after_approval(
                run=run,
                approval=decision,
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

        if run.final_answer:
            await channel.send(run_row["chat_id"], run.final_answer)

        self._storage.execute(
            "UPDATE agent_runs SET status = ?, final_answer = ?, iterations = ?, updated_at = ? WHERE id = ?",
            (_status_value(run.status), run.final_answer, run.iterations, int(time.time()), run.id),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_agent(self, chat_id: str) -> AgentConfig:
        """Determine which agent config handles a given chat_id."""
        for agent_cfg in self._config.agents:
            if chat_id in agent_cfg.route_chat_ids:
                return agent_cfg
        return self._config.agents[0]
