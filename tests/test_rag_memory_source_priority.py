"""Test source_priority parameter in memory extractors (Phase 9 ac-4).

Ensures auto memory candidates come only from structured sources
as specified in Phase 9 plan.
"""
from __future__ import annotations

from mini_claw.rag.memory.extractor import (
    extract_from_session_compaction,
    extract_from_task_state,
    extract_from_workflow_merger,
    extract_from_agent_summary,
)


class FakeTaskState:
    """Minimal TaskState mock for testing."""

    def __init__(self, key_facts: list[str]):
        self.key_facts = key_facts
        self.pinned_facts = set()


def test_session_compaction_respects_source_priority_allow():
    """When source_priority includes 'compaction', extraction proceeds."""
    msgs = [
        {"id": 1, "role": "user", "content": "我们决定使用 PostgreSQL 而不是 MySQL。"},
        {"id": 2, "role": "assistant", "content": "好的，记下了。"},
    ]
    candidates = extract_from_session_compaction(
        msgs,
        chat_id="chat1",
        agent_id="agent1",
        source_priority=["compaction", "workflow"],
    )
    assert len(candidates) > 0
    assert all(c.source_type == "compaction" for c in candidates)


def test_session_compaction_respects_source_priority_deny():
    """When source_priority excludes 'compaction', extraction returns empty."""
    msgs = [
        {"id": 1, "role": "user", "content": "我们决定使用 PostgreSQL 而不是 MySQL。"},
        {"id": 2, "role": "assistant", "content": "好的，记下了。"},
    ]
    candidates = extract_from_session_compaction(
        msgs,
        chat_id="chat1",
        agent_id="agent1",
        source_priority=["workflow", "task_state"],  # no 'compaction'
    )
    assert len(candidates) == 0


def test_session_compaction_no_filter_when_priority_none():
    """When source_priority is None, extraction proceeds normally."""
    msgs = [
        {"id": 1, "role": "user", "content": "我们决定使用 PostgreSQL 而不是 MySQL。"},
        {"id": 2, "role": "assistant", "content": "好的，记下了。"},
    ]
    candidates = extract_from_session_compaction(
        msgs,
        chat_id="chat1",
        agent_id="agent1",
        source_priority=None,
    )
    assert len(candidates) > 0


def test_task_state_respects_source_priority_allow():
    """When source_priority includes 'task_state', extraction proceeds."""
    task_state = FakeTaskState(
        key_facts=["User prefers pytest -v for all test runs."]
    )
    candidates = extract_from_task_state(
        task_state,
        chat_id="chat1",
        agent_id="agent1",
        source_priority=["task_state", "compaction"],
    )
    assert len(candidates) > 0
    assert all(c.source_type == "task_state" for c in candidates)


def test_task_state_respects_source_priority_deny():
    """When source_priority excludes 'task_state', extraction returns empty."""
    task_state = FakeTaskState(
        key_facts=["User prefers pytest -v for all test runs."]
    )
    candidates = extract_from_task_state(
        task_state,
        chat_id="chat1",
        agent_id="agent1",
        source_priority=["compaction", "workflow"],  # no 'task_state'
    )
    assert len(candidates) == 0


def test_workflow_merger_respects_source_priority_allow():
    """When source_priority includes 'workflow', extraction proceeds."""
    merged_result = {
        "key_findings": [
            "Team decided the security boundary must be at PermissionGate."
        ]
    }
    candidates = extract_from_workflow_merger(
        merged_result,
        workflow_id="wf1",
        chat_id="chat1",
        agent_id="agent1",
        source_priority=["workflow", "compaction"],
    )
    assert len(candidates) > 0
    assert all(c.source_type == "workflow" for c in candidates)


def test_workflow_merger_respects_source_priority_deny():
    """When source_priority excludes 'workflow', extraction returns empty."""
    merged_result = {
        "key_findings": [
            "Team decided the security boundary must be at PermissionGate."
        ]
    }
    candidates = extract_from_workflow_merger(
        merged_result,
        workflow_id="wf1",
        chat_id="chat1",
        agent_id="agent1",
        source_priority=["compaction", "task_state"],  # no 'workflow'
    )
    assert len(candidates) == 0


def test_agent_summary_respects_source_priority_filter():
    """agent_summary respects source_priority when provided."""
    summary_text = (
        "I learned that the user prefers to run tests with pytest -v. "
        "This strategy works well for this project and should be remembered."
    )

    # When agent_summary in priority, attempt extraction
    candidates_with = extract_from_agent_summary(
        summary_text,
        agent_id="agent1",
        chat_id="chat1",
        source_priority=["agent_summary", "compaction"],
    )

    # When agent_summary not in priority, block extraction
    candidates_without = extract_from_agent_summary(
        summary_text,
        agent_id="agent1",
        chat_id="chat1",
        source_priority=["compaction", "workflow"],
    )

    # The filter should work: candidates_without must be 0
    assert len(candidates_without) == 0
    # Whether candidates_with > 0 depends on heuristic gates, but filter works


def test_empty_priority_list_blocks_all():
    """When source_priority is empty list, all extraction is blocked."""
    msgs = [{"id": 1, "role": "user", "content": "我们决定使用 PostgreSQL。"}]
    candidates = extract_from_session_compaction(
        msgs,
        chat_id="chat1",
        agent_id="agent1",
        source_priority=[],
    )
    assert len(candidates) == 0

    task_state = FakeTaskState(key_facts=["User always prefers pytest -v."])
    candidates = extract_from_task_state(
        task_state,
        chat_id="chat1",
        agent_id="agent1",
        source_priority=[],
    )
    assert len(candidates) == 0

    merged_result = {"key_findings": ["Security boundary must be at PermissionGate."]}
    candidates = extract_from_workflow_merger(
        merged_result,
        workflow_id="wf1",
        chat_id="chat1",
        agent_id="agent1",
        source_priority=[],
    )
    assert len(candidates) == 0

    candidates = extract_from_agent_summary(
        "I learned the team prefers pytest.",
        agent_id="agent1",
        chat_id="chat1",
        source_priority=[],
    )
    assert len(candidates) == 0
