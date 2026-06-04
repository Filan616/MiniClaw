"""Tests for Phase 7: prompt_reviewer node auto-injection."""

from __future__ import annotations

from pathlib import Path

import pytest

from mini_claw.config import AgentConfig, AppConfig, WorkflowConfig
from mini_claw.gateway.router import Gateway
from mini_claw.gateway.session import SessionManager
from mini_claw.permissions.approval_store import ApprovalStore
from mini_claw.permissions.gate import PermissionGate
from mini_claw.permissions.policy import PermissionPolicy
from mini_claw.providers.base import LLMResponse, Provider
from mini_claw.providers.manager import ProviderManager
from mini_claw.storage.db import Database
from mini_claw.tools.builtin import BUILTIN_TOOLS
from mini_claw.tools.registry import ToolRegistry
from mini_claw.workflow.planner import WorkflowPlanner
from mini_claw.workflow.prompt_compiler import (
    SubAgentPromptCompiler,
    redact_for_reviewer,
)
from mini_claw.workflow.reviewer_inject import inject_prompt_reviewer
from mini_claw.workflow.spec import WorkflowNodeResult, validate_workflow_spec
from mini_claw.workflow.store import WorkflowStore
from mini_claw.workflow.templates import code_review_workflow, debug_fix_workflow


# ----------- inject_prompt_reviewer unit tests -----------


def test_inject_adds_reviewer_node_with_correct_dependencies():
    spec = code_review_workflow("review project")
    original_node_ids = {n.id for n in spec.nodes}

    new_spec = inject_prompt_reviewer(spec, node_id="prompt_review", timeout=180)

    new_ids = [n.id for n in new_spec.nodes]
    assert "prompt_review" in new_ids
    assert len(new_spec.nodes) == len(spec.nodes) + 1

    reviewer = next(n for n in new_spec.nodes if n.id == "prompt_review")
    assert reviewer.agent_role == "prompt_reviewer"
    assert reviewer.tools == []
    # Reviewer depends on every body subagent (i.e. not summarizer/merge)
    body_ids = {n.id for n in spec.nodes if n.type == "subagent" and n.agent_role != "summarizer"}
    assert set(reviewer.depends_on) == body_ids
    # Merge node now also depends on reviewer
    merge = next(n for n in new_spec.nodes if n.id == "merge_findings")
    assert "prompt_review" in merge.depends_on
    assert original_node_ids.issubset(set(new_ids))


def test_injected_spec_passes_validate_workflow_spec():
    spec = debug_fix_workflow("fix the bug")
    new_spec = inject_prompt_reviewer(spec)
    # reviewer has tools=[], must not reject under available_tools
    validate_workflow_spec(
        new_spec,
        available_tools={"read_file", "write_file", "run_shell", "list_directory"},
        max_nodes=10,
        max_parallel=3,
    )


def test_inject_is_noop_when_already_injected():
    spec = code_review_workflow("audit")
    once = inject_prompt_reviewer(spec)
    twice = inject_prompt_reviewer(once)
    once_ids = [n.id for n in once.nodes]
    twice_ids = [n.id for n in twice.nodes]
    assert once_ids == twice_ids


def test_inject_does_not_mutate_original_spec():
    spec = code_review_workflow("audit")
    merge = next(n for n in spec.nodes if n.id == "merge_findings")
    original_deps = list(merge.depends_on)
    original_nodes_id_list = id(spec.nodes)

    inject_prompt_reviewer(spec)

    # Original list object & merge.depends_on are untouched
    assert id(spec.nodes) == original_nodes_id_list
    assert merge.depends_on == original_deps
    assert "prompt_review" not in merge.depends_on


# ----------- PromptCompiler reviewer-input formatting -----------


def test_redact_for_reviewer_strips_absolute_paths():
    s = (
        "Read /Users/alice/project/secrets.env and "
        "C:\\Users\\bob\\repo\\config.toml please."
    )
    redacted = redact_for_reviewer(s)
    assert "/Users/alice" not in redacted
    assert "C:\\Users\\bob" not in redacted
    assert "<workspace>/..." in redacted


def test_redact_for_reviewer_also_applies_secret_patterns():
    s = "Authorization: Bearer abc123 and api_key=mysecret"
    redacted = redact_for_reviewer(s)
    assert "abc123" not in redacted
    assert "mysecret" not in redacted
    assert "[REDACTED]" in redacted


