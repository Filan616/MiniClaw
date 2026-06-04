"""Workflow decision and template planning."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from mini_claw.config import WorkflowConfig
from mini_claw.providers.base import Provider
from mini_claw.workflow.spec import WorkflowSpec, WorkflowSpecError
from mini_claw.workflow.templates import code_review_workflow, debug_fix_workflow, migration_workflow


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class WorkflowDecision:
    use_workflow: bool
    reason: str
    workflow_type: str = "none"
    estimated_risk: str = "low"


_TEMPLATE_WHITELIST = {"code_review", "debug_fix", "migration", "none"}


_INTENT_SYSTEM_PROMPT = (
    "You are a workflow intent classifier for a developer assistant.\n"
    "Decide whether a user message describes a multi-step task that benefits from "
    "a structured workflow (parallel subagents + merge), or a normal one-shot conversation.\n"
    "Return JSON only, no markdown, no commentary. Schema:\n"
    "{\n"
    '  "use_workflow": boolean,\n'
    '  "template": "code_review" | "debug_fix" | "migration" | "none",\n'
    '  "reason": string\n'
    "}\n"
    "Rules:\n"
    "- Choose use_workflow=true only when the task spans multiple files/concerns "
    "or needs investigate-plan-implement-verify staging.\n"
    "- Pick template:\n"
    "  - debug_fix: stack traces, bug reports, broken tests.\n"
    "  - migration: refactors, large API changes, framework upgrades.\n"
    "  - code_review: broad audits, security/architecture/test review.\n"
    "  - none: simple Q&A, single edits, conversational replies.\n"
    "- When unsure, choose use_workflow=false.\n"
)


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

    async def classify_intent_llm(
        self,
        user_text: str,
        provider: Provider,
        *,
        timeout_s: float = 4.0,
    ) -> WorkflowDecision | None:
        """Phase 7: ask the LLM whether the message warrants a workflow.

        Returns None on any error (timeout, JSON parse failure, schema violation).
        Callers must fall back to ``use_workflow=False`` when None is returned.
        """
        messages = [
            {"role": "system", "content": _INTENT_SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ]
        try:
            response = await asyncio.wait_for(
                provider.chat(messages, tools=None, stream=False),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning("workflow intent classifier timed out after %.1fs", timeout_s)
            return None
        except Exception as exc:  # noqa: BLE001 — provider failures must not break inbound
            logger.warning("workflow intent classifier failed: %s", exc)
            return None

        text = getattr(response, "text", None) or ""
        text = text.strip()
        if text.startswith("```"):
            # Strip markdown fence if model ignores instructions
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].lstrip()
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            logger.warning("workflow intent classifier returned non-JSON: %r", text[:200])
            return None
        if not isinstance(parsed, dict):
            return None
        use = parsed.get("use_workflow")
        template = parsed.get("template")
        reason = parsed.get("reason") or "LLM classified as workflow"
        if not isinstance(use, bool) or not isinstance(template, str):
            return None
        if template not in _TEMPLATE_WHITELIST:
            return None
        if not use or template == "none":
            return WorkflowDecision(False, "LLM classified as normal conversation.", "none", "low")
        return WorkflowDecision(True, str(reason)[:200], template, "medium")

    async def decide_auto_intent(
        self, user_text: str, provider: Provider
    ) -> WorkflowDecision:
        """Phase 7: combined rule pre-filter + LLM fallback.

        - Keyword match → return immediately (no LLM call).
        - Length in [min_chars, max_chars] band → run LLM classifier.
        - Outside band or LLM fails → use_workflow=False.
        """
        rule_decision = self.should_use_workflow(user_text)
        if rule_decision.use_workflow:
            return rule_decision
        opts = self._config.auto_detect_options
        text_len = len(user_text)
        if text_len < opts.min_chars or text_len > opts.max_chars:
            return WorkflowDecision(False, "Text length outside auto-detect band.", "none", "low")
        timeout_s = max(0.5, opts.llm_timeout_ms / 1000.0)
        llm_decision = await self.classify_intent_llm(user_text, provider, timeout_s=timeout_s)
        if llm_decision is None:
            return WorkflowDecision(False, "LLM intent classifier unavailable.", "none", "low")
        return llm_decision

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
