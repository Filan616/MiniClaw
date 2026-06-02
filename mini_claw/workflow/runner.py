"""Workflow runner that executes subagent nodes through the existing AgentLoop."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from mini_claw.agent.context import AgentContext
from mini_claw.agent.loop import AgentRun, RunOutcome, run_agent_step
from mini_claw.agent.task_state import TaskState
from mini_claw.config import AgentConfig, WorkflowConfig
from mini_claw.providers.base import Provider
from mini_claw.tools.registry import ToolRegistry
from mini_claw.workflow.merger import WorkflowMerger
from mini_claw.workflow.prompt_compiler import SubAgentPromptCompiler
from mini_claw.workflow.scheduler import WorkflowScheduler, node_requires_write_lock
from mini_claw.workflow.spec import WorkflowNode, WorkflowNodeResult, WorkflowSpec
from mini_claw.workflow.store import WorkflowStore


WorkspaceLock = Callable[[str, str, Awaitable[Any]], Awaitable[Any]]


class WorkflowRunner:
    def __init__(
        self,
        *,
        config: WorkflowConfig,
        store: WorkflowStore,
        compiler: SubAgentPromptCompiler,
        provider: Provider,
        registry: ToolRegistry,
        permission_gate: Any,
        result_processor: Any,
        workspace_lock: WorkspaceLock | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._compiler = compiler
        self._provider = provider
        self._registry = registry
        self._permission_gate = permission_gate
        self._result_processor = result_processor
        self._workspace_lock = workspace_lock
        self._scheduler = WorkflowScheduler()
        self._merger = WorkflowMerger()

    async def run(
        self,
        workflow_id: str,
        spec: WorkflowSpec,
        *,
        agent_cfg: AgentConfig,
        ctx: AgentContext,
    ) -> dict[str, WorkflowNodeResult]:
        self._store.update_run_status(workflow_id, "running")
        if ctx.audit_logger:
            ctx.audit_logger.log_security_event(
                event_type="workflow_started",
                details={"workflow_id": workflow_id, "workflow_name": spec.name},
                chat_id=ctx.chat_id,
                agent_id=ctx.agent_id,
            )
        statuses = {node.id: "pending" for node in spec.nodes}
        results: dict[str, WorkflowNodeResult] = {}

        while any(status == "pending" for status in statuses.values()):
            ready = self._scheduler.ready_nodes(spec, statuses)
            if not ready:
                self._store.update_run_status(workflow_id, "failed", error="workflow has no ready nodes")
                raise RuntimeError("workflow has no ready nodes")

            read_batch, risky_batch = self._scheduler.split_batch(
                ready, min(spec.max_parallel, self._config.max_parallel_nodes)
            )
            batch = read_batch + risky_batch
            for node in batch:
                statuses[node.id] = "running"

            node_results = await asyncio.gather(
                *[
                    self._run_node_with_lock(workflow_id, spec, node, results, agent_cfg, ctx)
                    for node in batch
                ],
                return_exceptions=True,
            )

            for node, result in zip(batch, node_results):
                if isinstance(result, Exception):
                    statuses[node.id] = "failed"
                    node_result = WorkflowNodeResult(node_id=node.id, status="failed", error=str(result), summary=str(result))
                    self._store.update_node(workflow_id, node_result)
                    results[node.id] = node_result
                    self._store.update_run_status(workflow_id, "failed", error=str(result))
                    if ctx.audit_logger:
                        ctx.audit_logger.log_security_event(
                            event_type="workflow_failed",
                            details={"workflow_id": workflow_id, "node_id": node.id, "error": str(result)},
                            chat_id=ctx.chat_id,
                            agent_id=ctx.agent_id,
                        )
                    return results
                statuses[node.id] = result.status
                results[node.id] = result

        self._store.update_run_status(workflow_id, "done")
        if ctx.audit_logger:
            ctx.audit_logger.log_security_event(
                event_type="workflow_completed",
                details={"workflow_id": workflow_id, "workflow_name": spec.name},
                chat_id=ctx.chat_id,
                agent_id=ctx.agent_id,
            )
        return results

    async def _run_node_with_lock(
        self,
        workflow_id: str,
        spec: WorkflowSpec,
        node: WorkflowNode,
        current_results: dict[str, WorkflowNodeResult],
        agent_cfg: AgentConfig,
        ctx: AgentContext,
    ) -> WorkflowNodeResult:
        coro = self._run_node(workflow_id, spec, node, current_results, agent_cfg, ctx)
        if node_requires_write_lock(node) and self._workspace_lock is not None:
            return await self._workspace_lock(ctx.agent_id, str(ctx.workspace_dir), coro)
        return await coro

    async def _run_node(
        self,
        workflow_id: str,
        spec: WorkflowSpec,
        node: WorkflowNode,
        current_results: dict[str, WorkflowNodeResult],
        agent_cfg: AgentConfig,
        ctx: AgentContext,
    ) -> WorkflowNodeResult:
        if node.type == "merge" or node.agent_role == "summarizer":
            if ctx.audit_logger:
                ctx.audit_logger.log_security_event(
                    event_type="workflow_node_started",
                    details={"workflow_id": workflow_id, "node_id": node.id, "node_type": node.type},
                    chat_id=ctx.chat_id,
                    agent_id=ctx.agent_id,
                )
            dep_results = {dep: current_results[dep] for dep in node.depends_on if dep in current_results}
            merged = self._merger.merge(spec, dep_results)
            result = WorkflowNodeResult(node_id=node.id, status="done", summary=merged["final_summary"], artifacts=merged)
            self._store.update_node(workflow_id, result)
            if ctx.audit_logger:
                ctx.audit_logger.log_security_event(
                    event_type="workflow_node_finished",
                    details={"workflow_id": workflow_id, "node_id": node.id, "status": result.status},
                    chat_id=ctx.chat_id,
                    agent_id=ctx.agent_id,
                )
            return result

        dep_results = {dep: current_results[dep] for dep in node.depends_on if dep in current_results}
        task_state = TaskState.load(self._store.storage, ctx.chat_id, ctx.agent_id)
        prompt = self._compiler.compile(spec, node, spec.user_task, dep_results, task_state, agent_cfg)
        redacted_prompt = self._compiler.redact(prompt)
        self._store.save_prompt(workflow_id, node.id, redacted_prompt)
        if ctx.audit_logger:
            prompt_hash = hashlib.sha256(
                f"{redacted_prompt.system_prompt}\n{redacted_prompt.user_prompt}".encode("utf-8")
            ).hexdigest()
            ctx.audit_logger.log_security_event(
                event_type="workflow_node_prompt_compiled",
                details={
                    "workflow_id": workflow_id,
                    "node_id": node.id,
                    "prompt_hash": prompt_hash,
                    "redacted": redacted_prompt.redacted,
                },
                chat_id=ctx.chat_id,
                agent_id=ctx.agent_id,
            )

        run_id = str(uuid.uuid4())
        self._store.storage.execute(
            "INSERT INTO agent_runs (id, chat_id, agent_id, status, user_message, created_at, updated_at) "
            "VALUES (?, ?, ?, 'running', ?, ?, ?)",
            (run_id, ctx.chat_id, ctx.agent_id, prompt.user_prompt, int(time.time()), int(time.time())),
        )
        self._store.mark_node_running(workflow_id, node.id, run_id)
        if ctx.audit_logger:
            ctx.audit_logger.log_security_event(
                event_type="workflow_node_started",
                details={"workflow_id": workflow_id, "node_id": node.id, "node_type": node.type},
                chat_id=ctx.chat_id,
                agent_id=ctx.agent_id,
            )

        sub_ctx = AgentContext(
            chat_id=ctx.chat_id,
            agent_id=ctx.agent_id,
            workspace_dir=ctx.workspace_dir,
            channel=ctx.channel,
            timeout=node.timeout,
            sandbox_mode=ctx.sandbox_mode,
            audit_logger=ctx.audit_logger,
            chain_detector=ctx.chain_detector,
            system_prompt=prompt.system_prompt,
            skill_manager=ctx.skill_manager,
        )
        run = AgentRun(
            id=run_id,
            chat_id=ctx.chat_id,
            agent_id=ctx.agent_id,
            status=RunOutcome.DONE,
            messages=[{"role": "user", "content": prompt.user_prompt}],
            allowed_tools=prompt.allowed_tools,
        )

        try:
            run = await run_agent_step(
                run=run,
                provider=self._provider,
                registry=self._registry,
                permission_gate=self._permission_gate,
                result_processor=self._result_processor,
                ctx=sub_ctx,
            )
        except Exception as exc:
            result = WorkflowNodeResult(node_id=node.id, status="failed", error=str(exc), agent_run_id=run_id)
            self._store.update_node(workflow_id, result)
            return result

        artifacts = self._parse_artifacts(run.final_answer)
        result = WorkflowNodeResult(
            node_id=node.id,
            status="done" if run.status == RunOutcome.DONE else "failed",
            summary=self._summary_from_answer(run.final_answer),
            artifacts=artifacts,
            agent_run_id=run_id,
            error=None if run.status == RunOutcome.DONE else run.final_answer,
        )
        self._store.storage.execute(
            "UPDATE agent_runs SET status=?, final_answer=?, iterations=?, updated_at=? WHERE id=?",
            (run.status, run.final_answer, run.iterations, int(time.time()), run_id),
        )
        self._store.update_node(workflow_id, result)
        if ctx.audit_logger:
            ctx.audit_logger.log_security_event(
                event_type="workflow_node_finished",
                details={"workflow_id": workflow_id, "node_id": node.id, "status": result.status},
                chat_id=ctx.chat_id,
                agent_id=ctx.agent_id,
            )
        return result

    def _parse_artifacts(self, answer: str | None) -> dict[str, Any]:
        if not answer:
            return {}
        try:
            parsed = json.loads(answer)
        except json.JSONDecodeError:
            return {"raw": answer}
        return parsed if isinstance(parsed, dict) else {"raw": parsed}

    def _summary_from_answer(self, answer: str | None) -> str:
        if not answer:
            return ""
        artifacts = self._parse_artifacts(answer)
        summary = artifacts.get("summary") or artifacts.get("final_summary")
        if isinstance(summary, str):
            return summary
        return answer[:500]
