"""Scheduler manager: cron-based task execution via APScheduler."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

from mini_claw.channels.base import InboundMessage
from mini_claw.storage.db import Database

logger = logging.getLogger(__name__)


class SchedulerManager:
    """Manages scheduled tasks using APScheduler with SQLite job store.

    Each task is a cron expression that triggers a synthetic message
    routed through the gateway for agent processing.
    """

    def __init__(self, storage: Database, gateway: Any) -> None:
        self._storage = storage
        self._gateway = gateway
        self._scheduler: Any = None
        self._running = False

    def start(self) -> None:
        """Initialize and start the APScheduler instance."""
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.cron import CronTrigger
        except ImportError:
            logger.warning(
                "APScheduler not installed. Scheduler disabled. "
                "Install with: pip install apscheduler"
            )
            return

        self._scheduler = AsyncIOScheduler()

        # Load persisted tasks and register them
        tasks = self._storage.fetchall(
            "SELECT * FROM scheduled_tasks WHERE enabled = 1"
        )
        for task in tasks:
            self._register_job(task)

        self._scheduler.start()
        self._running = True
        logger.info("Scheduler started with %d active tasks", len(tasks))

    def stop(self) -> None:
        """Shutdown the scheduler gracefully."""
        if self._scheduler and self._running:
            self._scheduler.shutdown(wait=False)
            self._running = False
            logger.info("Scheduler stopped")

    def add_task(
        self,
        chat_id: str,
        agent_id: str,
        cron_expr: str,
        instruction: str,
    ) -> str:
        """Add a new scheduled task.

        Args:
            chat_id: The chat to send results to.
            agent_id: The agent that will process the task.
            cron_expr: Cron expression (5-field: min hour day month weekday).
            instruction: The instruction text to send as a synthetic message.

        Returns:
            The generated task ID.
        """
        task_id = str(uuid.uuid4())
        now = int(time.time())

        self._storage.execute(
            "INSERT INTO scheduled_tasks (id, chat_id, agent_id, cron, instruction, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, chat_id, agent_id, cron_expr, instruction, 1, now),
        )

        task = {
            "id": task_id,
            "chat_id": chat_id,
            "agent_id": agent_id,
            "cron": cron_expr,
            "instruction": instruction,
        }

        if self._scheduler and self._running:
            self._register_job(task)

        logger.info("Added scheduled task %s: %s", task_id, cron_expr)
        return task_id

    def remove_task(self, task_id: str) -> None:
        """Remove a scheduled task by ID."""
        self._storage.execute(
            "DELETE FROM scheduled_tasks WHERE id = ?", (task_id,)
        )

        if self._scheduler and self._running:
            try:
                self._scheduler.remove_job(task_id)
            except Exception:
                pass  # Job may not exist in scheduler

        logger.info("Removed scheduled task %s", task_id)

    def list_tasks(self, chat_id: Optional[str] = None) -> list[dict[str, Any]]:
        """List scheduled tasks, optionally filtered by chat_id."""
        if chat_id:
            return self._storage.fetchall(
                "SELECT * FROM scheduled_tasks WHERE chat_id = ?",
                (chat_id,),
            )
        return self._storage.fetchall("SELECT * FROM scheduled_tasks")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _register_job(self, task: dict[str, Any]) -> None:
        """Register a task as an APScheduler job."""
        from apscheduler.triggers.cron import CronTrigger

        parts = task["cron"].split()
        if len(parts) == 5:
            trigger = CronTrigger(
                minute=parts[0],
                hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=parts[4],
            )
        else:
            logger.warning("Invalid cron expression for task %s: %s", task["id"], task["cron"])
            return

        self._scheduler.add_job(
            self._execute_task,
            trigger=trigger,
            id=task["id"],
            args=[task["id"]],
            replace_existing=True,
        )

    async def _execute_task(self, task_id: str) -> None:
        """Callback invoked by APScheduler: creates a synthetic message and routes through gateway."""
        task = self._storage.fetchone(
            "SELECT * FROM scheduled_tasks WHERE id = ? AND enabled = 1",
            (task_id,),
        )
        if task is None:
            logger.warning("Scheduled task %s not found or disabled", task_id)
            return

        # Create a synthetic inbound message
        synthetic_msg = InboundMessage(
            chat_id=task["chat_id"],
            sender_id="scheduler",
            text=task["instruction"],
            event_id=f"sched-{task_id}-{int(time.time())}",
            timestamp=int(time.time()),
        )

        # Route through gateway (channel is set on the gateway during startup)
        logger.info("Executing scheduled task %s", task_id)
        await self._gateway.handle_message(synthetic_msg)
