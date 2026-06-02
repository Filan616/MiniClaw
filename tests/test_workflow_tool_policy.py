import pytest

from mini_claw.config import AgentConfig, WorkflowConfig
from mini_claw.workflow.prompt_compiler import SubAgentPromptCompiler
from mini_claw.workflow.spec import WorkflowNode, WorkflowSpecError


def test_effective_tools_are_intersection_of_node_agent_and_role():
    compiler = SubAgentPromptCompiler(WorkflowConfig())
    node = WorkflowNode(
        id="security",
        type="subagent",
        agent_role="security_reviewer",
        objective="Review",
        scope="read only",
        tools=["read_file", "write_file"],
    )
    tools = compiler.effective_tools(node, AgentConfig(tools=["read_file", "write_file"]))
    assert tools == ["read_file"]


def test_subagent_with_no_effective_tools_is_rejected():
    compiler = SubAgentPromptCompiler(WorkflowConfig())
    node = WorkflowNode(
        id="security",
        type="subagent",
        agent_role="security_reviewer",
        objective="Review",
        scope="read only",
        tools=["write_file"],
    )
    with pytest.raises(WorkflowSpecError):
        compiler.effective_tools(node, AgentConfig(tools=["write_file"]))
