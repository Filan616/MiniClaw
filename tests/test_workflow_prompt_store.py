from pathlib import Path

from mini_claw.storage.db import Database
from mini_claw.workflow.spec import SubAgentPrompt
from mini_claw.workflow.store import WorkflowStore
from mini_claw.workflow.templates import code_review_workflow


def test_workflow_prompt_store_persists_redacted_prompt(tmp_path: Path):
    db = Database(tmp_path / "workflow.db")
    store = WorkflowStore(db)
    spec = code_review_workflow("review")
    store.create_run("wf1", "chat", "agent", spec)
    store.save_prompt(
        "wf1",
        "architecture_review",
        SubAgentPrompt(
            system_prompt="Authorization: Bearer [REDACTED]",
            user_prompt="## Output Contract\nReturn JSON",
            output_schema={"summary": "string"},
            allowed_tools=["read_file"],
            forbidden_tools=["write_file"],
            success_criteria=["done"],
            redacted=True,
        ),
    )
    prompts = store.list_prompts("wf1")
    assert prompts[0]["redacted"] == 1
    assert "[REDACTED]" in prompts[0]["system_prompt"]
