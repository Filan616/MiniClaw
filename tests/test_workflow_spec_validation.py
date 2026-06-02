import pytest

from mini_claw.workflow.spec import WorkflowNode, WorkflowSpec, WorkflowSpecError, validate_workflow_spec


def test_validate_workflow_rejects_cycle():
    spec = WorkflowSpec(
        "cycle",
        "bad",
        [
            WorkflowNode("a", "subagent", "researcher", "A", "scope", ["read_file"], depends_on=["b"]),
            WorkflowNode("b", "subagent", "researcher", "B", "scope", ["read_file"], depends_on=["a"]),
        ],
    )
    with pytest.raises(WorkflowSpecError):
        validate_workflow_spec(spec, available_tools={"read_file"}, max_nodes=8, max_parallel=3)


def test_validate_workflow_rejects_unknown_tools():
    spec = WorkflowSpec(
        "unknown",
        "bad",
        [WorkflowNode("a", "subagent", "researcher", "A", "scope", ["unknown_tool"])],
    )
    with pytest.raises(WorkflowSpecError):
        validate_workflow_spec(spec, available_tools={"read_file"}, max_nodes=8, max_parallel=3)


def test_validate_workflow_accepts_valid_dag():
    spec = WorkflowSpec(
        "valid",
        "ok",
        [
            WorkflowNode("a", "subagent", "researcher", "A", "scope", ["read_file"]),
            WorkflowNode("b", "subagent", "planner", "B", "scope", ["read_file"], depends_on=["a"]),
        ],
    )
    validate_workflow_spec(spec, available_tools={"read_file"}, max_nodes=8, max_parallel=3)
