"""DAG scheduling helpers for workflow execution."""

from __future__ import annotations

from mini_claw.workflow.spec import WorkflowNode, WorkflowSpec


RISKY_TOOLS = {"write_file", "run_shell", "apply_patch"}


def node_requires_write_lock(node: WorkflowNode) -> bool:
    return bool(set(node.tools) & RISKY_TOOLS)


class WorkflowScheduler:
    def ready_nodes(self, spec: WorkflowSpec, statuses: dict[str, str]) -> list[WorkflowNode]:
        ready = []
        for node in spec.nodes:
            if statuses.get(node.id, "pending") != "pending":
                continue
            if all(statuses.get(dep) == "done" for dep in node.depends_on):
                ready.append(node)
        return ready

    def split_batch(self, nodes: list[WorkflowNode], max_parallel: int) -> tuple[list[WorkflowNode], list[WorkflowNode]]:
        read_only = [node for node in nodes if not node_requires_write_lock(node)]
        risky = [node for node in nodes if node_requires_write_lock(node)]
        if risky:
            return read_only[: max(0, max_parallel - 1)], risky[:1]
        return read_only[:max_parallel], []
