"""Gateway router: central orchestrator for message handling and agent dispatch."""

from __future__ import annotations

import asyncio
import hashlib
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
from mini_claw.gateway.session import SessionManager, derive_session_id
from mini_claw.permissions.chain_detector import ChainDetector
from mini_claw.providers.base import Provider
from mini_claw.providers.manager import ProviderManager
from mini_claw.storage.db import Database
from mini_claw.tools.registry import ToolRegistry
from mini_claw.workflow.merger import WorkflowMerger
from mini_claw.workflow.planner import WorkflowPlanner
from mini_claw.workflow.prompt_compiler import SubAgentPromptCompiler
from mini_claw.workflow.runner import WorkflowRunner
from mini_claw.workflow.spec import validate_workflow_spec
from mini_claw.workflow.store import WorkflowStore

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
        provider: Provider | None = None,
        registry: ToolRegistry | None = None,
        permission_gate: Any | None = None,
        result_processor: Any | None = None,
        workspace_manager: Any | None = None,
        provider_manager: Any | None = None,
        agent_manager: Any | None = None,
        channel_manager: Any | None = None,
        skill_manager: Any | None = None,
        rag_manager: Any | None = None,
        chat_search_manager: Any | None = None,
    ) -> None:
        if registry is None or permission_gate is None or result_processor is None or workspace_manager is None:
            raise TypeError(
                "Gateway requires registry, permission_gate, result_processor, and workspace_manager"
            )
        self._config = config
        # Phase 10 §6: WorkflowRunner needs to reach the unified
        # ``cfg.agent.react`` block when resolving per-node ReActPolicy.
        # Stash a reverse pointer so it can read it without changing the
        # public WorkflowConfig surface.
        try:
            self._config.workflow._app_config = self._config  # type: ignore[attr-defined]
        except Exception:
            pass
        self._storage = storage
        self._provider_manager = provider_manager or ProviderManager(config, default_provider=provider)
        self._agent_manager = agent_manager
        self._channel_manager = channel_manager
        self._skill_manager = skill_manager
        self._rag_manager = rag_manager  # Phase 8 M2
        self._chat_search_manager = chat_search_manager  # Phase 9 M9.1
        self._registry = registry
        self._permission_gate = permission_gate
        self._result_processor = result_processor
        self._workspace_manager = workspace_manager
        self._session_mgr = SessionManager(storage)
        self._audit_logger = SecurityAuditLogger(storage)
        self._chain_detector = ChainDetector(
            config={
                "enabled": config.permissions.chain_detector.enabled,
                "session_scope": config.permissions.chain_detector.session_scope,
                "session_ttl": config.permissions.chain_detector.session_ttl,
                "export_large_threshold": getattr(
                    config.rag.memory_control, "export_large_threshold", 50
                ),
            },
            storage=storage,
        )
        self._workflow_store = WorkflowStore(storage)
        self._workflow_planner = WorkflowPlanner(config.workflow)
        self._workflow_prompt_compiler = SubAgentPromptCompiler(config.workflow)
        self._dedup_lock = asyncio.Lock()  # Protects processed_events INSERT
        self._workspace_locks: dict[str, asyncio.Lock] = {}  # Per-workspace concurrency control
        self._channel: Channel | None = None

        # Phase 9 P0.1: Best-effort backfill of messages.workspace_dir for existing messages
        if workspace_manager is not None:
            try:
                backfill_stats = storage.backfill_workspace_dir(
                    lambda chat_id, agent_id: workspace_manager.get_workspace(chat_id, agent_id)
                )
                if backfill_stats["updated"] > 0:
                    logger.info(
                        "Backfilled workspace_dir for %d messages (failed=%d, skipped=%d)",
                        backfill_stats["updated"],
                        backfill_stats["failed"],
                        backfill_stats["skipped"],
                    )
            except Exception as exc:
                logger.warning("workspace_dir backfill failed: %s", exc)

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def set_channel(self, channel: Channel) -> None:
        """Inject the outbound channel used to deliver replies and approval cards."""
        self._channel = channel
        if self._channel_manager is not None:
            self._channel_manager.register_instance(channel)

    def set_channel_manager(self, channel_manager: Any) -> None:
        """Inject ChannelManager after construction."""
        self._channel_manager = channel_manager
        self._channel_manager.set_gateway(self)
        if self._channel is None and self._channel_manager.has_channel("feishu"):
            self._channel = self._channel_manager.get_channel("feishu")

    def _get_outbound_channel(self, channel_name: str = "feishu") -> Channel | None:
        if self._channel_manager is not None and self._channel_manager.has_channel(channel_name):
            return self._channel_manager.get_channel(channel_name)
        return self._channel

    async def _handle_feishu_command(self, msg: InboundMessage, channel: Channel) -> bool:
        """Handle Feishu channel diagnostics commands."""
        text = msg.text.strip()
        if text not in {"/feishu", "/feishu status", "/feishu help"}:
            return False

        if text in {"/feishu", "/feishu help"}:
            await channel.send(
                msg.chat_id,
                "Usage: /feishu status\n\n"
                "Shows Feishu long-connection health: thread state, received event "
                "count, last event time, and idle seconds.",
            )
            return True

        candidates: list[Channel] = []
        if self._channel_manager is not None:
            candidates.extend(self._channel_manager.list_channels())
        elif self._channel is not None:
            candidates.append(self._channel)

        feishu_channels = [
            ch for ch in candidates
            if getattr(ch, "channel_type", "") == "feishu" or hasattr(ch, "health_status")
        ]
        if not feishu_channels:
            await channel.send(msg.chat_id, "No Feishu channel is loaded.")
            return True

        lines = ["Feishu status:"]
        for ch in feishu_channels:
            health_fn = getattr(ch, "health_status", None)
            if not callable(health_fn):
                lines.append(f"\n- {ch.name}: health_status unavailable")
                continue
            lines.extend(self._format_feishu_status(health_fn()))

        await channel.send(msg.chat_id, "\n".join(lines))
        return True

    def _format_feishu_status(self, status: dict[str, Any]) -> list[str]:
        def fmt_ts(value: Any) -> str:
            if value in (None, ""):
                return "-"
            try:
                return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(value)))
            except (TypeError, ValueError, OSError):
                return str(value)

        idle = status.get("idle_seconds")
        idle_text = "never" if idle is None else f"{idle}s"
        uptime = status.get("uptime_seconds")
        uptime_text = "-" if uptime is None else f"{uptime}s"
        return [
            f"\n- Channel: {status.get('channel_name', '-')}",
            f"  app_id: {status.get('app_id', '-')}",
            f"  ws_thread_alive: {status.get('ws_thread_alive')}",
            f"  main_loop_alive: {status.get('main_loop_alive')}",
            f"  started_at: {fmt_ts(status.get('started_at'))}",
            f"  uptime: {uptime_text}",
            f"  received_count: {status.get('received_count', 0)}",
            f"  malformed_count: {status.get('malformed_count', 0)}",
            f"  last_event_at: {fmt_ts(status.get('last_event_at'))}",
            f"  idle: {idle_text}",
            f"  last_event_id: {status.get('last_event_id') or '-'}",
            f"  last_chat_id: {status.get('last_chat_id') or '-'}",
            f"  last_sender_id: {status.get('last_sender_id') or '-'}",
            f"  last_message_type: {status.get('last_message_type') or '-'}",
            f"  ws_exited_at: {fmt_ts(status.get('ws_exited_at'))}",
            f"  ws_exception: {status.get('ws_exception') or '-'}",
            f"  restart_on_disconnect: {status.get('restart_on_disconnect')}",
            f"  health_check_interval_sec: {status.get('health_check_interval_sec')}",
            f"  idle_restart_seconds: {status.get('idle_restart_seconds')}",
            f"  restart_count: {status.get('restart_count', 0)}",
            f"  last_restart_at: {fmt_ts(status.get('last_restart_at'))}",
            f"  last_restart_reason: {status.get('last_restart_reason') or '-'}",
        ]

    async def _send_prelude(
        self,
        chat_id: str,
        agent_id: str,
        channel: Channel,
        channel_name: str,
        workspace_dir: str,
        run_id: str,
        text: str,
    ) -> None:
        """Phase 9.7 (legacy): Send prelude to user and store with message_kind='prelude'.

        DEPRECATED for AgentLoop usage — Phase 10 M10.1 routes process
        messages through ``_send_react_user_update``. Kept for command
        helpers that haven't been migrated to ReActUserUpdate (eg.
        ``/context index`` prelude templates) and for backwards-compat
        bridging in tests that bind ``on_prelude`` directly.
        """
        try:
            await channel.send(chat_id, text)
            self._session_mgr.store_message(
                chat_id=chat_id,
                agent_id=agent_id,
                role="assistant",
                content=text,
                run_id=run_id,
                channel_name=channel_name,
                workspace_dir=workspace_dir,
                message_kind="prelude",
            )
        except Exception:
            logger.warning(
                "Prelude send or storage failed",
                exc_info=True,
                extra={"chat_id": chat_id, "run_id": run_id},
            )

    async def _send_react_user_update(
        self,
        chat_id: str,
        agent_id: str,
        channel: Channel,
        channel_name: str,
        workspace_dir: str,
        run_id: str,
        update: Any,
    ) -> bool:
        """Phase 10 M10.1: deliver a ReActUserUpdate to the user.

        Sends ``update.text`` over the channel, persists the row to
        ``react_user_updates`` (so the trace layer sees it), and mirrors
        the update as a ``messages.message_kind='react_update'`` row with
        the (update_id, step_id, event_type, ...) blob in metadata_json.

        Returns True iff the channel send completed successfully. Storage
        always runs — even on send failure — so audit / trace remain
        complete.
        """
        from mini_claw.agent.react_update import store_react_update

        send_ok = False
        try:
            await channel.send(chat_id, update.text)
            send_ok = True
            update.send_status = "sent"
            update.sent_at = int(time.time())
        except Exception:
            update.send_status = "failed"
            logger.warning(
                "react_user_update send failed",
                exc_info=True,
                extra={"chat_id": chat_id, "run_id": run_id, "update_id": update.update_id},
            )

        store_react_update(
            self._storage,
            update,
            store_redacted_text=self._config.agent.react_user_updates.store_redacted_text,
        )

        try:
            self._session_mgr.store_message(
                chat_id=chat_id,
                agent_id=agent_id,
                role="assistant",
                content=update.text,
                run_id=run_id,
                channel_name=channel_name,
                workspace_dir=workspace_dir,
                message_kind="react_update",
                metadata={
                    "react_update_id": update.update_id,
                    "react_step_id": update.step_id,
                    "react_event_type": update.event_type,
                    "visible_level": update.visible_level,
                    "is_important": bool(update.is_important),
                },
            )
        except Exception:
            logger.warning(
                "react_user_update mirror failed",
                exc_info=True,
                extra={"chat_id": chat_id, "run_id": run_id, "update_id": update.update_id},
            )

        if self._audit_logger:
            try:
                self._audit_logger.log_security_event(
                    event_type="react_user_update_created",
                    details={
                        "run_id": run_id,
                        "step_id": update.step_id,
                        "update_id": update.update_id,
                        "event_type": update.event_type,
                        "text_hash": update.text_hash,
                        "is_important": bool(update.is_important),
                        "send_status": update.send_status,
                    },
                    chat_id=chat_id,
                    agent_id=agent_id,
                )
            except Exception:
                pass

        return send_ok

    async def _send_progress(
        self,
        chat_id: str,
        agent_id: str,
        channel: Channel,
        channel_name: str,
        workspace_dir: str,
        run_id: str,
        text: str,
    ) -> None:
        """Phase 9.8: Send progress update to user and store with message_kind='progress'.

        Called by AgentLoop periodically to show the user what's happening.
        Fire-and-forget: failures are logged but do not interrupt execution.
        """
        try:
            await channel.send(chat_id, text)
            self._session_mgr.store_message(
                chat_id=chat_id,
                agent_id=agent_id,
                role="assistant",
                content=text,
                run_id=run_id,
                channel_name=channel_name,
                workspace_dir=workspace_dir,
                message_kind="progress",
            )
        except Exception:
            logger.warning(
                "Progress update send or storage failed",
                exc_info=True,
                extra={"chat_id": chat_id, "run_id": run_id},
            )

    async def _send_command_prelude(
        self,
        msg: InboundMessage,
        channel: Channel,
        channel_name: str,
        agent_id: str,
        text: str,
    ) -> None:
        """Phase 9.7 M4: Send prelude for command-initiated long tasks.

        Used by /context index, /context reindex, /memory export, /maintenance, etc.
        Fire-and-forget: failures are logged but do not interrupt command execution.
        """
        workspace_dir = self._workspace_manager.get_workspace(msg.chat_id, agent_id)
        try:
            await channel.send(msg.chat_id, text)
            self._session_mgr.store_message(
                chat_id=msg.chat_id,
                agent_id=agent_id,
                role="assistant",
                content=text,
                run_id=None,  # Commands don't have a run_id
                channel_name=channel_name,
                workspace_dir=workspace_dir,
                message_kind="prelude",
            )
        except Exception:
            logger.warning(
                "Command prelude send or storage failed",
                exc_info=True,
                extra={"chat_id": msg.chat_id, "command": msg.text[:50]},
            )

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
        channel_name: str = "feishu",
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
        channel = self._get_outbound_channel(channel_name)
        if channel is None:
            raise RuntimeError("Gateway has no channel attached")

        try:
            # Execute agent loop
            run = await run_agent_step(
                run=run,
                provider=self._provider_manager.get_provider_for_agent(agent_cfg),
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
            self._session_mgr.clear_single_use_bypass(chat_id, agent_id, channel_name=channel_name)

        # Send final answer if present
        if run.final_answer:
            await channel.send(chat_id, run.final_answer)

        # Phase 9 M9.4: optional agent-summary candidate extraction.
        # Gated by memory_control.auto_candidate_from_agent (default off).
        if (
            run.status == RunOutcome.DONE
            and run.final_answer
            and self._rag_manager is not None
            and self._config.rag.enabled
            and self._config.rag.namespaces.memory_enabled
            and getattr(
                self._config.rag.memory_control,
                "auto_candidate_from_agent",
                False,
            )
        ):
            try:
                workspace_dir = (
                    str(self._workspace_manager.get_workspace(chat_id, agent_id))
                    if self._workspace_manager
                    else None
                )
                n = self._rag_manager.submit_agent_summary_candidates(
                    run.final_answer,
                    chat_id=chat_id,
                    agent_id=agent_id,
                    channel=channel_name,
                    workspace_dir=workspace_dir,
                )
                if n and self._audit_logger:
                    self._audit_logger.log_security_event(
                        event_type="memory_candidate_created",
                        details={"source": "agent_summary", "count": n},
                        chat_id=chat_id,
                        agent_id=agent_id,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("agent summary memory extraction failed: %s", exc)

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
        # Phase B.4: Compute total_tokens from prompt + completion (best-effort)
        prompt_tokens = getattr(run, "prompt_tokens", 0) or 0
        completion_tokens = getattr(run, "completion_tokens", 0) or 0
        total_tokens = prompt_tokens + completion_tokens
        self._storage.execute(
            "UPDATE agent_runs SET status=?, final_answer=?, iterations=?, "
            "pending_tool_call=?, total_tokens=?, "
            "react_mode=COALESCE(?, react_mode), "
            "original_goal_raw=COALESCE(?, original_goal_raw), "
            "original_goal_summary=COALESCE(?, original_goal_summary), "
            "final_reflection_json=COALESCE(?, final_reflection_json), "
            "updated_at=? WHERE id=?",
            (
                _status_value(run.status),
                run.final_answer,
                run.iterations,
                run.pending_tool_call,
                total_tokens,
                getattr(ctx.react_policy, "mode", None) if getattr(ctx, "react_policy", None) else None,
                getattr(run, "original_goal_raw", None),
                getattr(run, "original_goal_summary", None),
                getattr(run, "final_reflection_json", None),
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
                    channel_name=channel_name,
                    workspace_dir=str(ctx.workspace_dir) if ctx.workspace_dir else None,
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
        channel_name = getattr(msg, "channel_name", "feishu")
        channel = self._get_outbound_channel(channel_name)
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
                    "(event_id, channel_name, chat_id, status, started_at, heartbeat_at) "
                    "VALUES (?, ?, ?, 'processing', ?, ?)",
                    (msg.event_id, channel_name, msg.chat_id, now, now),
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
            agent_cfg = self._resolve_agent(msg.chat_id, channel_name)
            agent_id = agent_cfg.id

            # Get or create session
            self._session_mgr.get_or_create(msg.chat_id, agent_id, channel_name=channel_name)

            # Handle /bypass subcommands via the dedicated command module.
            # Covers /bypass, /bypass next, /bypass <duration>, /bypass persistent,
            # and /bypass confirm. Returns None if the message is not a /bypass command.
            bypass_result = handle_bypass_command(
                self._storage, msg.chat_id, agent_id, msg.text, channel_name=channel_name
            )
            if bypass_result is not None:
                await channel.send(msg.chat_id, bypass_result.message)
                # Mark event as handled
                self._storage.execute(
                    "UPDATE processed_events SET status='handled', finished_at=? WHERE event_id=?",
                    (int(time.time()), msg.event_id),
                )
                return

            if await self._handle_feishu_command(msg, channel):
                self._storage.execute(
                    "UPDATE processed_events SET status='handled', finished_at=? WHERE event_id=?",
                    (int(time.time()), msg.event_id),
                )
                return

            # Handle special command: /safe (not covered by handle_bypass_command)
            if msg.text.strip() == "/safe":
                self._session_mgr.set_sandbox_mode(
                    msg.chat_id, agent_id, "safe", channel_name=channel_name
                )
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

            # Workspace (Phase 9 P0.1: moved before _handle_task_state_command for /compact)
            workspace_dir = self._workspace_manager.get_workspace(msg.chat_id, agent_id)

            # Handle TaskState slash commands: /pin, /goal, /tasks, /compact
            if await self._handle_task_state_command(msg, agent_id, channel, workspace_dir):
                # Mark event as handled before returning
                self._storage.execute(
                    "UPDATE processed_events SET status='handled', finished_at=? WHERE event_id=?",
                    (int(time.time()), msg.event_id),
                )
                return

            # Determine effective sandbox_mode (auto-reverts expired TTL bypass to "safe")
            sandbox_mode = self._resolve_sandbox_mode(
                msg.chat_id, agent_id, channel_name=channel_name
            )

            if await self._handle_workflow_command(
                msg,
                agent_cfg,
                agent_id,
                workspace_dir,
                sandbox_mode,
                channel,
                channel_name,
            ):
                self._storage.execute(
                    "UPDATE processed_events SET status='handled', finished_at=? WHERE event_id=?",
                    (int(time.time()), msg.event_id),
                )
                return

            # Phase 8 M2: /context commands
            if await self._handle_rag_command(
                msg,
                agent_cfg,
                agent_id,
                workspace_dir,
                sandbox_mode,
                channel,
                channel_name,
            ):
                self._storage.execute(
                    "UPDATE processed_events SET status='handled', finished_at=? WHERE event_id=?",
                    (int(time.time()), msg.event_id),
                )
                return

            # Phase 7: auto-detect whether this message should run as a workflow.
            if await self._maybe_auto_dispatch_workflow(
                msg,
                agent_cfg,
                agent_id,
                workspace_dir,
                sandbox_mode,
                channel,
                channel_name,
            ):
                self._storage.execute(
                    "UPDATE processed_events SET status='handled', finished_at=? WHERE event_id=?",
                    (int(time.time()), msg.event_id),
                )
                return

            # Persist the inbound user message before invoking the agent so it
            # counts toward auto-compaction and is visible to history readers.
            self._session_mgr.store_message(
                msg.chat_id,
                agent_id,
                "user",
                msg.text,
                channel_name=channel_name,
                workspace_dir=str(workspace_dir) if workspace_dir else None,
            )

            # Auto-compact when the active history exceeds the threshold so the
            # next request runs against a digested context window.
            total_messages = self._session_mgr.count_messages(
                msg.chat_id, agent_id, channel_name=channel_name
            )
            if total_messages > AUTO_COMPACT_THRESHOLD:
                compacted = self._session_mgr.compact_history(
                    msg.chat_id,
                    agent_id,
                    keep_recent=AUTO_COMPACT_KEEP_RECENT,
                    channel_name=channel_name,
                    workspace_dir=str(workspace_dir) if workspace_dir else None,
                )
                if compacted:
                    logger.info(
                        "Auto-compacted %d messages for chat=%s agent=%s",
                        compacted, msg.chat_id, agent_id,
                    )
                    self._maybe_extract_memory_from_compaction(
                        msg.chat_id, agent_id, channel_name, str(workspace_dir) if workspace_dir else None
                    )

            # Phase 9.7: Replaced delayed acknowledgment with intelligent prelude.
            # The prelude is generated by the LLM as part of the first tool call,
            # and sent before tool execution. See AgentContext.on_prelude.
            # Casual chats (no tool calls) naturally skip prelude.

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
            session_id = derive_session_id(
                channel_name=channel_name,
                chat_id=msg.chat_id,
                agent_id=agent_id,
                thread_id=None,  # TODO: extract from msg if threading supported
            )
            # Phase 10 §6: pull all runtime knobs from cfg.agent so we
            # don't hardcode mode/enabled/max_chars at every call site,
            # and resolve a default ReActPolicy so the standard router
            # flow really does enter the controlled ReAct path.
            agent_runtime = self._config.agent
            from mini_claw.agent.react_policy import resolve_react_policy

            default_react_policy = resolve_react_policy(
                config=agent_runtime.react,
            )

            ctx = AgentContext(
                chat_id=msg.chat_id,
                agent_id=agent_id,
                workspace_dir=workspace_dir,
                channel=channel,
                sandbox_mode=sandbox_mode,
                audit_logger=self._audit_logger,
                chain_detector=self._chain_detector,
                system_prompt=agent_cfg.system_prompt,
                skill_manager=self._skill_manager,
                storage=self._storage,
                rag_manager=self._rag_manager,
                session_id=session_id,
                channel_name=channel_name,
                chat_search_manager=self._chat_search_manager,
                # Phase 10 M10.1: bind ReActUserUpdate callback. The legacy
                # ``on_prelude`` path is deliberately NOT wired here so
                # message_kind='prelude' rows are no longer produced by the
                # main flow (only the legacy command helpers still use it).
                on_react_update=lambda update: self._send_react_user_update(
                    chat_id=msg.chat_id,
                    agent_id=agent_id,
                    channel=channel,
                    channel_name=channel_name,
                    workspace_dir=str(workspace_dir) if workspace_dir else "",
                    run_id=run_id,
                    update=update,
                ),
                react_user_updates_enabled=agent_runtime.react_user_updates.enabled,
                react_user_update_mode=agent_runtime.react_user_updates.mode,
                react_user_update_max_chars=agent_runtime.react_user_updates.max_update_chars,
                react_user_updates_sanitize_completion_claims=agent_runtime.react_user_updates.sanitize_completion_claims,
                react_user_updates_store_redacted_text=agent_runtime.react_user_updates.store_redacted_text,
                react_user_updates_send_failure_non_blocking=agent_runtime.react_user_updates.send_failure_non_blocking,
                on_progress=lambda text: self._send_progress(
                    chat_id=msg.chat_id,
                    agent_id=agent_id,
                    channel=channel,
                    channel_name=channel_name,
                    workspace_dir=workspace_dir,
                    run_id=run_id,
                    text=text,
                ),
                react_policy=default_react_policy,
                goal_anchor_enabled=agent_runtime.goal_anchor.enabled,
                goal_anchor_max_summary_chars=agent_runtime.goal_anchor.max_summary_chars,
                goal_anchor_mark_untrusted=agent_runtime.goal_anchor.mark_untrusted,
                goal_anchor_detect_policy=agent_runtime.goal_anchor.detect_policy_like_phrases,
                goal_anchor_inject_every_iteration=agent_runtime.goal_anchor.inject_every_iteration,
                goal_anchor_summarization_mode=agent_runtime.goal_anchor.summarization_mode,
            )

            # Load conversation history for context
            history = self._session_mgr.get_history(
                msg.chat_id, agent_id, channel_name=channel_name
            )
            messages = history + [{"role": "user", "content": msg.text}]

            run = AgentRun(
                id=run_id,
                chat_id=msg.chat_id,
                agent_id=agent_id,
                status=RunOutcome.DONE,
                messages=messages,
                allowed_tools=agent_cfg.tools,
                original_goal_raw=msg.text,
            )

            # Phase 10: persist the original goal on agent_runs row.
            try:
                self._storage.execute(
                    "UPDATE agent_runs SET original_goal_raw=? WHERE id=?",
                    (msg.text, run_id),
                )
            except Exception:
                pass

            # Update job to running
            self._storage.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                ("running", int(time.time()), job_id),
            )

            # ========== Execute agent loop with workspace lock ==========
            # Per-workspace lock ensures concurrent messages to same workspace are serialized
            await self._with_workspace_lock(
                agent_id, str(workspace_dir),
                self._execute_agent_run(
                    run,
                    agent_cfg,
                    ctx,
                    msg.chat_id,
                    agent_id,
                    run_id,
                    job_id,
                    msg.event_id,
                    channel_name=channel_name,
                )
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
        channel_name = "feishu"
        channel = self._get_outbound_channel(channel_name)
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
        channel_name = self._channel_name_for_run(run_row["id"])
        channel = self._get_outbound_channel(channel_name)
        if channel is None:
            logger.error(
                "Gateway has no channel %s attached; dropping approval %s",
                channel_name,
                approval_id,
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
            run_row["chat_id"], run_row["agent_id"], channel_name=channel_name
        )

        ctx = AgentContext(
            chat_id=run_row["chat_id"],
            agent_id=run_row["agent_id"],
            workspace_dir=workspace_dir,
            channel=channel,
            sandbox_mode=sandbox_mode,
            audit_logger=self._audit_logger,
            chain_detector=self._chain_detector,
            system_prompt=agent_cfg.system_prompt,
            skill_manager=self._skill_manager,
            storage=self._storage,
            rag_manager=self._rag_manager,
            session_id=derive_session_id(
                channel_name=channel_name,
                chat_id=run_row["chat_id"],
                agent_id=run_row["agent_id"],
            ),
            channel_name=channel_name,
            chat_search_manager=self._chat_search_manager,
            on_react_update=lambda update: self._send_react_user_update(
                chat_id=run_row["chat_id"],
                agent_id=run_row["agent_id"],
                channel=channel,
                channel_name=channel_name,
                workspace_dir=str(workspace_dir) if workspace_dir else "",
                run_id=run_row["id"],
                update=update,
            ),
            react_user_updates_enabled=self._config.agent.react_user_updates.enabled,
            react_user_update_mode=self._config.agent.react_user_updates.mode,
            react_user_update_max_chars=self._config.agent.react_user_updates.max_update_chars,
            react_user_updates_sanitize_completion_claims=self._config.agent.react_user_updates.sanitize_completion_claims,
            react_user_updates_store_redacted_text=self._config.agent.react_user_updates.store_redacted_text,
            react_user_updates_send_failure_non_blocking=self._config.agent.react_user_updates.send_failure_non_blocking,
            react_policy=__import__("mini_claw.agent.react_policy", fromlist=["resolve_react_policy"]).resolve_react_policy(
                config=self._config.agent.react,
            ),
            goal_anchor_enabled=self._config.agent.goal_anchor.enabled,
            goal_anchor_max_summary_chars=self._config.agent.goal_anchor.max_summary_chars,
            goal_anchor_mark_untrusted=self._config.agent.goal_anchor.mark_untrusted,
            goal_anchor_detect_policy=self._config.agent.goal_anchor.detect_policy_like_phrases,
            goal_anchor_inject_every_iteration=self._config.agent.goal_anchor.inject_every_iteration,
            goal_anchor_summarization_mode=self._config.agent.goal_anchor.summarization_mode,
        )

        history = self._session_mgr.get_history(
            run_row["chat_id"], run_row["agent_id"], channel_name=channel_name
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
                    provider=self._provider_manager.get_provider_for_agent(agent_cfg),
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

            # Phase B.4: Compute total_tokens
            prompt_tokens_resume = getattr(run, "prompt_tokens", 0) or 0
            completion_tokens_resume = getattr(run, "completion_tokens", 0) or 0
            total_tokens_resume = prompt_tokens_resume + completion_tokens_resume
            self._storage.execute(
                "UPDATE agent_runs SET status = ?, final_answer = ?, iterations = ?, "
                "total_tokens = ?, "
                "original_goal_summary=COALESCE(?, original_goal_summary), "
                "final_reflection_json=COALESCE(?, final_reflection_json), "
                "updated_at = ? WHERE id = ?",
                (
                    _status_value(run.status),
                    run.final_answer,
                    run.iterations,
                    total_tokens_resume,
                    getattr(run, "original_goal_summary", None),
                    getattr(run, "final_reflection_json", None),
                    int(time.time()),
                    run.id,
                ),
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
        workspace_dir: Any = None,
    ) -> bool:
        """Dispatch the TaskState slash commands.

        Supported commands:

        * ``/pin <fact>``     — append a key fact to the persisted TaskState.
        * ``/goal <text>``    — overwrite the task description / goal.
        * ``/tasks``          — render the current TaskState back to the user.
        * ``/compact``        — manually trigger history compaction.
        * ``/clear``          — clear active chat history without calling LLM.

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
            # Phase 9 P0.2: pass channel_name to TaskState
            channel_name = msg.channel_name if hasattr(msg, "channel_name") and msg.channel_name else "legacy"
            state = TaskState.load(self._storage, msg.chat_id, agent_id, channel_name)
            state.add_fact(argument)
            state.save(self._storage, msg.chat_id, agent_id, channel_name)
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
            # Phase 9 P0.2: pass channel_name to TaskState
            channel_name = msg.channel_name if hasattr(msg, "channel_name") and msg.channel_name else "legacy"
            state = TaskState.load(self._storage, msg.chat_id, agent_id, channel_name)
            state.task_description = argument
            state.save(self._storage, msg.chat_id, agent_id, channel_name)
            await channel.send(
                msg.chat_id,
                f"任务目标已更新：{argument}",
            )
            return True

        if head == "/tasks":
            # Phase 9 P0.2: pass channel_name to TaskState
            channel_name = msg.channel_name if hasattr(msg, "channel_name") and msg.channel_name else "legacy"
            state = TaskState.load(self._storage, msg.chat_id, agent_id, channel_name)
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

        if head == "/clear":
            if argument:
                await channel.send(msg.chat_id, "用法：`/clear`")
                return True
            channel_name = getattr(msg, "channel_name", "feishu")
            cleared = self._session_mgr.clear_history(
                msg.chat_id,
                agent_id,
                channel_name=channel_name,
            )
            await channel.send(
                msg.chat_id,
                f"已清理当前会话上下文（隐藏 {cleared} 条活跃历史消息）。RAG、长期 memory 和审计记录不会被删除。",
            )
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
                msg.chat_id,
                agent_id,
                keep_recent=keep_recent,
                channel_name=getattr(msg, "channel_name", "feishu"),
                workspace_dir=str(workspace_dir) if workspace_dir else None,
            )
            if compacted:
                await channel.send(
                    msg.chat_id,
                    f"已压缩 {compacted} 条历史消息（保留最近 {keep_recent} 条）。",
                )
                self._maybe_extract_memory_from_compaction(
                    msg.chat_id,
                    agent_id,
                    getattr(msg, "channel_name", "feishu"),
                    str(workspace_dir) if workspace_dir else None,
                )
            else:
                await channel.send(
                    msg.chat_id,
                    f"暂无可压缩的历史消息（保留窗口 {keep_recent}）。",
                )
            return True

        # Phase 10 M10.4: /run trace <run_id> | /run inspect <run_id>
        if head == "/run":
            return await self._handle_run_command(msg, channel, argument)

        return False

    async def _handle_run_command(
        self,
        msg: "InboundMessage",
        channel: "Channel",
        argument: str,
    ) -> bool:
        """Phase 10 M10.4: /run list | /run trace <id> | /run inspect <id>."""
        from mini_claw.agent.trace import build_run_trace, render_trace_text

        if not argument:
            await channel.send(
                msg.chat_id,
                "Usage: /run list | /run trace <run_id> | /run inspect <run_id>",
            )
            return True

        parts = argument.split(maxsplit=1)
        sub = parts[0].lower()
        target = parts[1].strip() if len(parts) > 1 else ""

        if sub == "list":
            rows = self._storage.fetchall(
                "SELECT id, status, iterations, created_at FROM agent_runs "
                "WHERE chat_id=? ORDER BY created_at DESC LIMIT 20",
                (msg.chat_id,),
            )
            if not rows:
                await channel.send(msg.chat_id, "(no recent runs)")
                return True
            lines = [f"Recent runs ({len(rows)}):"]
            for r in rows:
                lines.append(
                    f"- {r['id']} status={r['status']} iters={r.get('iterations', 0)}"
                )
            await channel.send(msg.chat_id, "\n".join(lines))
            return True

        if sub in {"trace", "inspect"}:
            if not target:
                await channel.send(msg.chat_id, f"Usage: /run {sub} <run_id>")
                return True
            trace = build_run_trace(self._storage, target, audit_logger=self._audit_logger)
            if trace is None:
                await channel.send(msg.chat_id, f"Run not found: {target}")
                return True
            await channel.send(msg.chat_id, render_trace_text(trace))
            return True

        await channel.send(msg.chat_id, f"Unknown /run subcommand: {sub}")
        return True

    # ------------------------------------------------------------------
    # Phase 8 M2: /context commands
    # ------------------------------------------------------------------

    async def _handle_rag_command(
        self,
        msg: InboundMessage,
        agent_cfg: AgentConfig,
        agent_id: str,
        workspace_dir: Any,
        sandbox_mode: str,
        channel: Channel,
        channel_name: str,
    ) -> bool:
        """Handle /context, /rag status, /memory slash commands. Returns True if handled."""
        text = (msg.text or "").strip()

        # Phase 8 M4.5: /rag status
        if text == "/rag" or text.startswith("/rag "):
            parts = text.split(maxsplit=1)
            sub = parts[1].strip().lower() if len(parts) > 1 else ""
            if sub == "" or sub == "status":
                if not self._config.rag.enabled:
                    await channel.send(
                        msg.chat_id,
                        "RAG is disabled. Set rag.enabled=true in config.yaml to use it.",
                    )
                    return True
                if self._rag_manager is None:
                    await channel.send(msg.chat_id, "RAG manager not initialized.")
                    return True
                await channel.send(msg.chat_id, self._rag_manager.status_text())
                return True
            await channel.send(
                msg.chat_id,
                f"Unknown /rag subcommand: {sub}. Try /rag status",
            )
            return True

        # Phase 8 M5: /memory subcommands
        if text == "/memory" or text.startswith("/memory "):
            return await self._handle_memory_command(
                msg, agent_id, channel, channel_name
            )

        # Phase 9 M9.1: /chat subcommands
        if text == "/chat" or text.startswith("/chat "):
            # Build a minimal AgentContext for scope filtering
            workspace_dir = self._workspace_manager.get_workspace(msg.chat_id, agent_id)
            session_id = derive_session_id(channel_name, msg.chat_id, agent_id)
            ctx = AgentContext(
                chat_id=msg.chat_id,
                agent_id=agent_id,
                workspace_dir=workspace_dir,
                channel=channel,
                session_id=session_id,
                channel_name=channel_name,
                chat_search_manager=self._chat_search_manager,
            )
            return await self._handle_chat_command(msg, agent_id, channel, ctx)

        # Phase 9 M9.3: /workspace memory alias — forwards list/search/inspect/
        # remember to /memory with --scope workspace baked in. Keeps existing
        # `remember` short-circuit (which goes through ``remember_workspace``
        # so workspace-typed metadata is set correctly).
        if text.startswith("/workspace memory "):
            remainder = text[len("/workspace memory "):].strip()
            if remainder.startswith("remember "):
                content = remainder[len("remember "):].strip()
                if not content:
                    await channel.send(msg.chat_id, "Usage: /workspace memory remember <text>")
                    return True

                from mini_claw.rag.memory.workspace import remember_workspace

                workspace_dir = self._workspace_manager.get_workspace(msg.chat_id, agent_id)
                if not workspace_dir:
                    await channel.send(msg.chat_id, "[ERROR] No workspace directory set for this session.")
                    return True

                session_id = derive_session_id(channel_name, msg.chat_id, agent_id)
                ctx_dict = {
                    "agent_id": agent_id,
                    "chat_id": msg.chat_id,
                    "channel_name": channel_name,
                    "workspace_dir": str(workspace_dir),
                    "session_id": session_id,
                }

                cand_id, approval_id, status = remember_workspace(
                    content,
                    memory_type="project_constraint",
                    ctx=ctx_dict,
                    rag_manager=self._rag_manager,
                )

                if status.startswith("rejected:"):
                    await channel.send(msg.chat_id, f"[ERROR] {status}")
                    return True

                if self._audit_logger:
                    self._audit_logger.log_security_event(
                        event_type="workspace_memory_created",
                        details={
                            "candidate_id": cand_id,
                            "approval_id": approval_id,
                            "scope": "workspace",
                            "memory_type": "project_constraint",
                        },
                        chat_id=msg.chat_id,
                        agent_id=agent_id,
                    )

                await channel.send(
                    msg.chat_id,
                    f"Workspace memory candidate created.\n"
                    f"  candidate_id: {cand_id}\n"
                    f"  approval_id : {approval_id}\n"
                    f"Reply `/memory approve {cand_id}` after reviewing.",
                )
                return True

            # Forward list/search/inspect/pin/unpin/archive to /memory with --scope workspace
            if remainder.startswith(("list", "search ", "inspect ", "pin ", "unpin ", "archive ")):
                # Splice "--scope workspace" into the rewritten command.
                head, _, tail = remainder.partition(" ")
                rewritten = (
                    f"/memory {head} --scope workspace"
                    + (f" {tail}" if tail else "")
                )
                msg.text = rewritten
                return await self._handle_memory_command(msg, agent_id, channel, channel_name)

            await channel.send(
                msg.chat_id,
                "Usage: /workspace memory <remember|list|search|inspect|pin|unpin|archive> ...\n"
                "All operations are scoped to the current workspace.",
            )
            return True

        if not (text == "/context" or text.startswith("/context ")):
            return False

        # When RAG disabled, the command exists but is gated.
        if not self._config.rag.enabled or not self._config.rag.namespaces.context_enabled:
            await channel.send(
                msg.chat_id,
                "RAG is disabled. To enable, set rag.enabled=true and "
                "rag.namespaces.context_enabled=true in config.yaml.",
            )
            return True

        if self._rag_manager is None:
            await channel.send(msg.chat_id, "RAG manager not initialized.")
            return True

        parts = text.split(maxsplit=2)
        if len(parts) == 1:
            await channel.send(
                msg.chat_id,
                "Usage: /context index <path> | /context search <query> | "
                "/context list | /context inspect <id> | /context use <id> | "
                "/context clear | /context archive <id> | /context delete <id> | "
                "/context reindex <id> | /context rebind <id> <new_path> | "
                "/context cleanup",
            )
            return True

        command = parts[1].lower()
        argument = parts[2].strip() if len(parts) > 2 else ""

        ctx_dict = {
            "agent_id": agent_id,
            "workspace_dir": workspace_dir,
            "sandbox_mode": sandbox_mode,
            "chat_id": msg.chat_id,
            "session_id": derive_session_id(channel_name, msg.chat_id, agent_id),
            "channel_name": channel_name,
        }

        if command == "index":
            if not argument:
                await channel.send(msg.chat_id, "Usage: /context index <path>")
                return True
            # Phase 9.7 M4: Command Prelude
            await self._send_command_prelude(
                msg, channel, channel_name, agent_id,
                f"好的，我先为 `{argument}` 建立上下文索引。",
            )
            item_id, error = self._rag_manager.index_context(argument, ctx=ctx_dict)
            if error and not item_id:
                await channel.send(msg.chat_id, f"[ERROR] {error}")
            else:
                if self._audit_logger:
                    self._audit_logger.log_security_event(
                        event_type="rag_index_completed",
                        details={"item_id": item_id, "path": argument, "source": "command"},
                        chat_id=msg.chat_id,
                        agent_id=agent_id,
                    )
                await channel.send(
                    msg.chat_id, f"Indexed {argument} (item_id={item_id})"
                )
            return True

        if command == "search":
            if not argument:
                await channel.send(msg.chat_id, "Usage: /context search <query>")
                return True
            results, error = self._rag_manager.search_context(argument, ctx=ctx_dict)
            if error:
                await channel.send(msg.chat_id, f"[ERROR] {error}")
                return True
            if not results:
                await channel.send(msg.chat_id, f"No results for: {argument}")
                return True

            # CI-4: Reduce audit query recording for /context search (limit PII exposure)
            # Use query hash instead of raw query to avoid logging sensitive search terms
            if self._audit_logger:
                import hashlib
                from mini_claw.permissions.policy import looks_like_exfil_query, get_exfil_query_keywords

                query_hash = hashlib.sha256(argument.encode("utf-8")).hexdigest()[:16]
                matched_keywords = get_exfil_query_keywords(argument)
                keyword_class = matched_keywords if matched_keywords else []

                self._audit_logger.log_security_event(
                    event_type="rag_search_performed",
                    details={
                        "query_hash": query_hash,
                        "query_length": len(argument),
                        "hit_count": len(results),
                        "keyword_class": keyword_class,
                        "scope": "context",
                    },
                    chat_id=msg.chat_id,
                    agent_id=agent_id,
                )

                # Log separate event for sensitive queries (exfil detection)
                if looks_like_exfil_query(argument):
                    self._audit_logger.log_security_event(
                        event_type="rag_search_sensitive_query",
                        details={
                            "query_hash": query_hash,
                            "query_length": len(argument),
                            "tool": "/context_search_command",
                            "keyword_class": keyword_class,
                            "scope": "context",
                        },
                        chat_id=msg.chat_id,
                        agent_id=agent_id,
                    )

            lines = [f"Found {len(results)} result(s):\n"]
            for i, r in enumerate(results, 1):
                lines.append(
                    f"[{i}] {r.source_path}:{r.start_line}-{r.end_line} "
                    f"(score={r.score:.3f})"
                )
                excerpt = r.content[:200].replace("\n", " ")
                lines.append(f"    {excerpt}...")
            await channel.send(msg.chat_id, "\n".join(lines))
            return True

        if command == "list":
            items = self._rag_manager.list_contexts(ctx=ctx_dict)
            if not items:
                await channel.send(msg.chat_id, "No indexed contexts.")
                return True
            lines = [f"Indexed contexts ({len(items)}):"]
            for item in items:
                lines.append(
                    f"- {item.item_id}: {item.title or item.source_path} "
                    f"[{item.status}, {item.source_type}]"
                )
            await channel.send(msg.chat_id, "\n".join(lines))
            return True

        if command == "inspect":
            if not argument:
                await channel.send(msg.chat_id, "Usage: /context inspect <id>")
                return True
            item, error = self._rag_manager.inspect_context(argument, ctx=ctx_dict)
            if error or not item:
                await channel.send(msg.chat_id, f"[ERROR] {error or 'not found'}")
                return True
            await channel.send(
                msg.chat_id,
                f"Context: {item.item_id}\n"
                f"Source: {item.source_path}\n"
                f"Type: {item.source_type}\n"
                f"Status: {item.status}\n"
                f"Sensitivity: {item.sensitivity_level}\n"
                f"Hash: {item.content_hash}\n"
                f"Active version: {item.active_version}",
            )
            return True

        # /context use <id> — set active context for current session
        if command == "use":
            if not argument:
                await channel.send(msg.chat_id, "Usage: /context use <id>")
                return True
            # argument may include trailing extra text; take only the first token as id
            context_id = argument.split(maxsplit=1)[0]
            ok, error = self._rag_manager.use_context(context_id, ctx=ctx_dict)
            if not ok:
                await channel.send(msg.chat_id, f"[ERROR] {error}")
                return True
            await channel.send(
                msg.chat_id,
                f"Active context set: {context_id}\n"
                "Subsequent /context search will boost results from this context.\n"
                "Tip: to ask a question about it, use /context search <query>.",
            )
            return True

        # /context archive <id> — mark item status='archived'
        if command == "archive":
            if not argument:
                await channel.send(msg.chat_id, "Usage: /context archive <id>")
                return True
            context_id = argument.split(maxsplit=1)[0]
            ok, error = self._rag_manager.archive_context(context_id, ctx=ctx_dict)
            if not ok:
                await channel.send(msg.chat_id, f"[ERROR] {error}")
                return True
            await channel.send(msg.chat_id, f"Archived: {context_id}")
            return True

        # /context delete <id> — 7-step atomic delete
        if command == "delete":
            if not argument:
                await channel.send(msg.chat_id, "Usage: /context delete <id>")
                return True
            context_id = argument.split(maxsplit=1)[0]
            ok, error = self._rag_manager.delete_context(context_id, ctx=ctx_dict)
            if not ok:
                await channel.send(msg.chat_id, f"[ERROR] {error}")
                return True
            await channel.send(msg.chat_id, f"Deleted: {context_id}")
            return True

        # /context reindex <id> — atomic version-bump reindex
        if command == "reindex":
            if not argument:
                await channel.send(msg.chat_id, "Usage: /context reindex <id>")
                return True
            context_id = argument.split(maxsplit=1)[0]
            # Phase 9.7 M4: Command Prelude
            await self._send_command_prelude(
                msg, channel, channel_name, agent_id,
                f"收到，我先重新索引 `{context_id}` 的内容。",
            )
            ok, error = self._rag_manager.reindex_context(context_id, ctx=ctx_dict)
            if not ok:
                await channel.send(msg.chat_id, f"[ERROR] {error}")
                return True
            await channel.send(msg.chat_id, f"Reindexed: {context_id}")
            return True

        # /context rebind <id> <new_path> — update source_path (hash-checked)
        if command == "rebind":
            tokens = argument.split(maxsplit=1) if argument else []
            if len(tokens) < 2:
                await channel.send(
                    msg.chat_id, "Usage: /context rebind <id> <new_path>"
                )
                return True
            context_id, new_path = tokens[0], tokens[1].strip()
            ok, error = self._rag_manager.rebind_context(
                context_id, new_path, ctx=ctx_dict
            )
            if not ok:
                await channel.send(msg.chat_id, f"[ERROR] {error}")
                return True
            await channel.send(msg.chat_id, f"Rebound {context_id} -> {new_path}")
            return True

        # /context cleanup — run one lifecycle pass
        if command == "cleanup":
            counts = self._rag_manager.cleanup_lifecycle()
            summary = ", ".join(f"{k}={v}" for k, v in counts.items()) or "no changes"
            await channel.send(msg.chat_id, f"Lifecycle cleanup: {summary}")
            return True

        # Unknown /context subcommand — fall through to fallback at end of function
        # (the /memory subcommands below also use the same `command` variable; we
        #  rely on the dispatcher in `_handle_memory_command` to route /memory
        #  commands separately. This branch only executes when text starts with
        #  /context, so any unmatched command is a /context subcommand.)
        if text.startswith("/context"):
            await channel.send(
                msg.chat_id,
                f"Unknown /context subcommand: {command}. "
                "Try: /context index|search|list|inspect|use|archive|delete|"
                "reindex|rebind|cleanup",
            )
            return True

        if command == "clear":
            # Phase mc-1: L3 ApprovalStore flow for /memory clear with --scope user/all, --hard-delete double confirmation
            # Parse: /memory clear --scope <type> [--confirm] [--hard-delete] [--approve <approval_id>]
            tokens = text.split()
            if "--scope" not in tokens:
                await channel.send(
                    msg.chat_id,
                    "Usage: /memory clear --scope user|workspace|session|agent|all [--confirm] [--hard-delete] [--approve <approval_id>]",
                )
                return True

            try:
                scope_idx = tokens.index("--scope")
                scope_type = tokens[scope_idx + 1] if scope_idx + 1 < len(tokens) else None
                if scope_type not in {"user", "workspace", "session", "agent", "all"}:
                    await channel.send(msg.chat_id, f"Invalid scope: {scope_type}")
                    return True
            except (ValueError, IndexError):
                await channel.send(msg.chat_id, "Usage: /memory clear --scope <type> [--confirm] [--hard-delete] [--approve <approval_id>]")
                return True

            confirm = "--confirm" in tokens
            hard_delete = "--hard-delete" in tokens

            # Parse --approve flag
            approval_token = None
            if "--approve" in tokens:
                try:
                    ai = tokens.index("--approve")
                    approval_token = tokens[ai + 1] if ai + 1 < len(tokens) else None
                except (ValueError, IndexError):
                    approval_token = None

            channel_name_for_approval = channel_name or "legacy"

            # Phase 9 M9.2: Check if hard_delete is allowed in config
            if hard_delete and not self._config.rag.memory_control.allow_hard_delete:
                await channel.send(
                    msg.chat_id,
                    "[ERROR] Hard delete is disabled in configuration. "
                    "Set rag.memory_control.allow_hard_delete=true to enable.",
                )
                return True

            # Phase mc-1: L3 approval required for user/all scope OR hard-delete
            requires_approval = scope_type in ("user", "all") or hard_delete

            if requires_approval and approval_token is None:
                # First generate preview to show what would be cleared
                preview, error = self._rag_manager.clear_memory_scope(
                    scope_type,
                    scope_id=None,
                    ctx=ctx_dict,
                    dry_run=True,
                    hard_delete=hard_delete,
                )
                if error:
                    await channel.send(msg.chat_id, f"[ERROR] {error}")
                    return True

                if not preview:
                    await channel.send(msg.chat_id, f"No active memories in scope={scope_type}")
                    return True

                # Determine approval type
                if hard_delete:
                    approval_type = "memory_clear_hard_delete"
                    approval_reason = f"Hard delete memories (scope={scope_type}, count={len(preview)})"
                elif scope_type in ("user", "all"):
                    approval_type = "memory_clear_scope"
                    approval_reason = f"Clear large-scope memories (scope={scope_type}, count={len(preview)})"
                else:
                    approval_type = "memory_clear"
                    approval_reason = f"Clear memories (scope={scope_type}, count={len(preview)})"

                # Create pending approval
                approval_id = self._permission_gate.create_pending(
                    run_id=msg.chat_id,
                    chat_id=msg.chat_id,
                    agent_id=agent_id,
                    tool_call={
                        "tool": "memory_clear",
                        "args": {
                            "scope_type": scope_type,
                            "hard_delete": hard_delete,
                            "count": len(preview),
                        },
                    },
                    ttl=3600,
                    approval_type=approval_type,
                    channel_name=channel_name_for_approval,
                )

                if self._audit_logger:
                    self._audit_logger.log_security_event(
                        event_type="memory_clear_approval_required",
                        details={
                            "approval_id": approval_id,
                            "scope_type": scope_type,
                            "count": len(preview),
                            "hard_delete": hard_delete,
                            "approval_type": approval_type,
                        },
                        chat_id=msg.chat_id,
                        agent_id=agent_id,
                    )

                # Show preview with approval instruction
                lines = [
                    f"[L3 APPROVAL REQUIRED] {approval_reason}",
                    f"approval_id : {approval_id}",
                    f"\nPreview: {len(preview)} memory/memories would be {'deleted' if hard_delete else 'archived'}:\n",
                ]
                for p in preview[:10]:
                    lines.append(f"- {p['memory_id']}: {p['type']} | {p['content_preview']}")
                if len(preview) > 10:
                    lines.append(f"... and {len(preview) - 10} more")
                lines.append("\nApprove via the approval UI, then re-run:")
                lines.append(f"  /memory clear --scope {scope_type} {'--hard-delete ' if hard_delete else ''}--confirm --approve {approval_id}")
                await channel.send(msg.chat_id, "\n".join(lines))
                return True

            # Verify approval if required
            if requires_approval and approval_token is not None:
                record = self._permission_gate._approval_store.get_pending(
                    approval_token, channel_name=channel_name_for_approval
                )
                if record is None:
                    await channel.send(
                        msg.chat_id,
                        f"[ERROR] approval {approval_token} not found or wrong channel",
                    )
                    return True

                # Accept memory_clear, memory_clear_scope, or memory_clear_hard_delete
                valid_types = ("memory_clear", "memory_clear_scope", "memory_clear_hard_delete")
                if record.get("approval_type") not in valid_types:
                    await channel.send(
                        msg.chat_id,
                        f"[ERROR] approval {approval_token} is not a memory clear approval (type={record.get('approval_type')})",
                    )
                    return True

                if record.get("status") != "approved":
                    await channel.send(
                        msg.chat_id,
                        f"[ERROR] approval {approval_token} status={record.get('status')}; "
                        "must be 'approved' before clearing.",
                    )
                    return True

                # Phase mc-1: Double confirmation for hard-delete
                if hard_delete and not confirm:
                    await channel.send(
                        msg.chat_id,
                        "[ERROR] Hard delete requires double confirmation.\n"
                        "You have approved the operation, but you must also add --confirm flag:\n"
                        f"  /memory clear --scope {scope_type} --hard-delete --confirm --approve {approval_token}",
                    )
                    return True

            # For non-approval-required operations, still show preview if no --confirm
            dry_run = not confirm
            if not requires_approval and dry_run:
                preview, error = self._rag_manager.clear_memory_scope(
                    scope_type,
                    scope_id=None,
                    ctx=ctx_dict,
                    dry_run=True,
                    hard_delete=hard_delete,
                )
                if error:
                    await channel.send(msg.chat_id, f"[ERROR] {error}")
                    return True

                if not preview:
                    await channel.send(msg.chat_id, f"No active memories in scope={scope_type}")
                    return True

                lines = [f"Preview: {len(preview)} memory/memories would be {'deleted' if hard_delete else 'archived'}:\n"]
                for p in preview[:10]:
                    lines.append(f"- {p['memory_id']}: {p['type']} | {p['content_preview']}")
                if len(preview) > 10:
                    lines.append(f"... and {len(preview) - 10} more")
                lines.append("\nAdd --confirm to execute.")
                await channel.send(msg.chat_id, "\n".join(lines))
                return True

            # Execute the clear operation
            preview, error = self._rag_manager.clear_memory_scope(
                scope_type,
                scope_id=None,
                ctx=ctx_dict,
                dry_run=False,
                hard_delete=hard_delete,
            )
            if error:
                await channel.send(msg.chat_id, f"[ERROR] {error}")
                return True

            # Audit the completed operation
            if self._audit_logger:
                self._audit_logger.log_security_event(
                    event_type="memory_cleared_scope",
                    details={
                        "scope_type": scope_type,
                        "count": len(preview),
                        "hard_delete": hard_delete,
                        "approval_id": approval_token,
                        "dry_run": False,
                    },
                    chat_id=msg.chat_id,
                    agent_id=agent_id,
                )

            action = "deleted" if hard_delete else "archived"
            await channel.send(msg.chat_id, f"{len(preview)} memory/memories {action} (scope={scope_type})")
            return True


        if command == "export":
            # /memory export --scope <type> [--full-content] [--approve <approval_id>]
            tokens = text.split()
            if "--scope" not in tokens:
                await channel.send(
                    msg.chat_id,
                    "Usage: /memory export --scope user|workspace|agent|all [--full-content]",
                )
                return True

            try:
                scope_idx = tokens.index("--scope")
                scope_type = tokens[scope_idx + 1] if scope_idx + 1 < len(tokens) else None
            except (ValueError, IndexError):
                await channel.send(msg.chat_id, "Usage: /memory export --scope <type>")
                return True

            full_content = "--full-content" in tokens
            approval_token: str | None = None
            if "--approve" in tokens:
                try:
                    ai = tokens.index("--approve")
                    approval_token = tokens[ai + 1] if ai + 1 < len(tokens) else None
                except (ValueError, IndexError):
                    approval_token = None

            channel_name_for_approval = channel_name or "legacy"

            # Phase 9 横切: ChainDetector evaluate before export.
            # 1) memory_export_after_sensitive_search: any preceding RAG/chat exfil search
            # mc-5: Large-scope exports (user/all) now require unconditional L3 approval
            #       via the approval flow below, not ChainDetector blocking.
            row_estimate = 0
            try:
                if scope_type == "agent":
                    row = self._storage.fetchone(
                        "SELECT COUNT(*) AS cnt FROM rag_items "
                        "WHERE namespace='memory' AND status='active' AND owner_agent_id = ?",
                        (agent_id,),
                    )
                    row_estimate = int(row["cnt"]) if row else 0
                elif scope_type == "workspace":
                    ws = (
                        self._workspace_manager.get_workspace(msg.chat_id, agent_id)
                        if self._workspace_manager
                        else None
                    )
                    row = self._storage.fetchone(
                        "SELECT COUNT(*) AS cnt FROM rag_items "
                        "WHERE namespace='memory' AND status='active' AND workspace_dir = ?",
                        (str(ws or ""),),
                    )
                    row_estimate = int(row["cnt"]) if row else 0
                elif scope_type in ("user", "all"):
                    row = self._storage.fetchone(
                        "SELECT COUNT(*) AS cnt FROM rag_items "
                        "WHERE namespace='memory' AND status='active' AND channel_name = ?",
                        (channel_name_for_approval,),
                    )
                    row_estimate = int(row["cnt"]) if row else 0
            except Exception:
                row_estimate = 0

            if self._chain_detector is not None:
                fake_call = {
                    "name": "memory_export",
                    "arguments": {
                        "scope": scope_type,
                        "full_content": full_content,
                        "row_estimate": row_estimate,
                    },
                }
                # Build a minimal ctx dict for ChainDetector (it reads
                # chat_id/agent_id/channel_name from dict OR attribute).
                detector_ctx = {
                    "chat_id": msg.chat_id,
                    "agent_id": agent_id,
                    "channel_name": channel_name_for_approval,
                }
                detector_decision = self._chain_detector.evaluate_before_tool(
                    fake_call,
                    type("R", (), {"written_scripts": {}, "dangerous_actions": {}})(),
                    detector_ctx,
                )
                if detector_decision and detector_decision.get("action") == "deny":
                    audit_event = detector_decision.get("audit_event") or {}
                    if self._audit_logger:
                        self._audit_logger.log_security_event(
                            event_type=audit_event.get(
                                "event_type", "memory_export_blocked"
                            ),
                            details=audit_event,
                            chat_id=msg.chat_id,
                            agent_id=agent_id,
                        )
                    await channel.send(
                        msg.chat_id,
                        f"[denied] {detector_decision.get('reason', 'memory export blocked')}",
                    )
                    return True

            # mc-5: Unconditional L3 approval for --scope user/all (regardless of --full-content).
            # Phase 9 M9.2: full_content export also requires L3 approval.
            requires_approval = full_content or scope_type in ("user", "all")

            if requires_approval:
                # Determine approval type based on what triggered the requirement
                if full_content:
                    approval_type = "memory_export_full"
                    approval_reason = "Full-content memory export"
                elif scope_type in ("user", "all"):
                    approval_type = "memory_export_scope"
                    approval_reason = f"Large-scope memory export (scope={scope_type})"
                else:
                    approval_type = "memory_export_full"
                    approval_reason = "Memory export"

                if approval_token:
                    # Verify approval is approved + matches type/channel
                    record = self._permission_gate._approval_store.get_pending(
                        approval_token,
                        channel_name=channel_name_for_approval,
                    )
                    if record is None:
                        await channel.send(
                            msg.chat_id,
                            f"[ERROR] approval {approval_token} not found or wrong channel",
                        )
                        return True
                    # Accept either memory_export_full or memory_export_scope
                    if record.get("approval_type") not in ("memory_export_full", "memory_export_scope"):
                        await channel.send(
                            msg.chat_id,
                            f"[ERROR] approval {approval_token} is not a memory export approval",
                        )
                        return True
                    if record.get("status") != "approved":
                        await channel.send(
                            msg.chat_id,
                            f"[ERROR] approval {approval_token} status={record.get('status')}; "
                            "must be 'approved' before export.",
                        )
                        return True
                    # au-4: Audit approval grant consumed (recovery chain completeness)
                    if self._audit_logger:
                        self._audit_logger.log_security_event(
                            event_type="memory_export_approval_granted",
                            details={
                                "approval_id": approval_token,
                                "scope": scope_type,
                                "approval_type": record.get("approval_type"),
                                "full_content": full_content,
                                "row_estimate": row_estimate,
                            },
                            chat_id=msg.chat_id,
                            agent_id=agent_id,
                        )
                else:
                    approval_id = self._permission_gate.create_pending(
                        run_id=msg.chat_id,
                        chat_id=msg.chat_id,
                        agent_id=agent_id,
                        tool_call={
                            "tool": "memory_export",
                            "args": {"scope": scope_type, "full_content": full_content},
                        },
                        ttl=3600,
                        approval_type=approval_type,
                        channel_name=channel_name_for_approval,
                    )
                    if self._audit_logger:
                        self._audit_logger.log_security_event(
                            event_type="memory_export_approval_required",
                            details={
                                "approval_id": approval_id,
                                "scope": scope_type,
                                "approval_type": approval_type,
                                "full_content": full_content,
                            },
                            chat_id=msg.chat_id,
                            agent_id=agent_id,
                        )
                    await channel.send(
                        msg.chat_id,
                        f"[L3 APPROVAL REQUIRED] {approval_reason}.\n"
                        f"approval_id : {approval_id}\n"
                        "Approve via the approval UI, then re-run:\n"
                        f"  /memory export --scope {scope_type}"
                        + (" --full-content" if full_content else "")
                        + f" --approve {approval_id}",
                    )
                    return True

            # Phase 9.7 M4: Command Prelude before export execution
            await self._send_command_prelude(
                msg, channel, channel_name, agent_id,
                f"收到，我先导出 `{scope_type}` 范围的记忆数据。",
            )

            export_data, error = self._rag_manager.export_memories(
                scope_type, scope_id=None, ctx=ctx_dict, full_content=full_content
            )
            if error:
                await channel.send(msg.chat_id, f"[ERROR] {error}")
                return True

            if not export_data:
                await channel.send(msg.chat_id, f"No memories found in scope={scope_type}")
                return True

            # Format as JSON for copy-paste
            import json
            json_str = json.dumps(export_data, indent=2, ensure_ascii=False)

            # Phase 9 M9.2: persist export to artifacts table and return artifact id.
            artifact_id = ""
            try:
                import uuid as _uuid
                artifact_id = f"export-{_uuid.uuid4().hex[:12]}"
                self._storage.execute(
                    "INSERT INTO artifacts (id, content, created_at) VALUES (?, ?, ?)",
                    (artifact_id, json_str, int(time.time())),
                )
                try:
                    self._storage._conn.commit()
                except Exception:
                    pass
            except Exception:
                artifact_id = ""

            display = json_str
            if len(display) > 4000:
                display = display[:4000] + "\n... [truncated, full export has " + str(len(export_data)) + " items]"

            # Phase 9 M9.2: audit export — event renamed to ``memory_exported``
            if self._audit_logger:
                self._audit_logger.log_security_event(
                    event_type="memory_exported",
                    details={
                        "scope": scope_type,
                        "count": len(export_data),
                        "full_content": full_content,
                        "approval_id": approval_token,
                        "redacted": not full_content,
                        "artifact_id": artifact_id,
                    },
                    chat_id=msg.chat_id,
                    agent_id=agent_id,
                )

            artifact_line = f"\nartifact_id: {artifact_id}" if artifact_id else ""
            await channel.send(
                msg.chat_id,
                f"Exported {len(export_data)} memory/memories (scope={scope_type}):"
                f"{artifact_line}\n```json\n{display}\n```",
            )
            return True

        if command == "candidates":
            # /memory candidates [--type X] [--older-than Nd]
            tokens = text.split()
            memory_type = None
            older_than = None

            if "--type" in tokens:
                try:
                    type_idx = tokens.index("--type")
                    memory_type = tokens[type_idx + 1] if type_idx + 1 < len(tokens) else None
                except (ValueError, IndexError):
                    pass

            if "--older-than" in tokens:
                try:
                    older_idx = tokens.index("--older-than")
                    older_str = tokens[older_idx + 1] if older_idx + 1 < len(tokens) else None
                    if older_str and older_str.endswith("d"):
                        older_than = int(older_str[:-1])
                except (ValueError, IndexError):
                    pass

            candidates = self._rag_manager.list_memory_candidates(
                memory_type=memory_type, older_than_days=older_than
            )

            # Phase 9 横切3: audit candidate listing
            if self._audit_logger:
                self._audit_logger.log_security_event(
                    event_type="memory_candidate_listed",
                    details={
                        "count": len(candidates),
                        "memory_type": memory_type,
                        "older_than_days": older_than,
                    },
                    chat_id=msg.chat_id,
                    agent_id=agent_id,
                )

            if not candidates:
                await channel.send(msg.chat_id, "No pending memory candidates found.")
                return True

            lines = [f"Found {len(candidates)} pending candidate(s):\n"]
            for c in candidates[:10]:
                lines.append(
                    f"- {c['candidate_id']}: [{c['type']}] {c['content_preview']}... (sensitivity={c['sensitivity']})"
                )
            if len(candidates) > 10:
                lines.append(f"... and {len(candidates) - 10} more")
            lines.append("\nUse /memory approve <id> or /memory approve-all to batch approve.")

            await channel.send(msg.chat_id, "\n".join(lines))
            return True

        if command == "approve-all":
            # Phase 9 mc-8: L3 approval flow for batch approve
            # /memory approve-all [--type X] [--confirm] [--approve <approval_id>]
            tokens = text.split()
            memory_type = None
            confirm = "--confirm" in tokens
            approval_token = None

            if "--type" in tokens:
                try:
                    type_idx = tokens.index("--type")
                    memory_type = tokens[type_idx + 1] if type_idx + 1 < len(tokens) else None
                except (ValueError, IndexError):
                    pass

            if "--approve" in tokens:
                try:
                    approve_idx = tokens.index("--approve")
                    approval_token = tokens[approve_idx + 1] if approve_idx + 1 < len(tokens) else None
                except (ValueError, IndexError):
                    pass

            # Phase mc-8: Preview (dry_run=True) to show what would be approved
            preview_result, error = self._rag_manager.approve_all_candidates(
                memory_type=memory_type, dry_run=True, ctx=ctx_dict
            )
            if error:
                await channel.send(msg.chat_id, f"[ERROR] {error}")
                return True

            if not preview_result:
                await channel.send(msg.chat_id, "No candidates to approve.")
                return True

            # Phase mc-8: If not confirmed, show preview and exit
            if not confirm:
                # Enhanced preview with sensitivity, memory_type, creation time
                import time as _t
                lines = [f"Preview: {len(preview_result)} candidate(s) would be approved:\n"]
                for i, cand in enumerate(preview_result[:10], 1):
                    # Format creation time
                    created_at = cand.get("created_at", 0)
                    delta = int(_t.time()) - created_at
                    if delta < 60:
                        time_str = f"{delta}s ago"
                    elif delta < 3600:
                        time_str = f"{delta // 60}m ago"
                    elif delta < 86400:
                        time_str = f"{delta // 3600}h ago"
                    else:
                        time_str = f"{delta // 86400}d ago"

                    lines.append(
                        f"  {i}. {cand['candidate_id']}: "
                        f"type={cand['memory_type']}, "
                        f"sensitivity={cand['sensitivity']}, "
                        f"created={time_str}"
                    )
                if len(preview_result) > 10:
                    lines.append(f"  ... and {len(preview_result) - 10} more")
                lines.append("\nAdd --confirm to execute.")
                await channel.send(msg.chat_id, "\n".join(lines))
                return True

            # Phase mc-8: L3 approval required for batch approve
            channel_name_for_approval = getattr(msg, "channel_name", None) or "legacy"
            if not approval_token:
                # Create pending approval
                import uuid as _uuid
                approval_id = f"appr-{_uuid.uuid4().hex[:12]}"
                self._permission_gate._approval_store.create_pending(
                    approval_id=approval_id,
                    run_id=getattr(ctx_dict, "run_id", "unknown"),
                    chat_id=msg.chat_id,
                    agent_id=agent_id,
                    tool_name="memory_approve_all",
                    tool_args={
                        "memory_type": memory_type,
                        "candidate_count": len(preview_result),
                    },
                    expires_at=int(time.time()) + 3600,
                    approval_type="memory_batch_approve",
                    channel_name=channel_name_for_approval,
                )
                if self._audit_logger:
                    self._audit_logger.log_security_event(
                        event_type="approval_requested",
                        details={
                            "approval_id": approval_id,
                            "approval_type": "memory_batch_approve",
                            "candidate_count": len(preview_result),
                            "memory_type": memory_type,
                        },
                        chat_id=msg.chat_id,
                        agent_id=agent_id,
                    )
                await channel.send(
                    msg.chat_id,
                    "[L3 APPROVAL REQUIRED] Batch approval will commit multiple memory candidates.\n"
                    f"approval_id : {approval_id}\n"
                    f"Candidates to approve: {len(preview_result)}\n"
                    "Approve via the approval UI, then re-run:\n"
                    f"  /memory approve-all"
                    + (f" --type {memory_type}" if memory_type else "")
                    + f" --confirm --approve {approval_id}",
                )
                return True

            # Verify approval
            record = self._permission_gate._approval_store.get_pending(
                approval_token, channel_name=channel_name_for_approval
            )
            if record is None:
                await channel.send(
                    msg.chat_id,
                    f"[ERROR] Invalid or expired approval_id: {approval_token}",
                )
                return True

            if record["status"] != "pending":
                await channel.send(
                    msg.chat_id,
                    f"[ERROR] Approval already {record['status']}: {approval_token}",
                )
                return True

            # Resolve approval
            self._permission_gate._approval_store.resolve_pending(
                approval_token, "approved", channel_name=channel_name_for_approval
            )

            # Execute batch approval (dry_run=False)
            result, error = self._rag_manager.approve_all_candidates(
                memory_type=memory_type, dry_run=False, ctx=ctx_dict
            )
            if error:
                await channel.send(msg.chat_id, f"[ERROR] {error}")
                return True

            # Audit
            if self._audit_logger:
                self._audit_logger.log_security_event(
                    event_type="memory_approved_batch",
                    details={
                        "count": len(result),
                        "type": memory_type,
                        "approval_id": approval_token,
                        "dry_run": False,
                        "confirm": True,
                        "candidate_ids": result[:10] if len(result) <= 10 else result[:10] + ["..."],
                    },
                    chat_id=msg.chat_id,
                    agent_id=agent_id,
                )
            await channel.send(msg.chat_id, f"Batch approved {len(result)} candidate(s).")
            return True

        if command == "reject-all":
            # Phase 9 M9.2 + mc-11: /memory reject-all [--type X] [--older-than Nd] [--confirm] [--approve <approval_id>]
            tokens = text.split()
            memory_type = None
            older_than = None
            confirm = "--confirm" in tokens
            approval_token = None

            if "--type" in tokens:
                try:
                    type_idx = tokens.index("--type")
                    memory_type = tokens[type_idx + 1] if type_idx + 1 < len(tokens) else None
                except (ValueError, IndexError):
                    pass

            if "--older-than" in tokens:
                try:
                    older_idx = tokens.index("--older-than")
                    older_str = tokens[older_idx + 1] if older_idx + 1 < len(tokens) else None
                    if older_str and older_str.endswith("d"):
                        older_than = int(older_str[:-1])
                except (ValueError, IndexError):
                    pass

            if "--approve" in tokens:
                try:
                    ai = tokens.index("--approve")
                    approval_token = tokens[ai + 1] if ai + 1 < len(tokens) else None
                except (ValueError, IndexError):
                    pass

            # Get candidates matching filters
            candidates = self._rag_manager.list_memory_candidates(
                memory_type=memory_type, older_than_days=older_than
            )
            if not candidates:
                await channel.send(msg.chat_id, "No candidates to reject.")
                return True

            # mc-11: Apply batch safety limit
            batch_limit = self._config.rag.memory_control.max_batch_reject
            if len(candidates) > batch_limit:
                await channel.send(
                    msg.chat_id,
                    f"[ERROR] Batch reject limited to {batch_limit} candidates. "
                    f"Found {len(candidates)} matching candidates. "
                    f"Please use more specific filters (--type or --older-than).",
                )
                return True

            if not confirm:
                await channel.send(
                    msg.chat_id,
                    f"Preview: {len(candidates)} candidate(s) would be rejected.\n"
                    f"Candidates: {', '.join(c['candidate_id'] for c in candidates[:5])}{'...' if len(candidates) > 5 else ''}\n"
                    "Add --confirm to execute.",
                )
                return True

            # mc-11: Require L3 approval for batch reject operations
            channel_name_for_approval = getattr(msg, "channel_name", None) or "legacy"
            if approval_token is None:
                # Create L3 approval
                import uuid as _uuid
                approval_id = f"appr-{_uuid.uuid4().hex[:12]}"
                self._permission_gate._approval_store.create_pending(
                    approval_id=approval_id,
                    run_id=getattr(ctx_dict, "run_id", "unknown"),
                    chat_id=msg.chat_id,
                    agent_id=agent_id,
                    tool_name="memory_reject_all",
                    tool_args={
                        "memory_type": memory_type,
                        "older_than_days": older_than,
                        "candidate_count": len(candidates),
                    },
                    expires_at=int(time.time()) + 3600,
                    approval_type="memory_batch_reject",
                    channel_name=channel_name_for_approval,
                )
                if self._audit_logger:
                    self._audit_logger.log_security_event(
                        event_type="approval_requested",
                        details={
                            "approval_id": approval_id,
                            "approval_type": "memory_batch_reject",
                            "candidate_count": len(candidates),
                            "memory_type": memory_type,
                            "older_than_days": older_than,
                        },
                        chat_id=msg.chat_id,
                        agent_id=agent_id,
                    )
                await channel.send(
                    msg.chat_id,
                    f"[L3 APPROVAL REQUIRED] Batch reject {len(candidates)} candidate(s).\n"
                    f"approval_id : {approval_id}\n"
                    "Approve via the approval UI, then re-run:\n"
                    f"  /memory reject-all --confirm"
                    + (f" --type {memory_type}" if memory_type else "")
                    + (f" --older-than {older_than}d" if older_than else "")
                    + f" --approve {approval_id}",
                )
                return True

            # Verify approval
            approval_record = self._permission_gate._approval_store.get_pending(
                approval_token, channel_name=channel_name_for_approval
            )
            if approval_record is None:
                await channel.send(
                    msg.chat_id,
                    f"[ERROR] approval {approval_token} not found or wrong channel",
                )
                return True
            if approval_record.get("approval_type") != "memory_batch_reject":
                await channel.send(
                    msg.chat_id,
                    f"[ERROR] approval {approval_token} is not memory_batch_reject",
                )
                return True
            if approval_record.get("status") != "approved":
                await channel.send(
                    msg.chat_id,
                    f"[ERROR] approval {approval_token} status={approval_record.get('status')}; "
                    "must be 'approved' before batch reject.",
                )
                return True

            # Resolve approval
            self._permission_gate._approval_store.resolve_pending(
                approval_token, "approved", channel_name=channel_name_for_approval
            )

            # Execute rejection
            rejected_count = 0
            rejected_ids = []
            rejection_reasons = []
            for c in candidates:
                cid = c["candidate_id"]
                if self._rag_manager.reject_memory(cid):
                    rejected_count += 1
                    rejected_ids.append(cid)
                    # Collect rejection reason from candidate if available
                    reason = f"batch_reject: type={memory_type or 'any'}, older_than={older_than or 'none'}"
                    rejection_reasons.append({"candidate_id": cid, "reason": reason})

            if self._audit_logger:
                self._audit_logger.log_security_event(
                    event_type="memory_rejected_batch",
                    details={
                        "count": rejected_count,
                        "type": memory_type,
                        "older_than_days": older_than,
                        "approval_id": approval_token,
                        "dry_run": False,
                        "confirm": True,
                        "candidate_ids": rejected_ids[:10] if len(rejected_ids) <= 10 else rejected_ids[:10] + ["..."],
                        "rejection_reasons": rejection_reasons[:5],  # Limit to first 5 for audit
                    },
                    chat_id=msg.chat_id,
                    agent_id=agent_id,
                )
            await channel.send(msg.chat_id, f"Batch rejected {rejected_count} candidate(s).")
            return True

        if command == "approve":
            # Phase mc-3: L3 ApprovalStore flow for /memory approve
            if not argument:
                await channel.send(msg.chat_id, "Usage: /memory approve <candidate_id>")
                return True

            candidate_id = argument.strip()

            # Fetch candidate to get the associated approval_id
            cand = self._storage.fetchone(
                "SELECT approval_id, status, created_from_channel FROM memory_candidates WHERE candidate_id = ?",
                (candidate_id,),
            )
            if cand is None:
                await channel.send(msg.chat_id, f"[ERROR] Candidate not found: {candidate_id}")
                return True

            if cand["status"] not in ("pending", "approved"):
                await channel.send(
                    msg.chat_id,
                    f"[ERROR] Candidate {candidate_id} cannot be approved (status={cand['status']})",
                )
                return True

            approval_id = cand.get("approval_id")
            if not approval_id:
                await channel.send(
                    msg.chat_id,
                    f"[ERROR] Candidate {candidate_id} has no associated approval_id",
                )
                return True

            # Verify approval exists and get its details
            channel_name_for_approval = cand.get("created_from_channel") or channel_name or "legacy"
            approval_record = self._permission_gate._approval_store.get_pending(
                approval_id,
                channel_name=channel_name_for_approval,
            )
            if approval_record is None:
                await channel.send(
                    msg.chat_id,
                    f"[ERROR] Approval {approval_id} not found or wrong channel",
                )
                return True

            if approval_record.get("approval_type") != "memory_write":
                await channel.send(
                    msg.chat_id,
                    f"[ERROR] Approval {approval_id} is not memory_write type",
                )
                return True

            # Check if approval is already resolved
            if approval_record.get("status") != "pending":
                # If already approved, allow the commit to proceed (idempotent)
                if approval_record.get("status") == "approved":
                    pass  # Continue to commit
                else:
                    await channel.send(
                        msg.chat_id,
                        f"[ERROR] Approval {approval_id} status={approval_record.get('status')}; "
                        "must be 'pending' or 'approved'.",
                    )
                    return True
            else:
                # Resolve the approval as approved
                resolved = self._permission_gate.resolve(
                    approval_id, "approved", channel_name=channel_name_for_approval
                )
                if resolved is None:
                    await channel.send(
                        msg.chat_id,
                        f"[ERROR] Failed to resolve approval {approval_id}",
                    )
                    return True

            # Now commit the candidate
            item_id, error = self._rag_manager.approve_memory(candidate_id)
            if not item_id:
                await channel.send(msg.chat_id, f"[ERROR] {error}")
                return True

            if self._audit_logger:
                self._audit_logger.log_security_event(
                    event_type="memory_write_completed",
                    details={
                        "candidate_id": candidate_id,
                        "item_id": item_id,
                        "approval_id": approval_id,
                    },
                    chat_id=msg.chat_id,
                    agent_id=agent_id,
                )
            await channel.send(
                msg.chat_id, f"Approved: candidate {candidate_id} → memory {item_id}"
            )
            return True

        if command == "maintenance":
            # /memory maintenance [run|status] [--scope agent|workspace|all]
            tokens = text.split()
            scope = "agent"
            sub = ""
            for tok in tokens[2:]:
                if tok in ("run", "status"):
                    sub = tok
                    break
            if "--scope" in tokens:
                try:
                    scope_idx = tokens.index("--scope")
                    scope = tokens[scope_idx + 1] if scope_idx + 1 < len(tokens) else "agent"
                except (ValueError, IndexError):
                    scope = "agent"

            if scope == "workspace":
                workspace_dir = self._workspace_manager.get_workspace(msg.chat_id, agent_id)
                if workspace_dir:
                    ctx_dict["workspace_dir"] = str(workspace_dir)

            # /memory maintenance status — show last suggestions + mode
            if sub == "status":
                pending = self._storage.fetchall(
                    "SELECT suggestion_type, COUNT(*) AS cnt FROM memory_maintenance_suggestions "
                    "WHERE status='pending' GROUP BY suggestion_type",
                ) or []
                last_run = self._storage.fetchone(
                    "SELECT MAX(created_at) AS last_at FROM memory_maintenance_suggestions",
                )
                last_at = (last_run["last_at"] if last_run else 0) or 0
                # Phase 9 M9.6: read actual algorithm mode from config
                mode = self._config.rag.memory_maintenance.mode
                if mode == "auto":
                    # Auto-detect based on available backends
                    mode = "text_only"
                    if (
                        getattr(self._rag_manager, "embedder", None) is not None
                        and getattr(self._rag_manager, "vector_backend", None) is not None
                        and getattr(self._rag_manager.vector_backend, "name", "none") != "none"
                    ):
                        mode = "hybrid"
                lines = [
                    f"maintenance mode      : {mode}",
                    f"last suggestion at    : {last_at or 'never'}",
                    "pending by type       :",
                ]
                if pending:
                    for r in pending:
                        lines.append(f"  - {r['suggestion_type']}: {r['cnt']}")
                else:
                    lines.append("  (none)")
                await channel.send(msg.chat_id, "\n".join(lines))
                return True

            # default / explicit "run"
            # Phase 9.7 M4: Command Prelude before maintenance run
            await self._send_command_prelude(
                msg, channel, channel_name, agent_id,
                f"收到，我先扫描 `{scope}` 范围的记忆维护建议。",
            )
            result = self._rag_manager.run_memory_maintenance(ctx=ctx_dict, scope=scope)

            if result.get("error"):
                await channel.send(msg.chat_id, f"[ERROR] {result['error']}")
                return True

            # Phase 9 M9.6: read actual algorithm mode from config
            mode = self._config.rag.memory_maintenance.mode
            if mode == "auto":
                # Auto-detect based on available backends
                mode = "text_only"
                if (
                    getattr(self._rag_manager, "embedder", None) is not None
                    and getattr(self._rag_manager, "vector_backend", None) is not None
                    and getattr(self._rag_manager.vector_backend, "name", "none") != "none"
                ):
                    mode = "hybrid"

            lines = [
                f"Memory maintenance scan (scope={scope}, mode={mode}, scanned={result['scanned_count']} items):\n"
            ]

            duplicates = result["duplicates"]
            if duplicates:
                lines.append(f"=== Duplicates ({len(duplicates)} groups) ===")
                for d in duplicates[:5]:
                    lines.append(
                        f"  - {d['representative_id']} ↔ {len(d['duplicate_ids'])} dupes "
                        f"(similarity={d['similarity']:.2f})"
                    )
                if len(duplicates) > 5:
                    lines.append(f"  ... and {len(duplicates) - 5} more groups")
            else:
                lines.append("Duplicates: none")

            conflicts = result["conflicts"]
            if conflicts:
                lines.append(f"\n=== Conflicts ({len(conflicts)} pairs) ===")
                for c in conflicts[:5]:
                    lines.append(
                        f"  - {c['item_id_a']} vs {c['item_id_b']} "
                        f"(topic_overlap={c['similarity']:.2f}, reason={c['reason']})"
                    )
                if len(conflicts) > 5:
                    lines.append(f"  ... and {len(conflicts) - 5} more pairs")
            else:
                lines.append("\nConflicts: none")

            stale = result["stale"]
            if stale:
                lines.append(f"\n=== Stale candidates ({len(stale)} items) ===")
                for s in stale[:5]:
                    lines.append(
                        f"  - {s['item_id']} (age={s['age_days']}d, access={s['access_count']})"
                    )
                if len(stale) > 5:
                    lines.append(f"  ... and {len(stale) - 5} more items")
            else:
                lines.append("\nStale candidates: none")

            lines.append(
                "\nNOTE: These are SUGGESTIONS ONLY. No memories were modified. "
                "Use /memory apply-suggestion <id> or /memory reject-suggestion <id> to act on them."
            )

            if self._audit_logger:
                self._audit_logger.log_security_event(
                    event_type="memory_maintenance_run",
                    details={
                        "scope": scope,
                        "mode": mode,
                        "scanned": result["scanned_count"],
                        "duplicates_count": len(duplicates),
                        "conflicts_count": len(conflicts),
                        "stale_count": len(stale),
                    },
                    chat_id=msg.chat_id,
                    agent_id=agent_id,
                )
                # Phase 9 M9.6: emit per-category audit so retrievers can drill in
                if duplicates:
                    self._audit_logger.log_security_event(
                        event_type="memory_dedupe_suggested",
                        details={"count": len(duplicates), "scope": scope, "mode": mode},
                        chat_id=msg.chat_id,
                        agent_id=agent_id,
                    )
                if conflicts:
                    self._audit_logger.log_security_event(
                        event_type="memory_conflict_detected",
                        details={"count": len(conflicts), "scope": scope, "mode": mode},
                        chat_id=msg.chat_id,
                        agent_id=agent_id,
                    )
                if stale:
                    self._audit_logger.log_security_event(
                        event_type="memory_cleanup_suggested",
                        details={"count": len(stale), "scope": scope, "mode": mode},
                        chat_id=msg.chat_id,
                        agent_id=agent_id,
                    )

            await channel.send(msg.chat_id, "\n".join(lines))
            return True

        # Phase 9 M9.6 helper sub-commands — narrow views over the same scan
        if command in ("dedupe", "conflicts", "cleanup"):
            tokens = text.split()
            scope = "agent"
            if "--scope" in tokens:
                try:
                    si = tokens.index("--scope")
                    scope = tokens[si + 1] if si + 1 < len(tokens) else "agent"
                except (ValueError, IndexError):
                    scope = "agent"
            if scope == "workspace":
                ws = self._workspace_manager.get_workspace(msg.chat_id, agent_id)
                if ws:
                    ctx_dict["workspace_dir"] = str(ws)
            result = self._rag_manager.run_memory_maintenance(ctx=ctx_dict, scope=scope)

            if command == "dedupe":
                items = result["duplicates"]
                event = "memory_dedupe_suggested"
                header = f"Duplicates: {len(items)} groups (scope={scope})"
                lines = [header]
                for d in items[:20]:
                    lines.append(
                        f"  - rep={d['representative_id']} dupes={d['duplicate_ids']} "
                        f"sim={d['similarity']:.2f}"
                    )
            elif command == "conflicts":
                items = result["conflicts"]
                event = "memory_conflict_detected"
                header = f"Conflicts: {len(items)} pairs (scope={scope})"
                lines = [header]
                for c in items[:20]:
                    lines.append(
                        f"  - {c['item_id_a']} vs {c['item_id_b']} "
                        f"sim={c['similarity']:.2f}"
                    )
            else:  # cleanup → stale
                items = result["stale"]
                event = "memory_cleanup_suggested"
                header = f"Cleanup candidates: {len(items)} stale items (scope={scope})"
                lines = [header]
                for s in items[:20]:
                    lines.append(
                        f"  - {s['item_id']} age={s['age_days']}d access={s['access_count']}"
                    )

            if items and self._audit_logger:
                self._audit_logger.log_security_event(
                    event_type=event,
                    details={"count": len(items), "scope": scope},
                    chat_id=msg.chat_id,
                    agent_id=agent_id,
                )
            if not items:
                lines.append("(none)")
            await channel.send(msg.chat_id, "\n".join(lines))
            return True

        if command in ("apply-suggestion", "reject-suggestion"):
            # /memory apply-suggestion <id> [--approve <approval_id>]
            # /memory reject-suggestion <id> [reason]
            if not argument:
                await channel.send(
                    msg.chat_id,
                    f"Usage: /memory {command} <suggestion_id>"
                    + (" [reason]" if command == "reject-suggestion" else " [--approve <approval_id>]"),
                )
                return True

            from mini_claw.rag.memory.maintenance import (
                apply_suggestion,
                reject_suggestion,
            )

            tokens = text.split()
            suggestion_id = (argument.strip().split() or [""])[0]
            reason = ""
            if command == "reject-suggestion" and len(tokens) > 2:
                # everything after the suggestion id
                if suggestion_id and suggestion_id in tokens:
                    si = tokens.index(suggestion_id)
                    reason = " ".join(tokens[si + 1:])

            channel_name_for_approval = channel_name or "legacy"

            if command == "apply-suggestion":
                # Phase 9 M9.6: require L3 ApprovalStore before mutating rag_items.
                approval_token = None
                if "--approve" in tokens:
                    try:
                        ai = tokens.index("--approve")
                        approval_token = tokens[ai + 1] if ai + 1 < len(tokens) else None
                    except (ValueError, IndexError):
                        approval_token = None

                if approval_token is None:
                    approval_id = self._permission_gate.create_pending(
                        run_id=msg.chat_id,
                        chat_id=msg.chat_id,
                        agent_id=agent_id,
                        tool_call={
                            "tool": "memory_apply_suggestion",
                            "args": {"suggestion_id": suggestion_id},
                        },
                        ttl=3600,
                        approval_type="memory_apply_suggestion",
                        channel_name=channel_name_for_approval,
                    )
                    await channel.send(
                        msg.chat_id,
                        "[L3 APPROVAL REQUIRED] Apply maintenance suggestion will modify rag_items.\n"
                        f"approval_id : {approval_id}\n"
                        "Approve via the approval UI, then re-run:\n"
                        f"  /memory apply-suggestion {suggestion_id} --approve {approval_id}",
                    )
                    return True

                # Verify approval
                record = self._permission_gate._approval_store.get_pending(
                    approval_token, channel_name=channel_name_for_approval
                )
                if record is None:
                    await channel.send(
                        msg.chat_id,
                        f"[ERROR] approval {approval_token} not found or wrong channel",
                    )
                    return True
                if record.get("approval_type") != "memory_apply_suggestion":
                    await channel.send(
                        msg.chat_id,
                        f"[ERROR] approval {approval_token} is not memory_apply_suggestion",
                    )
                    return True
                if record.get("status") != "approved":
                    await channel.send(
                        msg.chat_id,
                        f"[ERROR] approval {approval_token} status={record.get('status')}; "
                        "must be 'approved'.",
                    )
                    return True

                ok, err = apply_suggestion(
                    suggestion_id, self._storage, self._rag_manager
                )
                event_type = "memory_suggestion_applied"
                approval_used = approval_token
            else:
                ok, err = reject_suggestion(
                    suggestion_id, self._storage, reason=reason
                )
                event_type = "memory_suggestion_rejected"
                approval_used = None

            if not ok:
                await channel.send(msg.chat_id, f"[ERROR] {err}")
                return True

            if self._audit_logger:
                details: dict[str, Any] = {"suggestion_id": suggestion_id}
                if approval_used:
                    details["approval_id"] = approval_used
                if command == "reject-suggestion" and reason:
                    details["reason"] = reason[:200]
                self._audit_logger.log_security_event(
                    event_type=event_type,
                    details=details,
                    chat_id=msg.chat_id,
                    agent_id=agent_id,
                )

            verb = "Applied" if command == "apply-suggestion" else "Rejected"
            await channel.send(msg.chat_id, f"{verb} suggestion: {suggestion_id}")
            return True

        if command == "reject":
            if not argument:
                await channel.send(msg.chat_id, "Usage: /memory reject <candidate_id> [--approve <approval_id>]")
                return True

            # Parse tokens for approval flag
            tokens = text.split()
            approval_token = None
            if "--approve" in tokens:
                try:
                    ai = tokens.index("--approve")
                    approval_token = tokens[ai + 1] if ai + 1 < len(tokens) else None
                except (ValueError, IndexError):
                    approval_token = None

            channel_name_for_approval = channel_name or "legacy"

            # Phase mc-4: L3 ApprovalStore flow for /memory reject
            if approval_token is None:
                approval_id = self._permission_gate.create_pending(
                    run_id=msg.chat_id,
                    chat_id=msg.chat_id,
                    agent_id=agent_id,
                    tool_call={
                        "tool": "memory_reject",
                        "args": {"candidate_id": argument},
                    },
                    ttl=3600,
                    approval_type="memory_reject",
                    channel_name=channel_name_for_approval,
                )
                if self._audit_logger:
                    self._audit_logger.log_security_event(
                        event_type="memory_reject_approval_required",
                        details={
                            "approval_id": approval_id,
                            "candidate_id": argument,
                            "approval_type": "memory_reject",
                        },
                        chat_id=msg.chat_id,
                        agent_id=agent_id,
                    )
                await channel.send(
                    msg.chat_id,
                    "[L3 APPROVAL REQUIRED] Rejecting a memory candidate requires approval.\n"
                    f"approval_id : {approval_id}\n"
                    "Approve via the approval UI, then re-run:\n"
                    f"  /memory reject {argument} --approve {approval_id}",
                )
                return True

            # Verify approval
            record = self._permission_gate._approval_store.get_pending(
                approval_token, channel_name=channel_name_for_approval
            )
            if record is None:
                await channel.send(
                    msg.chat_id,
                    f"[ERROR] approval {approval_token} not found or wrong channel",
                )
                return True
            if record.get("approval_type") != "memory_reject":
                await channel.send(
                    msg.chat_id,
                    f"[ERROR] approval {approval_token} is not memory_reject",
                )
                return True
            if record.get("status") != "approved":
                await channel.send(
                    msg.chat_id,
                    f"[ERROR] approval {approval_token} status={record.get('status')}; "
                    "must be 'approved'.",
                )
                return True

            # Execute rejection with approved token
            ok = self._rag_manager.reject_memory(argument)
            if not ok:
                await channel.send(msg.chat_id, f"Candidate not found: {argument}")
                return True
            if self._audit_logger:
                self._audit_logger.log_security_event(
                    event_type="memory_write_rejected",
                    details={
                        "candidate_id": argument,
                        "reason": "user_rejected",
                        "approval_id": approval_token,
                    },
                    chat_id=msg.chat_id,
                    agent_id=agent_id,
                )
            await channel.send(msg.chat_id, f"Rejected candidate: {argument}")
            return True

        await channel.send(
            msg.chat_id, f"Unknown /memory subcommand: {command}"
        )
        return True

    async def _handle_memory_command(
        self,
        msg: InboundMessage,
        agent_id: str,
        channel: Channel,
        channel_name: str,
    ) -> bool:
        """Handle low-risk /memory commands.

        The larger memory approval/export/clear flows still need a cleanup pass,
        but this method keeps normal memory commands from falling through to an
        AttributeError and breaking channel callbacks.
        """
        text = msg.text.strip()
        parts = text.split(maxsplit=2)
        command = parts[1].lower() if len(parts) > 1 else ""
        argument = parts[2].strip() if len(parts) > 2 else ""

        if not self._config.rag.enabled or not self._config.rag.namespaces.memory_enabled:
            await channel.send(
                msg.chat_id,
                "Memory RAG is disabled. To enable, set rag.enabled=true and "
                "rag.namespaces.memory_enabled=true in config.yaml.",
            )
            return True
        if self._rag_manager is None:
            await channel.send(msg.chat_id, "RAG manager not initialized.")
            return True

        workspace_dir = (
            self._workspace_manager.get_workspace(msg.chat_id, agent_id)
            if self._workspace_manager
            else None
        )
        session_id = derive_session_id(channel_name, msg.chat_id, agent_id)
        ctx_dict = {
            "agent_id": agent_id,
            "chat_id": msg.chat_id,
            "channel_name": channel_name,
            "workspace_dir": str(workspace_dir) if workspace_dir else None,
            "session_id": session_id,
        }

        if command in {"", "help"}:
            await channel.send(
                msg.chat_id,
                "Usage: /memory remember <text> | /memory list | "
                "/memory search <query> | /memory inspect <id> | "
                "/memory candidates | /memory approve <cand_id> | "
                "/memory reject <cand_id>",
            )
            return True

        if command == "remember":
            if not argument:
                await channel.send(msg.chat_id, "Usage: /memory remember <text>")
                return True
            cand_id, approval_id, status = self._rag_manager.remember(
                argument,
                ctx=ctx_dict,
            )
            if status.startswith("rejected:"):
                await channel.send(msg.chat_id, f"[ERROR] {status}")
                return True
            await channel.send(
                msg.chat_id,
                f"Memory candidate created.\n"
                f"  candidate_id: {cand_id}\n"
                f"  approval_id : {approval_id}\n"
                f"  status      : {status}",
            )
            return True

        if command == "list":
            limit = 20
            tokens = text.split()
            if "--limit" in tokens:
                try:
                    limit = max(1, min(100, int(tokens[tokens.index("--limit") + 1])))
                except (ValueError, IndexError):
                    await channel.send(msg.chat_id, "Usage: /memory list [--limit N]")
                    return True
            memories = self._rag_manager.list_memories(ctx=ctx_dict, limit=limit)
            if not memories:
                await channel.send(msg.chat_id, "No active memories.")
                return True
            lines = [f"Active memories ({len(memories)}):"]
            for item in memories:
                title = item.title or item.source_type or "memory"
                lines.append(
                    f"- {item.item_id}: {title} "
                    f"[{item.source_type}, pinned={item.pinned}, confidence={item.confidence}]"
                )
            await channel.send(msg.chat_id, "\n".join(lines))
            return True

        if command == "search":
            if not argument:
                await channel.send(msg.chat_id, "Usage: /memory search <query> [--scope agent|workspace|user|all]")
                return True
            tokens = text.split()
            scope = "agent"
            if "--scope" in tokens:
                try:
                    scope = tokens[tokens.index("--scope") + 1]
                except IndexError:
                    await channel.send(msg.chat_id, "Usage: /memory search <query> [--scope agent|workspace|user|all]")
                    return True
                argument = " ".join(
                    t for i, t in enumerate(tokens[2:])
                    if t != "--scope" and (i == 0 or tokens[i + 1] != "--scope")
                ).strip()
            results, error = self._rag_manager.search_memory(
                argument,
                ctx=ctx_dict,
                scope=scope,
            )
            if error:
                await channel.send(msg.chat_id, f"[ERROR] {error}")
                return True
            if not results:
                await channel.send(msg.chat_id, f"No memory results for: {argument}")
                return True
            lines = [f"Found {len(results)} memory result(s):"]
            for i, r in enumerate(results, 1):
                excerpt = r.content[:180].replace("\n", " ")
                lines.append(f"[{i}] {r.item_id} score={r.score:.3f} {excerpt}...")
            await channel.send(msg.chat_id, "\n".join(lines))
            return True

        if command == "inspect":
            if not argument:
                await channel.send(msg.chat_id, "Usage: /memory inspect <id>")
                return True
            item, error = self._rag_manager.inspect_memory(argument, ctx=ctx_dict)
            if error or item is None:
                await channel.send(msg.chat_id, f"[ERROR] {error or 'memory not found'}")
                return True
            await channel.send(
                msg.chat_id,
                f"Memory: {item.item_id}\n"
                f"Type: {item.source_type}\n"
                f"Scope: {item.scope_type}:{item.scope_id}\n"
                f"Status: {item.status}\n"
                f"Pinned: {item.pinned}\n"
                f"Confidence: {item.confidence}",
            )
            return True

        if command == "candidates":
            candidates = self._rag_manager.list_memory_candidates()
            if not candidates:
                await channel.send(msg.chat_id, "No pending memory candidates found.")
                return True
            lines = [f"Pending memory candidates ({len(candidates)}):"]
            for c in candidates[:20]:
                lines.append(
                    f"- {c['candidate_id']}: [{c.get('type') or c.get('memory_type')}] "
                    f"{c.get('content_preview', '')}..."
                )
            await channel.send(msg.chat_id, "\n".join(lines))
            return True

        if command == "approve":
            if not argument:
                await channel.send(msg.chat_id, "Usage: /memory approve <cand_id>")
                return True
            candidate_id = argument.split(maxsplit=1)[0]
            item_id, error = self._rag_manager.approve_memory(candidate_id)
            if error or item_id is None:
                await channel.send(msg.chat_id, f"[ERROR] {error or 'approval failed'}")
                return True
            await channel.send(
                msg.chat_id, f"Memory approved and committed: {item_id}"
            )
            return True

        if command == "reject":
            if not argument:
                await channel.send(msg.chat_id, "Usage: /memory reject <cand_id>")
                return True
            candidate_id = argument.split(maxsplit=1)[0]
            ok = self._rag_manager.reject_memory(candidate_id)
            if not ok:
                await channel.send(msg.chat_id, "[ERROR] reject failed or candidate not found")
                return True
            await channel.send(msg.chat_id, f"Memory candidate {candidate_id} rejected")
            return True

        await channel.send(
            msg.chat_id,
            f"Unknown /memory subcommand: {command}. Try /memory help",
        )
        return True

    def _maybe_extract_memory_from_compaction(
        self, chat_id: str, agent_id: str, channel_name: str, workspace_dir: str | None = None
    ) -> None:
        """Phase 8 M5 + Phase 9 M9.4: surface compacted-message decisions as memory candidates.

        Phase 9: now passes workspace_dir for workspace-scoped candidate classification.

        Called after :meth:`SessionManager.compact_history` returns >0. Noop
        when memory namespace is disabled or no RagManager is configured.
        Failures are swallowed — memory extraction must never break the
        message-handling loop.
        """
        if (
            self._rag_manager is None
            or not self._config.rag.enabled
            or not self._config.rag.namespaces.memory_enabled
        ):
            return
        try:
            # Fetch messages compacted in the just-completed pass.
            # Phase 10 M10.1: process-only kinds (prelude / react_update) are
            # never extracted into Memory candidates — they are user-facing
            # process echoes, not facts.
            rows = self._storage.fetchall(
                "SELECT id, role, content FROM messages "
                "WHERE chat_id = ? AND agent_id = ? AND channel_name = ? "
                "AND COALESCE(compacted, 0) = 1 "
                "AND COALESCE(message_kind, 'normal') NOT IN ('prelude', 'react_update') "
                "ORDER BY created_at DESC LIMIT 50",
                (chat_id, agent_id, channel_name),
            )
            messages = [
                {"id": r["id"], "role": r["role"], "content": r["content"]}
                for r in rows
            ]
            n = self._rag_manager.submit_session_compaction_candidates(
                messages,
                chat_id=chat_id,
                agent_id=agent_id,
                channel=channel_name,
                workspace_dir=workspace_dir,
            )
            if n and self._audit_logger:
                self._audit_logger.log_security_event(
                    event_type="memory_candidate_created",
                    details={"source": "compaction", "count": n},
                    chat_id=chat_id,
                    agent_id=agent_id,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory candidate extraction failed: %s", exc)

    async def _handle_chat_command(
        self,
        msg: InboundMessage,
        agent_id: str,
        channel: Channel,
        ctx: AgentContext,
    ) -> bool:
        """Phase 9 M9.1: dispatch /chat search <query>|reindex|status commands.

        Returns True if command was handled (caller should mark event as handled).
        """
        text = msg.text.strip()
        if not text.startswith("/chat "):
            return False

        if self._chat_search_manager is None:
            await channel.send(
                msg.chat_id,
                "Chat search is disabled. Set chat_search.enabled=true in config.yaml.",
            )
            return True

        parts = text.split(maxsplit=3)
        if len(parts) == 1:
            await channel.send(
                msg.chat_id,
                "Usage: /chat search <query> [--scope session|agent|workspace|all] | "
                "/chat search reindex | /chat search status",
            )
            return True

        command = parts[1].lower()

        if command == "search":
            if len(parts) < 3:
                await channel.send(msg.chat_id, "Usage: /chat search <query> [--scope ...]")
                return True

            subcommand = parts[2].lower()

            # Handle /chat search reindex
            if subcommand == "reindex":
                # Phase 9 M9.1: full three-event audit (started/completed/failed)
                total_row = self._storage.fetchone(
                    "SELECT COUNT(*) AS cnt FROM messages WHERE content IS NOT NULL"
                )
                total_messages = int(total_row["cnt"]) if total_row else 0
                if self._audit_logger:
                    self._audit_logger.log_security_event(
                        event_type="chat_search_rebuild_started",
                        details={"total_messages": total_messages, "scope": "all", "source": "/chat search reindex"},
                        chat_id=msg.chat_id,
                        agent_id=agent_id,
                    )
                try:
                    result = self._chat_search_manager.rebuild_index()
                    if self._audit_logger:
                        self._audit_logger.log_security_event(
                            event_type="chat_search_rebuild_completed",
                            details={
                                "total": result["total"],
                                "indexed": result["indexed"],
                                "skipped": result.get("skipped", 0),
                                "duration_ms": result["duration_ms"],
                            },
                            chat_id=msg.chat_id,
                            agent_id=agent_id,
                        )

                    await channel.send(
                        msg.chat_id,
                        f"Chat search index rebuilt: {result['indexed']}/{result['total']} indexed, "
                        f"{result['skipped']} skipped, took {result['duration_ms']}ms",
                    )
                except Exception as e:
                    if self._audit_logger:
                        self._audit_logger.log_security_event(
                            event_type="chat_search_rebuild_failed",
                            details={"error": str(e)[:200], "partial_count": 0, "source": "/chat search reindex"},
                            chat_id=msg.chat_id,
                            agent_id=agent_id,
                        )
                    logger.exception("chat search reindex failed")
                    await channel.send(msg.chat_id, f"[ERROR] Reindex failed: {e}")

                return True

            # Handle /chat search status
            if subcommand == "status":
                try:
                    status = self._chat_search_manager.get_status()
                    fts_status = "available" if status["fts_available"] else "unavailable (using LIKE fallback)"

                    # Format last_rebuild_time
                    last_rebuild_str = "never"
                    if status.get("last_rebuild_time"):
                        import time as _t
                        last_rebuild_time = status["last_rebuild_time"]
                        delta = int(_t.time()) - last_rebuild_time
                        if delta < 60:
                            last_rebuild_str = f"{delta}s ago"
                        elif delta < 3600:
                            last_rebuild_str = f"{delta // 60}m ago"
                        elif delta < 86400:
                            last_rebuild_str = f"{delta // 3600}h ago"
                        else:
                            last_rebuild_str = f"{delta // 86400}d ago"

                    await channel.send(
                        msg.chat_id,
                        f"Chat search status:\n"
                        f"- FTS5: {fts_status}\n"
                        f"- Total messages: {status['total_messages']}\n"
                        f"- Indexed: {status['fts_count']}\n"
                        f"- Last rebuild: {last_rebuild_str}",
                    )
                except Exception as e:
                    logger.exception("chat search status failed")
                    await channel.send(msg.chat_id, f"[ERROR] Status check failed: {e}")

                return True

            # Parse query and optional --scope flag
            query_and_flags = parts[2]
            scope = "current_session"  # default
            query = query_and_flags

            if "--scope" in query_and_flags:
                tokens = query_and_flags.split()
                try:
                    scope_idx = tokens.index("--scope")
                    if scope_idx + 1 < len(tokens):
                        scope_val = tokens[scope_idx + 1]
                        # Map shorthand to full scope names
                        scope_map = {
                            "session": "current_session",
                            "agent": "current_agent",
                            "workspace": "workspace",
                            "all": "all_visible",
                        }
                        scope = scope_map.get(scope_val, scope_val)
                        query = " ".join(tokens[:scope_idx] + tokens[scope_idx + 2 :])
                except (ValueError, IndexError):
                    pass

            try:
                results = self._chat_search_manager.search(query, scope=scope, ctx=ctx, top_k=10)

                # Phase 9 横切3: audit chat search (NEVER record raw query — only hash)
                import hashlib
                from mini_claw.permissions.policy import looks_like_exfil_query, get_exfil_query_keywords
                query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]

                # Determine keyword_class based on query content
                matched_keywords = get_exfil_query_keywords(query)
                keyword_class = matched_keywords if matched_keywords else []

                if self._audit_logger:
                    self._audit_logger.log_security_event(
                        event_type="chat_search_performed",
                        details={
                            "query_hash": query_hash,
                            "query_length": len(query),
                            "scope": scope,
                            "hit_count": len(results),
                            "keyword_class": keyword_class,
                        },
                        chat_id=msg.chat_id,
                        agent_id=agent_id,
                    )
                # Phase 9 M9.1 验收第 12 条：显式 /chat search 也要写
                # chat_search_sensitive_query 当 query 命中 EXFIL 关键词。
                if looks_like_exfil_query(query):
                    if self._audit_logger:
                        self._audit_logger.log_security_event(
                            event_type="chat_search_sensitive_query",
                            details={
                                "query_hash": query_hash,
                                "query_length": len(query),
                                "scope": scope,
                                "tool": "/chat_search_command",
                                "keyword_class": keyword_class,
                            },
                            chat_id=msg.chat_id,
                            agent_id=agent_id,
                        )
                # Phase 9 横切: feed ChainDetector so subsequent write_file /
                # run_shell external trigger link E even when chat search is
                # invoked via the /chat command (not the agent tool).
                if self._chain_detector is not None:
                    try:
                        self._chain_detector._record_chat_search(
                            query, {"scope": scope}, ctx
                        )
                    except Exception:
                        pass

                if not results:
                    await channel.send(msg.chat_id, f"No messages found matching '{query}' (scope={scope})")
                    return True

                lines = [f"Found {len(results)} message(s) matching '{query}' (scope={scope}):\n"]
                for i, result in enumerate(results, 1):
                    role = result.get("role", "unknown")
                    content = (result.get("content") or "")[:150]
                    created_at = result.get("created_at", 0)
                    lines.append(f"{i}. [{role}] {content}... (ts={created_at})")

                await channel.send(msg.chat_id, "\n".join(lines))
            except ValueError as e:
                await channel.send(msg.chat_id, f"[ERROR] {e}")
            except Exception as e:
                logger.exception("chat search failed")
                await channel.send(msg.chat_id, f"[ERROR] Chat search failed: {e}")

            return True

        else:
            await channel.send(
                msg.chat_id,
                f"Unknown /chat subcommand: {command}. "
                "Use /chat search <query>|reindex|status.",
            )
            return True

    async def _handle_workflow_command(
        self,
        msg: InboundMessage,
        agent_cfg: AgentConfig,
        agent_id: str,
        workspace_dir: Any,
        sandbox_mode: str,
        channel: Channel,
        channel_name: str,
    ) -> bool:
        text = (msg.text or "").strip()
        if not (text == "/workflow" or text.startswith("/workflow ")):
            return False

        parts = text.split(maxsplit=2)
        if len(parts) == 1:
            await channel.send(
                msg.chat_id,
                "Usage: /workflow plan <task> | /workflow run <task> | "
                "/workflow approve <workflow_id> | /workflow reject <workflow_id> | "
                "/workflow status <workflow_id> | /workflow inspect <workflow_id>",
            )
            return True

        command = parts[1].lower()
        argument = parts[2].strip() if len(parts) > 2 else ""

        if command in {"plan", "run"}:
            if not self._config.workflow.enabled:
                await channel.send(msg.chat_id, "Workflow is disabled. Set workflow.enabled=true to use /workflow commands.")
                return True
            if not argument:
                await channel.send(msg.chat_id, f"Usage: /workflow {command} <task>")
                return True
            spec = self._workflow_planner.plan(argument)
            await self._dispatch_workflow_plan(
                spec=spec,
                user_task=argument,
                agent_cfg=agent_cfg,
                msg=msg,
                agent_id=agent_id,
                workspace_dir=workspace_dir,
                sandbox_mode=sandbox_mode,
                channel=channel,
                channel_name=channel_name,
                command=command,
                force_approval=False,
                source="command",
            )
            return True

        if command in {"approve", "reject"}:
            if not argument:
                await channel.send(msg.chat_id, f"Usage: /workflow {command} <workflow_id>")
                return True
            row = self._workflow_store.get_run(argument)
            if row is None:
                await channel.send(msg.chat_id, f"Workflow not found: {argument}")
                return True
            if row["status"] != "awaiting_approval":
                await channel.send(msg.chat_id, f"Workflow {argument} is not awaiting approval (status={row['status']}).")
                return True
            if command == "reject":
                approval_id = row.get("approval_id")
                approval_type = "workflow_plan"
                if approval_id:
                    approval_row = self._storage.fetchone(
                        "SELECT approval_type FROM pending_approvals WHERE id=?",
                        (approval_id,),
                    )
                    if approval_row and approval_row.get("approval_type"):
                        approval_type = approval_row["approval_type"]
                    self._permission_gate.resolve(approval_id, "rejected")
                self._workflow_store.mark_rejected(argument)
                if (
                    self._audit_logger is not None
                    and approval_type == "workflow_reviewer_override"
                ):
                    self._audit_logger.log_security_event(
                        event_type="workflow_reviewer_override_rejected",
                        details={"workflow_id": argument},
                        chat_id=msg.chat_id,
                        agent_id=agent_id,
                    )
                await channel.send(msg.chat_id, f"Workflow rejected: {argument}")
                return True

            approval_id = row.get("approval_id")
            approval_type = "workflow_plan"
            if approval_id:
                approval_row = self._storage.fetchone(
                    "SELECT approval_type FROM pending_approvals WHERE id=?",
                    (approval_id,),
                )
                if approval_row and approval_row.get("approval_type"):
                    approval_type = approval_row["approval_type"]
                resolved = self._permission_gate.resolve(approval_id, "approved")
                if resolved is None:
                    await channel.send(msg.chat_id, f"Workflow approval is not pending: {argument}")
                    return True
            self._workflow_store.mark_approved(argument)
            spec = self._workflow_store.get_spec(argument)
            if spec is None:
                await channel.send(msg.chat_id, f"Workflow spec missing: {argument}")
                return True
            if approval_type == "workflow_reviewer_override":
                await self._resume_workflow_after_reviewer(
                    argument, spec, agent_cfg, msg, workspace_dir, sandbox_mode, channel, channel_name
                )
                return True
            await self._run_workflow_now(argument, spec, agent_cfg, msg, workspace_dir, sandbox_mode, channel, channel_name)
            return True

        if command == "status":
            await channel.send(msg.chat_id, self._render_workflow_status(argument))
            return True

        if command == "inspect":
            # Phase 10 M10.4: ``/workflow inspect <id> --trace`` renders a
            # node-by-node ReAct trace pulling each node's agent_run_id from
            # workflow_nodes and resolving its react_steps + user_updates.
            tokens = argument.split()
            wf_id = tokens[0] if tokens else ""
            want_trace = "--trace" in tokens
            if want_trace:
                await channel.send(msg.chat_id, self._render_workflow_trace(wf_id))
            else:
                await channel.send(msg.chat_id, self._render_workflow_inspect(wf_id))
            return True

        await channel.send(msg.chat_id, f"Unknown workflow command: {command}")
        return True

    async def _dispatch_workflow_plan(
        self,
        *,
        spec: Any,
        user_task: str,
        agent_cfg: AgentConfig,
        msg: InboundMessage,
        agent_id: str,
        workspace_dir: Any,
        sandbox_mode: str,
        channel: Channel,
        channel_name: str,
        command: str,
        force_approval: bool,
        source: str,
    ) -> str:
        """Phase 7: shared plan→validate→store→compile→dispatch path.

        Used by both `/workflow plan|run` command branch and the auto-detect
        injection. ``force_approval=True`` short-circuits ``_workflow_requires_approval``
        (auto-detected workflows always require human approval). ``source`` is
        recorded in the audit event (``command`` or ``auto_detect``).

        Returns the rendered text sent to the user (also useful for tests).
        """
        workflow_id = str(uuid.uuid4())
        if (
            getattr(self._config.workflow, "prompt_review", None) is not None
            and self._config.workflow.prompt_review.enabled
        ):
            try:
                from mini_claw.workflow.reviewer_inject import inject_prompt_reviewer

                spec = inject_prompt_reviewer(
                    spec,
                    node_id=self._config.workflow.prompt_review.node_id,
                    timeout=self._config.workflow.prompt_review.timeout,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("prompt_reviewer injection skipped: %s", exc)
        available_tools = set(self._registry.list_tools())
        validate_workflow_spec(
            spec,
            available_tools=available_tools,
            max_nodes=self._config.workflow.max_nodes_per_workflow,
            max_parallel=self._config.workflow.max_parallel_nodes,
            allow_llm_generated_script=self._config.workflow.allow_llm_generated_script,
        )
        self._workflow_store.create_run(workflow_id, msg.chat_id, agent_id, spec, status="planning")
        # Phase 9 P0.2: pass channel_name
        channel_name = msg.channel_name if hasattr(msg, "channel_name") and msg.channel_name else "legacy"
        preview = self._compile_and_store_workflow_prompts(
            workflow_id, spec, user_task, msg.chat_id, agent_cfg, channel_name
        )

        audit_logger = getattr(self, "_audit_logger", None)
        if source == "auto_detect" and audit_logger is not None:
            audit_logger.log_security_event(
                event_type="workflow_auto_triggered",
                details={
                    "workflow_id": workflow_id,
                    "workflow_name": spec.name,
                    "text_len": len(user_task),
                    "source": source,
                },
                chat_id=msg.chat_id,
                agent_id=agent_id,
            )

        if command == "plan":
            text = self._render_workflow_plan(workflow_id, spec, preview)
            await channel.send(msg.chat_id, text)
            return text

        if force_approval or self._workflow_requires_approval(spec):
            approval_id = self._permission_gate.create_pending(
                run_id=workflow_id,
                chat_id=msg.chat_id,
                agent_id=agent_id,
                tool_call={"tool": "workflow_plan", "args": {"workflow_id": workflow_id, "name": spec.name}},
                ttl=3600,
                approval_type="workflow_plan",
            )
            self._workflow_store.update_run_status(
                workflow_id,
                "awaiting_approval",
                approval_id=approval_id,
                approval_reason="workflow requires approval before execution",
            )
            if audit_logger is not None:
                audit_logger.log_security_event(
                    event_type="workflow_approval_required",
                    details={
                        "workflow_id": workflow_id,
                        "approval_id": approval_id,
                        "workflow_name": spec.name,
                        "source": source,
                    },
                    chat_id=msg.chat_id,
                    agent_id=agent_id,
                )
            text = (
                self._render_workflow_plan(workflow_id, spec, preview)
                + f"\n\nApproval required. Run `/workflow approve {workflow_id}` or `/workflow reject {workflow_id}`."
            )
            if source == "auto_detect":
                text = (
                    f"Auto-detected as {spec.name} workflow. Original message recorded.\n\n"
                    + text
                )
            await channel.send(msg.chat_id, text)
            return text

        await self._run_workflow_now(workflow_id, spec, agent_cfg, msg, workspace_dir, sandbox_mode, channel, channel_name)
        return f"workflow {workflow_id} executed"

    async def _maybe_auto_dispatch_workflow(
        self,
        msg: InboundMessage,
        agent_cfg: AgentConfig,
        agent_id: str,
        workspace_dir: Any,
        sandbox_mode: str,
        channel: Channel,
        channel_name: str,
    ) -> bool:
        """Phase 7: detect whether a normal user message should become a workflow.

        Returns True iff the message has been dispatched as a workflow (caller
        must mark the event handled and stop normal processing). Returns False
        for slash-prefixed messages, when auto-detect is disabled, or when
        intent classification declines.
        """
        wf_cfg = self._config.workflow
        if not (wf_cfg.enabled and wf_cfg.auto_detect):
            return False
        text = (msg.text or "").strip()
        if not text or text.startswith("/"):
            return False

        provider = self._provider_manager.get_provider_for_agent(agent_cfg)
        try:
            decision = await self._workflow_planner.decide_auto_intent(text, provider)
        except Exception as exc:  # noqa: BLE001 — fallback never blocks the conversation
            logger.warning("auto-detect intent classifier crashed: %s", exc)
            return False
        if not decision.use_workflow:
            return False

        # Persist the user message before dispatching so history reflects original intent.
        self._session_mgr.store_message(
            msg.chat_id,
            agent_id,
            "user",
            msg.text,
            channel_name=channel_name,
            workspace_dir=str(workspace_dir) if workspace_dir else None,
        )

        try:
            spec = self._workflow_planner.plan(text, workflow_type=decision.workflow_type)
        except Exception as exc:  # noqa: BLE001 — disabled template etc; fall back to normal chat
            logger.warning("auto-detect plan() failed: %s", exc)
            return False

        await self._dispatch_workflow_plan(
            spec=spec,
            user_task=text,
            agent_cfg=agent_cfg,
            msg=msg,
            agent_id=agent_id,
            workspace_dir=workspace_dir,
            sandbox_mode=sandbox_mode,
            channel=channel,
            channel_name=channel_name,
            command="run",
            force_approval=True,
            source="auto_detect",
        )
        return True

    def _compile_and_store_workflow_prompts(
        self,
        workflow_id: str,
        spec: Any,
        user_task: str,
        chat_id: str,
        agent_cfg: AgentConfig,
        channel_name: str = "legacy",
    ) -> list[dict[str, Any]]:
        task_state = TaskState.load(self._storage, chat_id, agent_cfg.id, channel_name)
        preview = []
        for node in spec.nodes:
            deps = {}
            prompt = self._workflow_prompt_compiler.compile(
                spec, node, user_task, deps, task_state, agent_cfg
            )
            redacted_prompt = self._workflow_prompt_compiler.redact(prompt)
            self._workflow_store.save_prompt(workflow_id, node.id, redacted_prompt)
            prompt_hash = hashlib.sha256(
                f"{redacted_prompt.system_prompt}\n{redacted_prompt.user_prompt}".encode("utf-8")
            ).hexdigest()
            audit_logger = getattr(self, "_audit_logger", None)
            if audit_logger is not None:
                audit_logger.log_security_event(
                    event_type="workflow_node_prompt_compiled",
                    details={
                        "workflow_id": workflow_id,
                        "node_id": node.id,
                        "prompt_hash": prompt_hash,
                        "redacted": redacted_prompt.redacted,
                    },
                    chat_id=chat_id,
                    agent_id=agent_cfg.id,
                )
            preview.append(
                {
                    "node_id": node.id,
                    "role": node.agent_role,
                    "tools": redacted_prompt.allowed_tools,
                    "risk": node.risk_level,
                    "redacted": redacted_prompt.redacted,
                }
            )
        return preview

    def _workflow_requires_approval(self, spec: Any) -> bool:
        if self._config.workflow.require_approval or spec.requires_approval:
            return True
        for node in spec.nodes:
            if node.risk_level in {"medium", "high"}:
                return True
            if {"write_file", "run_shell", "apply_patch"} & set(node.tools):
                return True
        return False

    async def _run_workflow_now(
        self,
        workflow_id: str,
        spec: Any,
        agent_cfg: AgentConfig,
        msg: InboundMessage,
        workspace_dir: Any,
        sandbox_mode: str,
        channel: Channel,
        channel_name: str,
    ) -> None:
        ctx = AgentContext(
            chat_id=msg.chat_id,
            agent_id=agent_cfg.id,
            workspace_dir=workspace_dir,
            channel=channel,
            sandbox_mode=sandbox_mode,
            audit_logger=self._audit_logger,
            chain_detector=self._chain_detector,
            system_prompt=agent_cfg.system_prompt,
            skill_manager=self._skill_manager,
            storage=self._storage,
            rag_manager=self._rag_manager,
            session_id=derive_session_id(
                channel_name=channel_name,
                chat_id=msg.chat_id,
                agent_id=agent_cfg.id,
            ),
            channel_name=channel_name,
            chat_search_manager=self._chat_search_manager,
        )
        runner = WorkflowRunner(
            config=self._config.workflow,
            store=self._workflow_store,
            compiler=self._workflow_prompt_compiler,
            provider=self._provider_manager.get_provider_for_agent(agent_cfg),
            registry=self._registry,
            permission_gate=self._permission_gate,
            result_processor=self._result_processor,
            workspace_lock=self._with_workspace_lock,
        )
        results = await runner.run(workflow_id, spec, agent_cfg=agent_cfg, ctx=ctx)
        final_text = WorkflowMerger().render_text(spec, results)
        await channel.send(msg.chat_id, final_text)

    async def _resume_workflow_after_reviewer(
        self,
        workflow_id: str,
        spec: Any,
        agent_cfg: AgentConfig,
        msg: InboundMessage,
        workspace_dir: Any,
        sandbox_mode: str,
        channel: Channel,
        channel_name: str,
    ) -> None:
        """Phase 7: continue a workflow whose reviewer flagged issues but was approved.

        Re-uses :meth:`WorkflowRunner.resume` so already-done nodes (including
        the reviewer) are skipped — only pending merge/verify nodes execute.
        """
        ctx = AgentContext(
            chat_id=msg.chat_id,
            agent_id=agent_cfg.id,
            workspace_dir=workspace_dir,
            channel=channel,
            sandbox_mode=sandbox_mode,
            audit_logger=self._audit_logger,
            chain_detector=self._chain_detector,
            system_prompt=agent_cfg.system_prompt,
            skill_manager=self._skill_manager,
            storage=self._storage,
            rag_manager=self._rag_manager,
            session_id=derive_session_id(
                channel_name=channel_name,
                chat_id=msg.chat_id,
                agent_id=agent_cfg.id,
            ),
            channel_name=channel_name,
            chat_search_manager=self._chat_search_manager,
        )
        runner = WorkflowRunner(
            config=self._config.workflow,
            store=self._workflow_store,
            compiler=self._workflow_prompt_compiler,
            provider=self._provider_manager.get_provider_for_agent(agent_cfg),
            registry=self._registry,
            permission_gate=self._permission_gate,
            result_processor=self._result_processor,
            workspace_lock=self._with_workspace_lock,
        )
        if self._audit_logger is not None:
            self._audit_logger.log_security_event(
                event_type="workflow_reviewer_override_approved",
                details={"workflow_id": workflow_id},
                chat_id=msg.chat_id,
                agent_id=agent_cfg.id,
            )
        results = await runner.resume(workflow_id, spec, agent_cfg=agent_cfg, ctx=ctx)
        final_text = WorkflowMerger().render_text(spec, results)
        await channel.send(msg.chat_id, final_text)

    def _render_workflow_plan(self, workflow_id: str, spec: Any, preview: list[dict[str, Any]]) -> str:
        lines = [
            f"Workflow plan: {spec.name}",
            f"id: {workflow_id}",
            f"reason: {spec.reason}",
            f"max_parallel: {spec.max_parallel}",
            "nodes:",
        ]
        for node in spec.nodes:
            item = next((p for p in preview if p["node_id"] == node.id), {})
            deps = ", ".join(node.depends_on) or "-"
            tools = ", ".join(item.get("tools", [])) or "-"
            lines.append(f"- {node.id} ({node.agent_role}, risk={node.risk_level}, deps={deps}, tools={tools})")
        return "\n".join(lines)

    def _render_workflow_status(self, workflow_id: str) -> str:
        if not workflow_id:
            return "Usage: /workflow status <workflow_id>"
        row = self._workflow_store.get_run(workflow_id)
        if row is None:
            return f"Workflow not found: {workflow_id}"
        nodes = self._workflow_store.list_nodes(workflow_id)
        lines = [f"Workflow {workflow_id}: {row['status']}"]
        for node in nodes:
            lines.append(f"- {node['node_id']}: {node['status']}")
        return "\n".join(lines)

    def _render_workflow_inspect(self, workflow_id: str) -> str:
        if not workflow_id:
            return "Usage: /workflow inspect <workflow_id>"
        row = self._workflow_store.get_run(workflow_id)
        if row is None:
            return f"Workflow not found: {workflow_id}"
        nodes = self._workflow_store.list_nodes(workflow_id)
        prompts = self._workflow_store.list_prompts(workflow_id)
        payload = {
            "workflow_id": workflow_id,
            "status": row["status"],
            "spec": json.loads(row["spec_json"]),
            "nodes": nodes,
            "prompts": prompts,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _render_workflow_trace(self, workflow_id: str) -> str:
        """Phase 10 M10.4: render a workflow's per-node ReAct trace.

        Walks ``workflow_nodes``, picks each node's ``agent_run_id`` and
        renders that run via :func:`mini_claw.agent.trace.render_trace_text`.
        Nodes without an agent run (eg. merge nodes) appear as a header
        line only.
        """
        from mini_claw.agent.trace import build_run_trace, render_trace_text

        if not workflow_id:
            return "Usage: /workflow inspect <workflow_id> --trace"
        row = self._workflow_store.get_run(workflow_id)
        if row is None:
            return f"Workflow not found: {workflow_id}"
        nodes = self._workflow_store.list_nodes(workflow_id)

        lines = [f"Workflow {workflow_id}: {row['status']}", ""]
        for node in nodes:
            node_id = node.get("node_id")
            status = node.get("status") or "?"
            run_id = node.get("agent_run_id")
            lines.append(f"▸ Node {node_id} ({status})")
            if not run_id:
                lines.append("  (no agent run — merge/skipped node)")
                lines.append("")
                continue
            trace = build_run_trace(self._storage, run_id, audit_logger=getattr(self, "_audit_logger", None))
            if trace is None:
                lines.append(f"  (agent run {run_id} not found)")
            else:
                indented = "\n".join(
                    "  " + ln for ln in render_trace_text(trace).splitlines()
                )
                lines.append(indented)
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _resolve_agent(self, chat_id: str, channel_name: str = "feishu") -> AgentConfig:
        """Determine which agent config handles a given chat_id."""
        if self._agent_manager is not None:
            return self._agent_manager.resolve_for_chat(channel_name, chat_id)
        for agent_cfg in self._config.agents:
            if chat_id in agent_cfg.route_chat_ids:
                return agent_cfg
        return self._config.agents[0]

    def _channel_name_for_run(self, run_id: str) -> str:
        row = self._storage.fetchone(
            "SELECT channel_name FROM processed_events WHERE run_id=? ORDER BY started_at DESC LIMIT 1",
            (run_id,),
        )
        if row and row.get("channel_name"):
            return row["channel_name"]
        return "feishu"

    def _resolve_sandbox_mode(
        self, chat_id: str, agent_id: str, channel_name: str = "feishu"
    ) -> str:
        """Return the effective sandbox mode for ``(chat_id, agent_id)``.

        Centralizes the TTL-aware resolution so ``handle_message`` and
        ``handle_approval`` cannot drift apart — the previous direct
        ``get_sandbox_mode`` call in ``handle_approval`` skipped TTL semantics
        and could resume a run under a stale bypass.
        """
        return self._session_mgr.get_effective_sandbox_mode(
            chat_id, agent_id, channel_name=channel_name
        )
