from mini_claw.workflow.scheduler import WorkflowScheduler
from mini_claw.workflow.spec import WorkflowNode


def test_scheduler_splits_read_only_parallel_and_risky_serial():
    scheduler = WorkflowScheduler()
    nodes = [
        WorkflowNode("read1", "subagent", "researcher", "Read", "scope", ["read_file"]),
        WorkflowNode("read2", "subagent", "researcher", "Read", "scope", ["list_directory"]),
        WorkflowNode("write", "subagent", "implementer", "Write", "scope", ["write_file"]),
        WorkflowNode("shell", "verify", "tester", "Test", "scope", ["run_shell"]),
    ]
    read_batch, risky_batch = scheduler.split_batch(nodes, max_parallel=3)
    assert [node.id for node in read_batch] == ["read1", "read2"]
    assert [node.id for node in risky_batch] == ["write"]