def test_format_reviewer_inputs_includes_compiled_prompts_and_truncates():
    cfg = WorkflowConfig(max_prompt_chars=4000)
    compiler = SubAgentPromptCompiler(cfg)
    long_text = "X" * 5000
    deps = {
        "node_a": WorkflowNodeResult(
            node_id="node_a",
            status="done",
            artifacts={
                "compiled_prompt": {
                    "system_prompt": long_text,
                    "user_prompt": "/Users/test/secret.env please review",
                }
            },
        ),
        "node_b": WorkflowNodeResult(
            node_id="node_b",
            status="done",
            artifacts={
                "compiled_prompt": {
                    "system_prompt": "you are tester",
                    "user_prompt": "run tests",
                }
            },
        ),
    }
    rendered = compiler._format_reviewer_inputs(deps)
    assert "## Upstream Compiled Prompts" in rendered
    assert "### node_a" in rendered
    assert "### node_b" in rendered
    assert "[truncated]" in rendered  # node_a body was truncated
    assert "/Users/test" not in rendered  # absolute path scrubbed


# ----------- End-to-end gateway scenarios with reviewer escalation -----------


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))


class FakeMsg:
    chat_id = "rev-chat"
    channel_name = "cli"
    event_id = "rev-evt"

    def __init__(self, text: str) -> None:
        self.text = text


class _StubProvider(Provider):
    def __init__(self, response_text: str = "{}") -> None:
        self.response_text = response_text
        self.call_count = 0

    async def chat(self, messages, tools=None, stream=False, stream_callback=None):
        self.call_count += 1
        return LLMResponse(text=self.response_text)

    def format_tools(self, tools):
        return tools


def _gateway(tmp_path: Path, *, prompt_review_enabled: bool = True):
    config = AppConfig(
        workflow=WorkflowConfig(
            enabled=True,
            require_approval=False,  # we want auto-run, then reviewer drives the approval
        )
    )
    config.workflow.prompt_review.enabled = prompt_review_enabled
    db = Database(tmp_path / "rev.db")
    registry = ToolRegistry()
    for tool in BUILTIN_TOOLS:
        registry.register(tool)
    gate = PermissionGate(PermissionPolicy(config.permissions), ApprovalStore(db))

    gw = Gateway.__new__(Gateway)
    gw._config = config
    gw._storage = db
    gw._registry = registry
    gw._permission_gate = gate
    gw._workflow_store = WorkflowStore(db)
    gw._workflow_planner = WorkflowPlanner(config.workflow)
    gw._workflow_prompt_compiler = SubAgentPromptCompiler(config.workflow)
    gw._session_mgr = SessionManager(db)
    gw._audit_logger = None
    gw._provider_manager = ProviderManager(config, default_provider=_StubProvider())
    return gw


def test_dispatch_workflow_plan_injects_reviewer_when_enabled(tmp_path):
    """When prompt_review.enabled=True, /workflow plan creates the reviewer node in DB."""

    import asyncio

    gw = _gateway(tmp_path, prompt_review_enabled=True)
    channel = FakeChannel()
    asyncio.run(
        gw._dispatch_workflow_plan(
            spec=gw._workflow_planner.plan("review project", workflow_type="code_review"),
            user_task="review project",
            agent_cfg=AgentConfig(tools=["read_file", "write_file", "run_shell", "list_directory"]),
            msg=FakeMsg("review project"),
            agent_id="default",
            workspace_dir=tmp_path,
            sandbox_mode="safe",
            channel=channel,
            channel_name="cli",
            command="plan",
            force_approval=False,
            source="command",
        )
    )

    rows = gw._storage.fetchall("SELECT node_id FROM workflow_nodes ORDER BY rowid")
    node_ids = [r["node_id"] for r in rows]
    assert "prompt_review" in node_ids


def test_dispatch_workflow_plan_skips_reviewer_when_disabled(tmp_path):
    """When prompt_review.enabled=False, no reviewer node is created (Phase 6 parity)."""

    import asyncio

    gw = _gateway(tmp_path, prompt_review_enabled=False)
    channel = FakeChannel()
    asyncio.run(
        gw._dispatch_workflow_plan(
            spec=gw._workflow_planner.plan("review project", workflow_type="code_review"),
            user_task="review project",
            agent_cfg=AgentConfig(tools=["read_file", "write_file", "run_shell", "list_directory"]),
            msg=FakeMsg("review project"),
            agent_id="default",
            workspace_dir=tmp_path,
            sandbox_mode="safe",
            channel=channel,
            channel_name="cli",
            command="plan",
            force_approval=False,
            source="command",
        )
    )
    rows = gw._storage.fetchall("SELECT node_id FROM workflow_nodes")
    node_ids = [r["node_id"] for r in rows]
    assert "prompt_review" not in node_ids


