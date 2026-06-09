"""Phase 10 M10.3: ReActPolicyResolver tests."""

from mini_claw.agent.react_policy import (
    policy_from_config,
    resolve_react_policy,
)
from mini_claw.agent.reflection_trigger import ReActPolicy
from mini_claw.workflow.spec import ReactNodePolicy, WorkflowNode


def test_policy_from_none_uses_defaults():
    p = policy_from_config(None)
    assert p.mode == "controlled"
    assert p.reflect_every_iteration is False
    assert p.reflect_before_finalize is True


def test_resolve_no_overrides_yields_defaults():
    p = resolve_react_policy()
    assert p.mode == "controlled"


def test_resolve_high_risk_task_promotes_to_strict():
    p = resolve_react_policy(task_risk="high")
    assert p.mode == "strict"
    assert p.reflect_every_iteration is True


def test_resolve_workflow_node_strict_overrides_controlled_default():
    node = WorkflowNode(
        id="n1",
        type="subagent",
        agent_role="implementer",
        objective="x",
        scope="x",
        tools=[],
        react_policy=ReactNodePolicy(mode="strict", reflect_every_iteration=True),
    )
    p = resolve_react_policy(workflow_node=node)
    assert p.mode == "strict"
    assert p.reflect_every_iteration is True


def test_resolve_user_override_wins():
    p = resolve_react_policy(user_override={"reflect_before_finalize": False})
    assert p.reflect_before_finalize is False


def test_node_controlled_keeps_default_when_explicit_controlled():
    node = WorkflowNode(
        id="n1",
        type="subagent",
        agent_role="researcher",
        objective="x",
        scope="x",
        tools=[],
        react_policy=ReactNodePolicy(mode="controlled"),
    )
    p = resolve_react_policy(workflow_node=node)
    assert p.mode == "controlled"
    assert p.reflect_every_iteration is False


def test_apply_high_risk_defaults_idempotent():
    p = ReActPolicy()
    p.apply_high_risk_defaults()
    p.apply_high_risk_defaults()
    assert p.mode == "strict"
    assert p.reflect_every_iteration is True
