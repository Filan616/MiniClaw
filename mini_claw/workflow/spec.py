"""Structured workflow DSL for Mini-Claw."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


WorkflowNodeType = Literal["subagent", "tool", "merge", "verify"]
WorkflowRiskLevel = Literal["low", "medium", "high"]
WorkflowStatus = Literal[
    "planning",
    "awaiting_approval",
    "running",
    "suspended",
    "done",
    "failed",
    "rejected",
    "cancelled",
]
WorkflowNodeStatus = Literal["pending", "running", "done", "failed", "skipped"]


@dataclass(slots=True)
class NodePromptSpec:
    role_name: str
    mission: str
    focus_areas: list[str] = field(default_factory=list)
    in_scope: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    required_inputs: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    forbidden_tools: list[str] = field(default_factory=list)
    expected_artifacts: list[str] = field(default_factory=list)
    output_format: dict[str, Any] = field(default_factory=dict)
    success_criteria: list[str] = field(default_factory=list)


@dataclass(slots=True)
class WorkflowNode:
    id: str
    type: WorkflowNodeType
    agent_role: str
    objective: str
    scope: str
    tools: list[str]
    depends_on: list[str] = field(default_factory=list)
    input_refs: list[str] = field(default_factory=list)
    output_contract: dict[str, Any] = field(default_factory=dict)
    risk_level: WorkflowRiskLevel = "low"
    prompt_spec: NodePromptSpec | None = None
    timeout: int = 300


@dataclass(slots=True)
class WorkflowSpec:
    name: str
    reason: str
    nodes: list[WorkflowNode]
    execution_mode: Literal["sequential", "parallel", "mixed"] = "mixed"
    merge_strategy: str = "summarize"
    max_parallel: int = 3
    requires_approval: bool = False
    user_task: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkflowSpec":
        nodes = []
        for node_data in data.get("nodes", []):
            prompt_data = node_data.get("prompt_spec")
            prompt_spec = NodePromptSpec(**prompt_data) if prompt_data else None
            nodes.append(
                WorkflowNode(
                    id=node_data["id"],
                    type=node_data.get("type", "subagent"),
                    agent_role=node_data.get("agent_role", "researcher"),
                    objective=node_data.get("objective") or node_data.get("task", ""),
                    scope=node_data.get("scope", ""),
                    tools=list(node_data.get("tools", [])),
                    depends_on=list(node_data.get("depends_on", [])),
                    input_refs=list(node_data.get("input_refs", [])),
                    output_contract=dict(node_data.get("output_contract", {})),
                    risk_level=node_data.get("risk_level", "low"),
                    prompt_spec=prompt_spec,
                    timeout=int(node_data.get("timeout", 300)),
                )
            )
        return cls(
            name=data["name"],
            reason=data.get("reason", ""),
            nodes=nodes,
            execution_mode=data.get("execution_mode", "mixed"),
            merge_strategy=data.get("merge_strategy", "summarize"),
            max_parallel=int(data.get("max_parallel", 3)),
            requires_approval=bool(data.get("requires_approval", False)),
            user_task=data.get("user_task", ""),
        )

    @classmethod
    def from_json(cls, raw: str) -> "WorkflowSpec":
        return cls.from_dict(json.loads(raw))


@dataclass(slots=True)
class WorkflowNodeResult:
    node_id: str
    status: WorkflowNodeStatus
    summary: str = ""
    artifacts: dict[str, Any] = field(default_factory=dict)
    agent_run_id: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class WorkflowRun:
    id: str
    chat_id: str
    agent_id: str
    status: WorkflowStatus
    spec: WorkflowSpec
    node_results: dict[str, WorkflowNodeResult] = field(default_factory=dict)
    approval_id: str | None = None
    approval_reason: str | None = None
    error: str | None = None


@dataclass(slots=True)
class SubAgentPrompt:
    system_prompt: str
    user_prompt: str
    output_schema: dict[str, Any]
    allowed_tools: list[str]
    forbidden_tools: list[str]
    success_criteria: list[str]
    redacted: bool = False


class WorkflowSpecError(ValueError):
    """Raised when a workflow spec is unsafe or invalid."""


def validate_workflow_spec(
    spec: WorkflowSpec,
    *,
    available_tools: set[str],
    max_nodes: int,
    max_parallel: int,
    allow_llm_generated_script: bool = False,
) -> None:
    """Validate the structural safety of a WorkflowSpec."""
    if not spec.nodes:
        raise WorkflowSpecError("workflow must contain at least one node")
    if len(spec.nodes) > max_nodes:
        raise WorkflowSpecError(f"workflow has too many nodes: {len(spec.nodes)} > {max_nodes}")
    if spec.max_parallel > max_parallel:
        raise WorkflowSpecError(f"max_parallel exceeds configured limit: {spec.max_parallel} > {max_parallel}")
    if allow_llm_generated_script:
        raise WorkflowSpecError("LLM generated scripts are not supported in Phase 5 MVP")

    ids = [node.id for node in spec.nodes]
    if len(set(ids)) != len(ids):
        raise WorkflowSpecError("workflow node ids must be unique")
    id_set = set(ids)

    for node in spec.nodes:
        if node.type not in ("subagent", "tool", "merge", "verify"):
            raise WorkflowSpecError(f"invalid node type: {node.type}")
        if node.risk_level not in ("low", "medium", "high"):
            raise WorkflowSpecError(f"invalid risk level for {node.id}: {node.risk_level}")
        missing = [dep for dep in node.depends_on if dep not in id_set]
        if missing:
            raise WorkflowSpecError(f"node {node.id} depends on missing nodes: {missing}")
        unknown_tools = sorted(set(node.tools) - available_tools)
        if unknown_tools:
            raise WorkflowSpecError(f"node {node.id} references unknown tools: {unknown_tools}")
        for value in [node.objective, node.scope]:
            lowered = value.lower()
            if "python script" in lowered or "shell script" in lowered or "execute script" in lowered:
                raise WorkflowSpecError("workflow nodes may not request generated scripts")

    _assert_acyclic(spec.nodes)


def _assert_acyclic(nodes: list[WorkflowNode]) -> None:
    by_id = {node.id: node for node in nodes}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visited:
            return
        if node_id in visiting:
            raise WorkflowSpecError("workflow dependencies must form a DAG")
        visiting.add(node_id)
        for dep in by_id[node_id].depends_on:
            visit(dep)
        visiting.remove(node_id)
        visited.add(node_id)

    for node in nodes:
        visit(node.id)
