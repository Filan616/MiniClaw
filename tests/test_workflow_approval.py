from pathlib import Path

from mini_claw.permissions.approval_store import ApprovalStore
from mini_claw.storage.db import Database
from mini_claw.workflow.store import WorkflowStore
from mini_claw.workflow.templates import code_review_workflow


def test_workflow_approval_type_and_run_state_are_persisted(tmp_path: Path):
    db = Database(tmp_path / "approval.db")
    store = WorkflowStore(db)
    approvals = ApprovalStore(db)
    spec = code_review_workflow("review")
    store.create_run("wf1", "chat", "agent", spec)
    approvals.create_pending(
        approval_id="ap1",
        run_id="wf1",
        chat_id="chat",
        agent_id="agent",
        tool_name="workflow_plan",
        tool_args={"workflow_id": "wf1"},
        expires_at=9999999999,
        approval_type="workflow_plan",
    )
    store.update_run_status("wf1", "awaiting_approval", approval_id="ap1", approval_reason="risk")

    row = store.get_run("wf1")
    pending = approvals.get_pending("ap1")
    assert row["status"] == "awaiting_approval"
    assert row["approval_id"] == "ap1"
    assert pending["approval_type"] == "workflow_plan"

    assert approvals.resolve_pending("ap1", "approved")["status"] == "approved"
    store.mark_approved("wf1")
    assert store.get_run("wf1")["status"] == "running"

    store.mark_rejected("wf1")
    assert store.get_run("wf1")["status"] == "rejected"
