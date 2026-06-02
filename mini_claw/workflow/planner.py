"""Workflow decision and template planning."""

from __future__ import annotations

from dataclasses import dataclass

from mini_claw.config import WorkflowConfig
from mini_claw.workflow.spec import WorkflowSpec, WorkflowSpecError
from mini_claw.workflow.templates import code_review_workflow, debug_fix_workflow, migration_workflow


@dataclass(slots=True)
class WorkflowDecision:
    use_workflow: bool
    reason: str
    workflow_type: str = "none"
    estimated_risk: str = "low"


class WorkflowPlanner:
    """MVP planner: manual command plus deterministic template selection."""

    def __init__(self, config: WorkflowConfig) -> None:
        self._config = config

    def should_use_workflow(self, user_text: str) -> WorkflowDecision:
        text = user_text.lower()
        if any(k in text for k in ["traceback", "报错", "bug", "失败", "failed", "error"]):
            return WorkflowDecision(True, "Task looks like a debug/fix workflow.", "debug_fix", "medium")
        if any(k in text for k in ["迁移", "重构", "refactor", "migration", "upgrade", "升级"]):
            return WorkflowDecision(True, "Task looks like a migration workflow.", "migration", "high")
        if any(k in text for k in ["全面", "完整", "审计", "review", "检查", "audit", "系统性"]):
            return WorkflowDecision(True, "Task looks like a broad review workflow.", "code_review", "medium")
        if len(user_text) > 500:
            return WorkflowDecision(True, "Task is long enough to benefit from workflow planning.", "code_review", "medium")
        return WorkflowDecision(False, "Task should use normal AgentLoop.", "none", "low")

    def plan(self, user_text: str, *, workflow_type: str | None = None) -> WorkflowSpec:
        chosen = workflow_type or self.should_use_workflow(user_text).workflow_type
        if chosen == "debug_fix" and self._config.templates.debug_fix.enabled:
            return debug_fix_workflow(user_text)
        if chosen == "migration" and self._config.templates.migration.enabled:
            return migration_workflow(user_text)
        if chosen in ("code_review", "none") and self._config.templates.code_review.enabled:
            return code_review_workflow(user_text)
        if self._config.allow_dynamic:
            raise WorkflowSpecError("dynamic workflow planning is not implemented in Phase 5 MVP")
        raise WorkflowSpecError(f"workflow template is disabled or unavailable: {chosen}")