def test_scheduler_does_not_co_batch_reviewer_with_merge():
    """Phase 7 invariant: while reviewer is pending, merge node never enters ready set."""
    from mini_claw.workflow.scheduler import WorkflowScheduler

    spec = inject_prompt_reviewer(code_review_workflow("audit"))
    statuses = {n.id: "pending" for n in spec.nodes}
    # Mark all body subagents done; reviewer & merge still pending
    for body in ["architecture_review", "security_review", "test_review"]:
        statuses[body] = "done"

    ready = WorkflowScheduler().ready_nodes(spec, statuses)
    ready_ids = {n.id for n in ready}
    assert "prompt_review" in ready_ids
    assert "merge_findings" not in ready_ids

    # Now reviewer is done — merge becomes ready
    statuses["prompt_review"] = "done"
    ready = WorkflowScheduler().ready_nodes(spec, statuses)
    ready_ids = {n.id for n in ready}
    assert "merge_findings" in ready_ids


def test_reviewer_blocking_escalates_workflow(tmp_path):
    """Reviewer returning approved=false escalates to awaiting_approval."""
    from mini_claw.agent.context import AgentContext
    from mini_claw.workflow.runner import WorkflowRunner

    config = WorkflowConfig(enabled=True, require_approval=False)
    config.prompt_review.enabled = True
    db = Database(tmp_path / "esc.db")
    store = WorkflowStore(db)
    compiler = SubAgentPromptCompiler(config)
    registry = ToolRegistry()
    for tool in BUILTIN_TOOLS:
        registry.register(tool)
    gate = PermissionGate(PermissionPolicy(AppConfig().permissions), ApprovalStore(db))

    spec = inject_prompt_reviewer(code_review_workflow("audit"))
    workflow_id = "wf-escalate"
    store.create_run(workflow_id, "rev-chat", "default", spec, status="running")

    runner = WorkflowRunner(
        config=config,
        store=store,
        compiler=compiler,
        provider=_StubProvider(),
        registry=registry,
        permission_gate=gate,
        result_processor=None,
    )

    reviewer_node = next(n for n in spec.nodes if n.agent_role == "prompt_reviewer")
    statuses = {n.id: "pending" for n in spec.nodes}
    issues = [
        {"node_id": "security_review", "issue": "scope unclear", "severity": "high"}
    ]
    results = {
        reviewer_node.id: WorkflowNodeResult(
            node_id=reviewer_node.id,
            status="done",
            summary="reviewer flagged issues",
            artifacts={"approved": False, "prompt_issues": issues},
        )
    }
    ctx = AgentContext(
        chat_id="rev-chat",
        agent_id="default",
        workspace_dir=tmp_path,
        channel=FakeChannel(),
        audit_logger=None,
    )

    blocking = runner._reviewer_blocking(workflow_id, spec, [reviewer_node], results, statuses, ctx)
    assert blocking is True

    row = db.fetchone(
        "SELECT status, approval_id, approval_reason FROM workflow_runs WHERE workflow_id=?",
        (workflow_id,),
    )
    assert row["status"] == "awaiting_approval"
    assert row["approval_id"]
    assert "issues" in (row["approval_reason"] or "").lower() or "issues" in (row["approval_reason"] or "")
    approval = db.fetchone(
        "SELECT approval_type FROM pending_approvals WHERE id=?",
        (row["approval_id"],),
    )
    assert approval["approval_type"] == "workflow_reviewer_override"


def test_reviewer_blocking_passes_when_approved(tmp_path):
    """Reviewer with approved=true does NOT escalate."""
    from mini_claw.agent.context import AgentContext
    from mini_claw.workflow.runner import WorkflowRunner

    config = WorkflowConfig(enabled=True, require_approval=False)
    config.prompt_review.enabled = True
    db = Database(tmp_path / "ok.db")
    store = WorkflowStore(db)
    compiler = SubAgentPromptCompiler(config)
    registry = ToolRegistry()
    for tool in BUILTIN_TOOLS:
        registry.register(tool)
    gate = PermissionGate(PermissionPolicy(AppConfig().permissions), ApprovalStore(db))

    spec = inject_prompt_reviewer(code_review_workflow("audit"))
    workflow_id = "wf-ok"
    store.create_run(workflow_id, "rev-chat", "default", spec, status="running")
    runner = WorkflowRunner(
        config=config,
        store=store,
        compiler=compiler,
        provider=_StubProvider(),
        registry=registry,
        permission_gate=gate,
        result_processor=None,
    )

    reviewer_node = next(n for n in spec.nodes if n.agent_role == "prompt_reviewer")
    statuses = {n.id: "pending" for n in spec.nodes}
    results = {
        reviewer_node.id: WorkflowNodeResult(
            node_id=reviewer_node.id,
            status="done",
            summary="all good",
            artifacts={"approved": True, "prompt_issues": []},
        )
    }
    ctx = AgentContext(
        chat_id="rev-chat",
        agent_id="default",
        workspace_dir=tmp_path,
        channel=FakeChannel(),
        audit_logger=None,
    )

    blocking = runner._reviewer_blocking(workflow_id, spec, [reviewer_node], results, statuses, ctx)
    assert blocking is False
    row = db.fetchone("SELECT status FROM workflow_runs WHERE workflow_id=?", (workflow_id,))
    # Status not changed by _reviewer_blocking — it stays as runner set it.
    assert row["status"] == "running"


