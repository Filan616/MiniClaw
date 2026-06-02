"""Gateway router: central orchestrator for message handling and agent dispatch."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from typing import Any, Set

from mini_claw.agent.context import AgentContext
from mini_claw.agent.loop import AgentRun, RunOutcome, resume_after_approval, run_agent_step
from mini_claw.agent.task_state import TaskState
from mini_claw.audit.logger import SecurityAuditLogger
from mini_claw.channels.base import Channel, InboundMessage
from mini_claw.commands.bypass import handle_bypass_command
from mini_claw.config import AgentConfig, AppConfig
from mini_claw.gateway.session import SessionManager
from mini_claw.permissions.chain_detector import ChainDetector
from mini_claw.providers.base import Provider
from mini_claw.storage.db import Database
from mini_claw.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


# Message-count threshold above which we auto-compact the active session.
AUTO_COMPACT_THRESHOLD = 50
# Number of most recent messages compaction should leave untouched.
AUTO_COMPACT_KEEP_RECENT = 20


def _status_value(status: Any) -> str:
    """Coerce a status (RunOutcome enum or plain str) to a string for DB storage."""
    if hasattr(status, "value"):
        return status.value
    return str(status)


class Gateway:
    """Central gateway that routes inbound messages to the correct agent.

    Concurrency notes:
    - Per-workspace lock is single-process only (asyncio.Lock in memory)
    - Multi-process deployments need SQLite advisory lock, file lock, or Redis lock
    """

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
        self._audit_logger = SecurityAuditLogger(storage)
        self._chain_detector = ChainDetector()
        self._dedup_lock = asyncio.Lock()  # Protects processed_events INSERT
        self._workspace_locks: dict[str, asyncio.Lock] = {}  # Per-workspace concurrency control
        self._channel: Channel | None = None

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def set_channel(self, channel: Channel) -> None:
        """Inject the outbound channel used to deliver replies and approval cards."""
        self._channel = channel

    async def _with_workspace_lock(self, agent_id: str, workspace_dir: str, coro):
        """Unified workspace lock wrapper for all entry points that execute tools."""
        lock_key = f"{agent_id}:{workspace_dir}"
        lock = self._workspace_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            return await coro

    async def _heartbeat_loop(self, event_id: str, interval: int = 30) -> None:
        """Background heartbeat task to update heartbeat_at for long-running tasks."""
        while True:
            await asyncio.sleep(interval)
            self._storage.execute(
                "UPDATE processed_events SET heartbeat_at=? WHERE event_id=?",
                (int(time.time()), event_id),
            )

    async def _execute_agent_run(
        self,
        run: AgentRun,
        agent_cfg: AgentConfig,
        ctx: AgentContext,
        chat_id: str,
        agent_id: str,
        run_id: str,
        job_id: str,
        event_id: str,
    ) -> None:
        """Execute the agent run and handle all state persistence.

        This method:
        1. Executes run_agent_step with all necessary parameters
        2. Handles exceptions (mark as ABORTED)
        3. Sends final_answer via channel
        4. Sends approval card if suspended
        5. Persists run state to agent_runs table
        6. Updates job status
        7. Stores messages in history
        8. Marks event as handled in processed_events
        9. Resets single-use bypass mode if expires_at=0
        """
        channel = self._channel
        if channel is None:
            raise RuntimeError("Gateway has no channel attached")

        try:
            # Execute agent loop
            run = await run_agent_step(
                run=run,
                provider=self._provider,
                registry=self._registry,
                permission_gate=self._permission_gate,
                result_processor=self._result_processor,
                ctx=ctx,
            )
        except Exception as exc:
            logger.exception("Agent run %s failed with exception: %s", run_id, exc)
            run.status = RunOutcome.ABORTED
            run.final_answer = f"Internal error: {exc}"
        finally:
            # Reset single-use bypass after consumption (uses the actual
            # session-table column names: sandbox_mode_override,
            # sandbox_mode_expires_at, sandbox_mode_single_use).
            self._session_mgr.clear_single_use_bypass(chat_id, agent_id)

        # Send final answer if present
        if run.final_answer:
            await channel.send(chat_id, run.final_answer)

        # Send approval card if suspended
        if run.status == RunOutcome.SUSPENDED and run.pending_approval_id:
            # Phase 0.3: Send approval card to channel (Phase 0 阶段沿用
            # self._channel；Phase 2 完成后统一改为 channel_manager 路由)
            if run.pending_tool_call:
                try:
                    pending = json.loads(run.pending_tool_call)
                    tool_name = pending.get("name", "unknown")
                    tool_args = pending.get("arguments", {})
                    level = pending.get("level", "L3")  # fallback to L3
                    await channel.send_approval_card(
                        chat_id, run.pending_approval_id, tool_name, tool_args, level
                    )
                except (json.JSONDecodeError, TypeError) as exc:
                    logger.warning(
                        "Failed to parse pending_tool_call for approval card: %s", exc
                    )
            else:
                logger.warning(
                    "Run %s suspended but no pending_tool_call to build card",
                    run_id,
                )

        # Persist run state to agent_runs table
        now = int(time.time())
        self._storage.execute(
            "UPDATE agent_runs SET status=?, final_answer=?, iterations=?, "
            "pending_tool_call=?, updated_at=? WHERE id=?",
            (
                _status_value(run.status),
                run.final_answer,
                run.iterations,
                run.pending_tool_call,
                now,
                run_id,
            ),
        )

        # Update job status
        job_status = "completed" if run.status == RunOutcome.DONE else "failed"
        if run.status == RunOutcome.SUSPENDED:
            job_status = "suspended"
        self._storage.execute(
            "UPDATE jobs SET status=?, updated_at=? WHERE id=?",
            (job_status, now, job_id),
        )

        # Store assistant messages in history. The inbound user message is
        # persisted earlier in ``handle_message`` so it can participate in
        # the auto-compaction threshold check; storing it again here would
        # produce duplicates.
        for msg in run.messages:
            if msg.get("role") == "assistant":
                content = msg.get("content")
                if content:
                    self._session_mgr.store_message(
                        chat_id,
                        agent_id,
                        "assistant",
                        content,
                        run_id=run_id,
                    )

        # Mark event as handled in processed_events
        self._storage.execute(
            "UPDATE processed_events SET status='handled', finished_at=?, run_id=? WHERE event_id=?",
            (now, run_id, event_id),
        )

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def handle_message(self, msg: InboundMessage) -> None:
        """Route an inbound message through the full processing pipeline."""
        channel = self._channel
        if channel is None:
            logger.error("Gateway has no channel attached; dropping message %s", msg.event_id)
            return

        run_id: str | None = None  # Pre-initialize to avoid undefined in exception path

        # ========== Event deduplication with crash recovery ==========
        async with self._dedup_lock:
            try:
                now = int(time.time())
                self._storage.execute(
                    "INSERT INTO processed_events "
                    "(event_id, chat_id, status, started_at, heartbeat_at) "
                    "VALUES (?, ?, 'processing', ?, ?)",
                    (msg.event_id, msg.chat_id, now, now),
                )
            except sqlite3.IntegrityError:
                existing = self._storage.fetchone(
                    "SELECT status FROM processed_events WHERE event_id = ?",
                    (msg.event_id,),
                )

                if existing["status"] == "handled":
                    return  # Already processed, skip

                if existing["status"] == "processing":
                    # Don't preempt stale processing here to avoid hurting long tasks.
                    # Stale recovery only happens on service startup (see app.py).
                    return

                elif existing["status"] == "failed":
                    # Last attempt failed, allow retry
                    now = int(time.time())
                    self._storage.execute(
                        "UPDATE processed_events "
                        "SET status='processing', started_at=?, heartbeat_at=?, "
                        "finished_at=NULL, error=NULL, attempt_count=attempt_count+1 "
                        "WHERE event_id = ?",
                        (now, now, msg.event_id),
                    )

        # Start background heartbeat task
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(msg.event_id, interval=30)
        )

        try:
            # Resolve agent
            agent_cfg = self._resolve_agent(msg.chat_id)
            agent_id = agent_cfg.id

            # Get or create session
            self._session_mgr.get_or_create(msg.chat_id, agent_id)

            # Handle /bypass subcommands via the dedicated command module.
            # Covers /bypass, /bypass next, /bypass <duration>, /bypass persistent,
            # and /bypass confirm. Returns None if the message is not a /bypass command.
            bypass_result = handle_bypass_command(
                self._storage, msg.chat_id, agent_id, msg.text
            )
            if bypass_result is not None:
                await channel.send(msg.chat_id, bypass_result.message)
                # Mark event as handled
                self._storage.execute(
                    "UPDATE processed_events SET status='handled', finished_at=? WHERE event_id=?",
                    (int(time.time()), msg.event_id),
                )
                return

            # Handle special command: /safe (not covered by handle_bypass_command)
            if msg.text.strip() == "/safe":
                self._session_mgr.set_sandbox_mode(msg.chat_id, agent_id, "safe")
                await channel.send(
                    msg.chat_id,
                    "✅ 已切换到 **safe 模式**\n\n"
                    "路径限制在 workspace 内，敏感文件（.env、id_rsa 等）会被拦截。\n\n"
                    "发送 `/bypass` 可临时获取整台电脑的权限。",
                )
                # Mark event as handled before returning
                self._storage.execute(
                    "UPDATE processed_events SET status='handled', finished_at=? WHERE event_id=?",
                    (int(time.time()), msg.event_id),
                )
                return

            # Handle TaskState slash commands: /pin, /goal, /tasks, /compact
            if await self._handle_task_state_command(msg, agent_id, channel):
                # Mark event as handled before returning
                self._storage.execute(
                    "UPDATE processed_events SET status='handled', finished_at=? WHERE event_id=?",
                    (int(time.time()), msg.event_id),
                )
                return

            # Workspace
            workspace_dir = self._workspace_manager.get_workspace(msg.chat_id, agent_id)

            # Determine effective sandbox_mode (auto-reverts expired TTL bypass to "safe")
            sandbox_mode = self._resolve_sandbox_mode(msg.chat_id, agent_id)

            # Persist the inbound user message before invoking the agent so it
            # counts toward auto-compaction and is visible to history readers.
            self._session_mgr.store_message(
                msg.chat_id, agent_id, "user", msg.text
            )

            # Auto-compact when the active history exceeds the threshold so the
            # next request runs against a digested context window.
            total_messages = self._session_mgr.count_messages(msg.chat_id, agent_id)
            if total_messages > AUTO_COMPACT_THRESHOLD:
                compacted = self._session_mgr.compact_history(
                    msg.chat_id, agent_id, keep_recent=AUTO_COMPACT_KEEP_RECENT
                )
                if compacted:
                    logger.info(
                        "Auto-compacted %d messages for chat=%s agent=%s",
                        compacted, msg.chat_id, agent_id,
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
                audit_logger=self._audit_logger,
                chain_detector=self._chain_detector,
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

            # ========== Execute agent loop with workspace lock ==========
            # Per-workspace lock ensures concurrent messages to same workspace are serialized
            await self._with_workspace_lock(
                agent_id, str(workspace_dir),
                self._execute_agent_run(run, agent_cfg, ctx, msg.chat_id, agent_id, run_id, job_id, msg.event_id)
            )

        except Exception as exc:
            # Mark event as failed
            self._storage.execute(
                "UPDATE processed_events "
                "SET status='failed', finished_at=?, error=?, run_id=? WHERE event_id=?",
                (int(time.time()), str(exc)[:500], run_id, msg.event_id),
            )
            raise
        finally:
            # Always cancel heartbeat task
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

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

        # Determine sandbox_mode honoring TTL semantics (Phase 0.4: aligned
        # with handle_message — the previous direct ``get_sandbox_mode`` call
        # ignored expires_at/single_use and could resume a run with a stale
        # bypass override).
        sandbox_mode = self._resolve_sandbox_mode(
            run_row["chat_id"], run_row["agent_id"]
        )

        ctx = AgentContext(
            chat_id=run_row["chat_id"],
            agent_id=run_row["agent_id"],
            workspace_dir=workspace_dir,
            channel=channel,
            sandbox_mode=sandbox_mode,
            audit_logger=self._audit_logger,
            chain_detector=self._chain_detector,
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

        async def _do_resume() -> None:
            nonlocal run
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

        # ========== Resume run with workspace lock ==========
        # Per-workspace lock ensures approval resume is serialized with concurrent
        # messages targeting the same workspace.
        await self._with_workspace_lock(
            run_row["agent_id"], str(workspace_dir), _do_resume()
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _handle_task_state_command(
        self,
        msg: InboundMessage,
        agent_id: str,
        channel: Channel,
    ) -> bool:
        """Dispatch the TaskState slash commands.

        Supported commands:

        * ``/pin <fact>``     — append a key fact to the persisted TaskState.
        * ``/goal <text>``    — overwrite the task description / goal.
        * ``/tasks``          — render the current TaskState back to the user.
        * ``/compact``        — manually trigger history compaction.

        Returns ``True`` when the message was recognized as a TaskState
        command (regardless of whether the payload was valid), so the caller
        can short-circuit the regular agent dispatch path. Returns ``False``
        when the message is not one of these commands.
        """
        text = (msg.text or "").strip()
        if not text.startswith("/"):
            return False

        # Split into command and the rest of the line for argument parsing.
        head, _, tail = text.partition(" ")
        head = head.lower()
        argument = tail.strip()

        if head == "/pin":
            if not argument:
                await channel.send(
                    msg.chat_id,
                    "用法：`/pin <要记住的事实>`",
                )
                return True
            state = TaskState.load(self._storage, msg.chat_id, agent_id)
            state.add_fact(argument)
            state.save(self._storage, msg.chat_id, agent_id)
            await channel.send(
                msg.chat_id,
                f"已记录关键信息：{argument}",
            )
            return True

        if head == "/goal":
            if not argument:
                await channel.send(
                    msg.chat_id,
                    "用法：`/goal <任务目标描述>`",
                )
                return True
            state = TaskState.load(self._storage, msg.chat_id, agent_id)
            state.task_description = argument
            state.save(self._storage, msg.chat_id, agent_id)
            await channel.send(
                msg.chat_id,
                f"任务目标已更新：{argument}",
            )
            return True

        if head == "/tasks":
            state = TaskState.load(self._storage, msg.chat_id, agent_id)
            lines = ["**当前任务状态**"]
            goal = (state.task_description or "").strip()
            lines.append(f"目标：{goal or '(未设置，使用 /goal 设置)'}")
            if state.key_facts:
                lines.append("关键信息：")
                for fact in state.key_facts:
                    lines.append(f"- {fact}")
            else:
                lines.append("关键信息：(空，使用 /pin 添加)")
            if state.error_log:
                lines.append("近期错误：")
                for err in state.error_log[-5:]:
                    err_msg = (err.get("error_msg") or "").strip()
                    if err_msg:
                        lines.append(f"- {err_msg}")
            else:
                lines.append("近期错误：无")
            lines.append(f"已压缩次数：{state.compaction_count}")
            await channel.send(msg.chat_id, "\n".join(lines))
            return True

        if head == "/compact":
            keep_recent = AUTO_COMPACT_KEEP_RECENT
            # Allow `/compact <n>` to override the keep_recent window.
            if argument:
                try:
                    keep_recent = max(0, int(argument))
                except ValueError:
                    await channel.send(
                        msg.chat_id,
                        "用法：`/compact [保留最近 N 条消息，默认 20]`",
                    )
                    return True
            compacted = self._session_mgr.compact_history(
                msg.chat_id, agent_id, keep_recent=keep_recent
            )
            if compacted:
                await channel.send(
                    msg.chat_id,
                    f"已压缩 {compacted} 条历史消息（保留最近 {keep_recent} 条）。",
                )
            else:
                await channel.send(
                    msg.chat_id,
                    f"暂无可压缩的历史消息（保留窗口 {keep_recent}）。",
                )
            return True

        return False

    def _resolve_agent(self, chat_id: str) -> AgentConfig:
        """Determine which agent config handles a given chat_id."""
        for agent_cfg in self._config.agents:
            if chat_id in agent_cfg.route_chat_ids:
                return agent_cfg
        return self._config.agents[0]

    def _resolve_sandbox_mode(self, chat_id: str, agent_id: str) -> str:
        """Return the effective sandbox mode for ``(chat_id, agent_id)``.

        Centralizes the TTL-aware resolution so ``handle_message`` and
        ``handle_approval`` cannot drift apart — the previous direct
        ``get_sandbox_mode`` call in ``handle_approval`` skipped TTL semantics
        and could resume a run under a stale bypass.
        """
        return self._session_mgr.get_effective_sandbox_mode(chat_id, agent_id)
