from pathlib import Path

import pytest

from mini_claw.agent.task_state import TaskState
from mini_claw.config import AgentConfig, WorkflowConfig
from mini_claw.workflow.prompt_compiler import SubAgentPromptCompiler
from mini_claw.workflow.spec import WorkflowNode, WorkflowNodeResult, WorkflowSpec


def test_prompt_compiler_generates_required_sections_and_dependency_context():
    node = WorkflowNode(
        id="security",
        type="subagent",
        agent_role="security_reviewer",
        objective="Review permission risks.",
        scope="permissions only",
        tools=["read_file", "list_directory", "write_file"],
        depends_on=["scan"],
        output_contract={"summary": "string", "risks": []},
    )
    spec = WorkflowSpec("review", "broad review", [node], user_task="检查安全边界")
    compiler = SubAgentPromptCompiler(WorkflowConfig())
    prompt = compiler.compile(
        spec,
        node,
        spec.user_task,
        {"scan": WorkflowNodeResult("scan", "done", summary="found permission files")},
        TaskState(task_description="phase 5"),
        AgentConfig(tools=["read_file", "list_directory", "write_file"]),
    )

    combined = prompt.system_prompt + "\n" + prompt.user_prompt
    for section in [
        "## Role",
        "## Global Goal",
        "## Local Mission",
        "## Context Inputs",
        "## Tool Policy",
        "## Boundaries",
        "## Output Contract",
        "## Done Criteria",
    ]:
        assert section in combined
    assert "found permission files" in combined
    assert prompt.allowed_tools == ["list_directory", "read_file"]
    assert "write_file" in prompt.forbidden_tools


def test_prompt_compiler_redacts_secret_patterns():
    compiler = SubAgentPromptCompiler(WorkflowConfig())
    node = WorkflowNode(
        id="scan",
        type="subagent",
        agent_role="researcher",
        objective="Read context",
        scope="read only",
        tools=["read_file"],
        output_contract={"summary": "string"},
    )
    spec = WorkflowSpec("review", "reason", [node], user_task="Authorization: Bearer secret-token")
    prompt = compiler.compile(
        spec,
        node,
        spec.user_task,
        {},
        TaskState(),
        AgentConfig(tools=["read_file"]),
    )
    redacted = compiler.redact(prompt)
    assert redacted.redacted is True
    assert "secret-token" not in redacted.system_prompt
    assert "[REDACTED]" in redacted.system_prompt