def test_reviewer_timeout_escalates_workflow(tmp_path):
    """Reviewer artifacts.timed_out=True escalates with workflow_reviewer_timeout audit."""
    from mini_claw.agent.context import AgentContext
    from mini_claw.workflow.runner import WorkflowRunner

    config = WorkflowConfig(enabled=True, require_approval=False)
    config.prompt_review.enabled = True
    db = Database(tmp_path / "to.db")
    store = WorkflowStore(db)
    compiler = SubAgentPromptCompiler(config)
    registry = ToolRegistry()
    for tool in BUILTIN_TOOLS:
        registry.register(tool)
    gate = PermissionGate(PermissionPolicy(AppConfig().permissions), ApprovalStore(db))

    captured_events: list[dict] = []

    class CapturingAudit:
        def log_security_event(self, *, event_type, details, chat_id, agent_id):
            captured_events.append({"event_type": event_type, "details": details})

    spec = inject_prompt_reviewer(code_review_workflow("audit"))
    workflow_id = "wf-timeout"
    store.create_run(workflow_id, "rev-chat", "default", spec, status="running")
    runner = WorkflowRunner(
        config=config,
        store=store,
        compiler=compiler,
        provider=_StubProvider(),
        registry=registry,
        permission_gate=gate,
        result_processor=None,
    )

    reviewer_node = next(n for n in spec.nodes if n.agent_role == "prompt_reviewer")
    statuses = {n.id: "pending" for n in spec.nodes}
    results = {
        reviewer_node.id: WorkflowNodeResult(
            node_id=reviewer_node.id,
            status="done",
            summary="reviewer timed out",
            artifacts={
                "approved": False,
                "timed_out": True,
                "prompt_issues": [
                    {"node_id": reviewer_node.id, "issue": "timeout", "severity": "high"}
                ],
            },
        )
    }
    ctx = AgentContext(
        chat_id="rev-chat",
        agent_id="default",
        workspace_dir=tmp_path,
        channel=FakeChannel(),
        audit_logger=CapturingAudit(),
    )

    blocking = runner._reviewer_blocking(workflow_id, spec, [reviewer_node], results, statuses, ctx)
    assert blocking is True
    event_types = [e["event_type"] for e in captured_events]
    assert "workflow_reviewer_timeout" in event_types

    row = db.fetchone(
        "SELECT status, approval_reason FROM workflow_runs WHERE workflow_id=?",
        (workflow_id,),
    )
    assert row["status"] == "awaiting_approval"
    assert "timeout" in (row["approval_reason"] or "").lower()


@pytest.mark.asyncio
async def test_workflow_reject_after_reviewer_override_writes_audit(tmp_path):
    """User rejects reviewer override → status=rejected and audit event recorded."""
    captured: list[dict] = []

    class CapturingAudit:
        def log_security_event(self, *, event_type, details, chat_id, agent_id):
            captured.append({"event_type": event_type, "details": details})

    gw = _gateway(tmp_path, prompt_review_enabled=True)
    gw._audit_logger = CapturingAudit()

    # Manually set up an awaiting_approval workflow with reviewer override approval.
    spec = inject_prompt_reviewer(code_review_workflow("review"))
    workflow_id = "wf-reject"
    gw._workflow_store.create_run(workflow_id, "rev-chat", "default", spec, status="running")
    approval_id = gw._permission_gate.create_pending(
        run_id=workflow_id,
        chat_id="rev-chat",
        agent_id="default",
        tool_call={"tool": "workflow_reviewer_override", "args": {}},
        ttl=3600,
        approval_type="workflow_reviewer_override",
    )
    gw._workflow_store.update_run_status(
        workflow_id, "awaiting_approval", approval_id=approval_id, approval_reason="reviewer flagged"
    )

    channel = FakeChannel()
    handled = await gw._handle_workflow_command(
        FakeMsg(f"/workflow reject {workflow_id}"),
        AgentConfig(tools=["read_file", "write_file", "run_shell", "list_directory"]),
        "default",
        tmp_path,
        "safe",
        channel,
        "cli",
    )
    assert handled is True

    row = gw._storage.fetchone(
        "SELECT status FROM workflow_runs WHERE workflow_id=?", (workflow_id,)
    )
    assert row["status"] == "rejected"
    event_types = [e["event_type"] for e in captured]
    assert "workflow_reviewer_override_rejected" in event_types
