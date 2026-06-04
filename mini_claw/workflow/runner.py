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


_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}


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

        return await self._drive_loop(
            workflow_id, spec, agent_cfg, ctx, statuses, results
        )

    async def resume(
        self,
        workflow_id: str,
        spec: WorkflowSpec,
        *,
        agent_cfg: AgentConfig,
        ctx: AgentContext,
    ) -> dict[str, WorkflowNodeResult]:
        """Phase 7: continue executing a workflow whose reviewer override was approved.

        Re-hydrates statuses from the DB so already-done nodes are skipped, then
        drives the remaining pending nodes through the same loop as ``run()``.
        """
        self._store.update_run_status(workflow_id, "running")
        statuses: dict[str, str] = {}
        results: dict[str, WorkflowNodeResult] = {}
        for row in self._store.list_nodes(workflow_id):
            statuses[row["node_id"]] = row.get("status") or "pending"
            if row.get("result_json"):
                try:
                    raw = json.loads(row["result_json"])
                    results[row["node_id"]] = WorkflowNodeResult(
                        node_id=raw.get("node_id", row["node_id"]),
                        status=raw.get("status", row.get("status") or "done"),
                        summary=raw.get("summary", ""),
                        artifacts=raw.get("artifacts") or {},
                        agent_run_id=raw.get("agent_run_id"),
                        error=raw.get("error"),
                    )
                except (json.JSONDecodeError, TypeError):
                    pass
        # Ensure every spec node has a status entry.
        for node in spec.nodes:
            statuses.setdefault(node.id, "pending")
        return await self._drive_loop(
            workflow_id, spec, agent_cfg, ctx, statuses, results
        )

    async def _drive_loop(
        self,
        workflow_id: str,
        spec: WorkflowSpec,
        agent_cfg: AgentConfig,
        ctx: AgentContext,
        statuses: dict[str, str],
        results: dict[str, WorkflowNodeResult],
    ) -> dict[str, WorkflowNodeResult]:
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

            # Phase 7: after every batch, check whether a reviewer node in this
            # batch demands escalation. We intentionally check AFTER the batch
            # completes (not via cancellation) and BEFORE picking the next ready
            # set — merge nodes are guaranteed to still be pending here because
            # their depends_on includes the reviewer.
            if self._reviewer_blocking(workflow_id, spec, batch, results, statuses, ctx):
                return results

        self._store.update_run_status(workflow_id, "done")
        if ctx.audit_logger:
            ctx.audit_logger.log_security_event(
                event_type="workflow_completed",
                details={"workflow_id": workflow_id, "workflow_name": spec.name},
                chat_id=ctx.chat_id,
                agent_id=ctx.agent_id,
            )

        # Phase 8 M5: surface workflow findings as memory candidates.
        # Cheap noop when memory namespace is disabled.
        # Phase 9 WM-1: Pass workflow_intent for better memory context.
        rag_mgr = getattr(ctx, "rag_manager", None)
        if rag_mgr is not None:
            try:
                # Find the summarizer/merge node result (deterministic merger output)
                merged: dict[str, Any] = {}
                for node in spec.nodes:
                    if node.type == "merge" or node.agent_role == "summarizer":
                        node_result = results.get(node.id)
                        if node_result and isinstance(node_result.artifacts, dict):
                            merged = node_result.artifacts
                            break
                if merged:
                    # Extract workflow intent from spec (name + reason + user_task)
                    workflow_intent = f"{spec.name}: {spec.reason}"
                    if spec.user_task:
                        workflow_intent = f"{workflow_intent} (task: {spec.user_task})"

                    n = rag_mgr.submit_workflow_candidates(
                        merged,
                        workflow_id=workflow_id,
                        chat_id=ctx.chat_id,
                        agent_id=ctx.agent_id,
                        channel=getattr(ctx, "channel_name", None),
                        workspace_dir=str(ctx.workspace_dir) if ctx.workspace_dir else None,
                        workflow_intent=workflow_intent,
                    )
                    if n and ctx.audit_logger:
                        ctx.audit_logger.log_security_event(
                            event_type="memory_candidate_created",
                            details={
                                "source": "workflow",
                                "workflow_id": workflow_id,
                                "count": n,
                            },
                            chat_id=ctx.chat_id,
                            agent_id=ctx.agent_id,
                        )
            except Exception:
                # Memory extraction failures must never break workflow completion
                pass

        return results

    def _reviewer_blocking(
        self,
        workflow_id: str,
        spec: WorkflowSpec,
        batch: list[WorkflowNode],
        results: dict[str, WorkflowNodeResult],
        statuses: dict[str, str],
        ctx: AgentContext,
    ) -> bool:
        """If a reviewer node in this batch returned approved=False (or timed
        out), escalate the workflow to ``awaiting_approval`` and return True.

        Defensive: if a merge/dependent node accidentally landed in the same
        batch as a reviewer, we surface a loud RuntimeError — that would mean
        ``inject_prompt_reviewer`` failed to wire depends_on correctly.
        """
        review_cfg = getattr(self._config, "prompt_review", None)
        if review_cfg is None or not review_cfg.enabled:
            return False
        reviewers = [n for n in batch if n.agent_role == "prompt_reviewer"]
        if not reviewers:
            return False

        reviewer_ids = {n.id for n in reviewers}
        for node in batch:
            if node.agent_role == "prompt_reviewer":
                continue
            if reviewer_ids & set(node.depends_on):
                raise RuntimeError(
                    "scheduler co-batched reviewer with dependent node "
                    f"{node.id} — depends_on wiring is broken"
                )

        threshold_rank = _SEVERITY_RANK.get(review_cfg.severity_threshold, 2)
        for reviewer in reviewers:
            result = results.get(reviewer.id)
            if result is None:
                continue
            artifacts = result.artifacts or {}
            timed_out = bool(artifacts.get("timed_out"))
            approved = artifacts.get("approved")
            issues = artifacts.get("prompt_issues") or []
            blocking_issue = any(
                _SEVERITY_RANK.get(str(issue.get("severity", "low")).lower(), 0)
                >= threshold_rank
                for issue in issues
                if isinstance(issue, dict)
            )
            blocking = timed_out or approved is False or blocking_issue
            if not blocking:
                continue

            self._escalate_workflow_for_reviewer(
                workflow_id=workflow_id,
                spec=spec,
                reviewer=reviewer,
                issues=issues,
                timed_out=timed_out,
                statuses=statuses,
                ctx=ctx,
            )
            return True
        return False

    def _escalate_workflow_for_reviewer(
        self,
        *,
        workflow_id: str,
        spec: WorkflowSpec,
        reviewer: WorkflowNode,
        issues: list,
        timed_out: bool,
        statuses: dict[str, str],
        ctx: AgentContext,
    ) -> None:
        gate = getattr(self, "_permission_gate", None)
        approval_id = None
        if gate is not None:
            try:
                approval_id = gate.create_pending(
                    run_id=workflow_id,
                    chat_id=ctx.chat_id,
                    agent_id=ctx.agent_id,
                    tool_call={
                        "tool": "workflow_reviewer_override",
                        "args": {
                            "workflow_id": workflow_id,
                            "reviewer_node": reviewer.id,
                            "prompt_issues": issues,
                            "timed_out": timed_out,
                        },
                    },
                    ttl=3600,
                    approval_type="workflow_reviewer_override",
                )
            except Exception as exc:  # noqa: BLE001
                ctx_logger = getattr(ctx, "audit_logger", None)
                if ctx_logger:
                    ctx_logger.log_security_event(
                        event_type="workflow_reviewer_override_failed",
                        details={"workflow_id": workflow_id, "error": str(exc)},
                        chat_id=ctx.chat_id,
                        agent_id=ctx.agent_id,
                    )

        reason = (
            "prompt_reviewer LLM timeout"
            if timed_out
            else "prompt_reviewer flagged blocking issues"
        )
        self._store.update_run_status(
            workflow_id,
            "awaiting_approval",
            approval_id=approval_id,
            approval_reason=reason,
        )

        event_type = (
            "workflow_reviewer_timeout" if timed_out else "workflow_reviewer_rejected"
        )
        if ctx.audit_logger:
            ctx.audit_logger.log_security_event(
                event_type=event_type,
                details={
                    "workflow_id": workflow_id,
                    "reviewer_node": reviewer.id,
                    "approval_id": approval_id,
                    "issue_count": len(issues),
                    "issues": [
                        {
                            "node_id": str(i.get("node_id", "")),
                            "issue": str(i.get("issue", ""))[:200],
                            "severity": str(i.get("severity", "")),
                        }
                        for i in issues
                        if isinstance(i, dict)
                    ][:20],
                },
                chat_id=ctx.chat_id,
                agent_id=ctx.agent_id,
            )

        # Push human-readable summary to the channel so the user sees what to act on.
        channel = getattr(ctx, "channel", None)
        if channel is not None:
            lines = [
                f"⚠ Prompt reviewer flagged this workflow ({workflow_id}). "
                f"Reason: {reason}.",
            ]
            for issue in issues[:10]:
                if not isinstance(issue, dict):
                    continue
                lines.append(
                    f"- [{issue.get('severity', '?')}] {issue.get('node_id', '?')}: "
                    f"{str(issue.get('issue', ''))[:200]}"
                )
            lines.append(
                f"Run `/workflow approve {workflow_id}` to override, "
                f"or `/workflow reject {workflow_id}` to abort."
            )
            try:
                # Best-effort; channels expose async send().
                send_coro = channel.send(ctx.chat_id, "\n".join(lines))
                if asyncio.iscoroutine(send_coro):
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        loop = None
                    if loop is not None:
                        loop.create_task(send_coro)
                    else:
                        send_coro.close()
            except Exception:  # noqa: BLE001
                pass

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
        # Phase 9 P0.2: pass channel_name to TaskState
        channel_name = getattr(ctx, "channel_name", None) or "legacy"
        task_state = TaskState.load(self._store.storage, ctx.chat_id, ctx.agent_id, channel_name)
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
            storage=ctx.storage,
            rag_manager=ctx.rag_manager,
            session_id=ctx.session_id,
            channel_name=ctx.channel_name,
            chat_search_manager=getattr(ctx, "chat_search_manager", None),
        )
        run = AgentRun(
            id=run_id,
            chat_id=ctx.chat_id,
            agent_id=ctx.agent_id,
            status=RunOutcome.DONE,
            messages=[{"role": "user", "content": prompt.user_prompt}],
            allowed_tools=prompt.allowed_tools,
        )

        timed_out = False
        try:
            run_coro = run_agent_step(
                run=run,
                provider=self._provider,
                registry=self._registry,
                permission_gate=self._permission_gate,
                result_processor=self._result_processor,
                ctx=sub_ctx,
            )
            if node.agent_role == "prompt_reviewer":
                review_cfg = getattr(self._config, "prompt_review", None)
                timeout_s = float(getattr(review_cfg, "timeout", node.timeout) or node.timeout)
                run = await asyncio.wait_for(run_coro, timeout=timeout_s)
            else:
                run = await run_coro
        except asyncio.TimeoutError:
            timed_out = node.agent_role == "prompt_reviewer"
            if not timed_out:
                result = WorkflowNodeResult(
                    node_id=node.id,
                    status="failed",
                    error="timeout",
                    agent_run_id=run_id,
                )
                self._store.update_node(workflow_id, result)
                return result
        except Exception as exc:
            result = WorkflowNodeResult(node_id=node.id, status="failed", error=str(exc), agent_run_id=run_id)
            self._store.update_node(workflow_id, result)
            return result

        if timed_out:
            artifacts: dict[str, Any] = {
                "approved": False,
                "timed_out": True,
                "prompt_issues": [
                    {
                        "node_id": node.id,
                        "issue": "reviewer LLM timeout — manual approval required",
                        "severity": "high",
                    }
                ],
                "summary": "reviewer timed out",
            }
            result = WorkflowNodeResult(
                node_id=node.id,
                status="done",
                summary="reviewer timed out",
                artifacts=artifacts,
                agent_run_id=run_id,
            )
            self._store.update_node(workflow_id, result)
            return result

        artifacts = self._parse_artifacts(run.final_answer)
        # Phase 7: stash redacted compiled prompt so a downstream prompt_reviewer
        # node can ingest it through dependency_results. Only add when reviewer
        # injection is enabled and this is a body subagent (not summarizer).
        review_cfg = getattr(self._config, "prompt_review", None)
        if (
            review_cfg is not None
            and review_cfg.enabled
            and node.type == "subagent"
            and node.agent_role not in {"summarizer", "prompt_reviewer"}
        ):
            artifacts = dict(artifacts)
            artifacts["compiled_prompt"] = {
                "system_prompt": redacted_prompt.system_prompt,
                "user_prompt": redacted_prompt.user_prompt,
            }
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
