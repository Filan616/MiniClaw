"""Tests for Phase 8 M5: extractors + consolidator."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from mini_claw.rag.memory.consolidator import consolidate
from mini_claw.rag.memory.extractor import (
    extract_from_session_compaction,
    extract_from_task_state,
    extract_from_workflow_merger,
)
from mini_claw.rag.models import MemoryCandidate


# ===================== extract_from_session_compaction =====================


def test_session_extractor_finds_decision_sentences():
    msgs = [
        {"id": 1, "role": "user", "content": "我们决定使用 PostgreSQL 而不是 MySQL。"},
        {"id": 2, "role": "assistant", "content": "好的，记下了。"},
        {"id": 3, "role": "user", "content": "另外用户 prefer 中文回复。"},
    ]
    candidates = extract_from_session_compaction(
        msgs, chat_id="c1", agent_id="a1", session_id="s1", channel="cli"
    )
    assert len(candidates) >= 1
    # source chain present
    for c in candidates:
        assert c.source_type == "compaction"
        assert c.created_by_agent_id == "a1"
        assert c.created_from_chat_id == "c1"
        assert c.source_session_id == "s1"
        assert c.created_from_channel == "cli"


def test_session_extractor_skips_non_decision_chatter():
    msgs = [
        {"id": 1, "role": "user", "content": "你好啊"},
        {"id": 2, "role": "assistant", "content": "你好！有什么可以帮你？"},
    ]
    candidates = extract_from_session_compaction(
        msgs, chat_id="c1", agent_id="a1"
    )
    assert candidates == []


def test_session_extractor_caps_candidate_count():
    msgs = [
        {"id": i, "role": "user", "content": f"Decision number {i} we always must follow."}
        for i in range(20)
    ]
    candidates = extract_from_session_compaction(
        msgs, chat_id="c1", agent_id="a1"
    )
    assert len(candidates) <= 5


# ===================== extract_from_task_state =====================


def test_task_state_extractor_promotes_decision_facts():
    state = SimpleNamespace(
        key_facts=[
            "we always run black before commit",
            "user prefers terse responses",
            "today is sunny",  # not a decision → skipped
        ],
        pinned_facts=set(),
    )
    cands = extract_from_task_state(state, chat_id="c1", agent_id="a1")
    assert all(isinstance(c, MemoryCandidate) for c in cands)
    contents = [c.content for c in cands]
    assert any("always" in c.lower() for c in contents)
    assert any("prefer" in c.lower() for c in contents)
    assert not any("sunny" in c.lower() for c in contents)


def test_task_state_pinned_facts_get_higher_stability():
    pinned_text = "always pin this rule"
    state = SimpleNamespace(
        key_facts=[pinned_text, "we decided to use postgres"],
        pinned_facts={pinned_text},
    )
    cands = extract_from_task_state(state, chat_id="c1", agent_id="a1")
    pinned_cand = next((c for c in cands if c.content == pinned_text), None)
    assert pinned_cand is not None
    assert pinned_cand.stability == 4
    assert pinned_cand.confidence >= 0.8


def test_task_state_extractor_handles_missing_attributes():
    """Cope with TaskState-like objects missing pinned_facts."""
    state = SimpleNamespace(key_facts=["we always test"])
    cands = extract_from_task_state(state, chat_id="c1", agent_id="a1")
    assert isinstance(cands, list)


def test_task_state_extractor_returns_empty_for_none():
    assert extract_from_task_state(None, chat_id="c1", agent_id="a1") == []


# ===================== extract_from_workflow_merger =====================


def test_workflow_extractor_promotes_decision_findings():
    """Phase 9 WM-4: Only key_findings are extracted as workflow_finding type."""
    merged = {
        "key_findings": [
            "we always lint before tests run",
            "must never commit secrets to git",
            "fixed bug in auth flow",  # too short / no decision → skipped
        ],
        "remaining_risks": [
            "some risk that should be ignored",
        ],
        "recommended_next_steps": [
            "some step that should be ignored",
        ],
    }
    cands = extract_from_workflow_merger(
        merged, workflow_id="wf-1", chat_id="c1", agent_id="a1", workspace_dir="/workspace"
    )
    assert cands
    # All candidates should be workflow_finding type (not constraint or operational_rule)
    types = {c.memory_type for c in cands}
    assert types == {"workflow_finding"}
    # Verify all are from key_findings
    for c in cands:
        assert c.source_workflow_id == "wf-1"
        assert c.scope_type == "workspace"
        assert c.scope_id == "/workspace"
        assert c.memory_type == "workflow_finding"


def test_workflow_extractor_handles_non_dict():
    assert extract_from_workflow_merger("not a dict", workflow_id="x", chat_id="c", agent_id="a", workspace_dir="/workspace") == []


def test_workflow_extractor_requires_workspace_dir():
    """Phase 9 WM-4: Workflow memory without workspace_dir falls back to agent_id."""
    merged = {
        "key_findings": [
            "we always lint before tests run",
        ],
    }
    # Without workspace_dir, should fall back to agent_id for scope_id
    cands = extract_from_workflow_merger(
        merged, workflow_id="wf-1", chat_id="c1", agent_id="a1"
    )
    assert len(cands) > 0
    for c in cands:
        assert c.scope_type == "workspace"
        assert c.scope_id == "a1"  # Falls back to agent_id

    # With workspace_dir, should use workspace_dir for scope_id
    cands = extract_from_workflow_merger(
        merged, workflow_id="wf-1", chat_id="c1", agent_id="a1", workspace_dir="/workspace"
    )
    assert len(cands) > 0
    for c in cands:
        assert c.scope_type == "workspace"
        assert c.scope_id == "/workspace"


def test_workflow_extractor_maps_workflow_type_to_memory_type():
    """Phase 9 WM-2: Map workflow intent (coding/security/test) to memory types."""
    merged = {
        "key_findings": [
            "we must validate all user input",
        ],
    }

    # Test security workflow -> security_rule
    cands = extract_from_workflow_merger(
        merged,
        workflow_id="wf-sec",
        chat_id="c1",
        agent_id="a1",
        workspace_dir="/workspace",
        workflow_intent="security_review: check permission boundaries",
    )
    assert len(cands) > 0
    assert all(c.memory_type == "security_rule" for c in cands)
    # Verify workflow_type is recorded in source_chain_json
    import json
    for c in cands:
        chain = json.loads(c.source_chain_json)
        assert chain["workflow_type"] == "security"

    # Test coding workflow -> module_boundary (for key_findings)
    cands = extract_from_workflow_merger(
        merged,
        workflow_id="wf-code",
        chat_id="c1",
        agent_id="a1",
        workspace_dir="/workspace",
        workflow_intent="code_review: review architecture",
    )
    assert len(cands) > 0
    assert all(c.memory_type == "module_boundary" for c in cands)
    for c in cands:
        chain = json.loads(c.source_chain_json)
        assert chain["workflow_type"] == "coding"

    # Test test/debug workflow -> implementation_note (for key_findings)
    cands = extract_from_workflow_merger(
        merged,
        workflow_id="wf-test",
        chat_id="c1",
        agent_id="a1",
        workspace_dir="/workspace",
        workflow_intent="debug_fix: fix authentication bug",
    )
    assert len(cands) > 0
    assert all(c.memory_type == "implementation_note" for c in cands)
    for c in cands:
        chain = json.loads(c.source_chain_json)
        assert chain["workflow_type"] == "test"

    # Test generic workflow -> workflow_finding (base type)
    cands = extract_from_workflow_merger(
        merged,
        workflow_id="wf-generic",
        chat_id="c1",
        agent_id="a1",
        workspace_dir="/workspace",
        workflow_intent="unknown_workflow: some task",
    )
    assert len(cands) > 0
    assert all(c.memory_type == "workflow_finding" for c in cands)
    for c in cands:
        chain = json.loads(c.source_chain_json)
        assert chain["workflow_type"] == "generic"


# ===================== consolidator =====================


class _FakeProvider:
    """Returns a JSON-shaped consolidated response."""

    def __init__(self, response_text: str):
        self.response_text = response_text
        self.calls = 0

    async def chat(self, messages, tools=None, stream=False, stream_callback=None):
        self.calls += 1
        return SimpleNamespace(text=self.response_text)


def _candidate(content="user prefers brief answers"):
    return MemoryCandidate(
        candidate_id="c1",
        content=content,
        memory_type="user_preference",
        scope_type="agent",
        scope_id="a1",
        source_type="explicit",
        status="pending",
        created_at=0,
        updated_at=0,
        source_chain_json="{}",
        created_by_agent_id="a1",
        created_from_chat_id="c1",
    )


def test_consolidator_no_provider_returns_input():
    cand = _candidate()
    result = asyncio.run(consolidate(cand, provider=None))
    assert result is cand


def test_consolidator_rewrites_with_valid_json():
    provider = _FakeProvider(
        '{"content": "User prefers concise replies in all chats", "summary": "user prefers concise"}'
    )
    cand = _candidate()
    result = asyncio.run(consolidate(cand, provider=provider))
    assert provider.calls == 1
    assert result.content == "User prefers concise replies in all chats"


def test_consolidator_falls_back_on_invalid_json():
    provider = _FakeProvider("sorry I don't know how to do that")
    cand = _candidate()
    result = asyncio.run(consolidate(cand, provider=provider))
    assert result.content == cand.content  # unchanged


def test_consolidator_extracts_json_from_prose():
    provider = _FakeProvider(
        'Here is your answer: {"content": "extracted fact", "summary": "ok"} hope it helps'
    )
    cand = _candidate()
    result = asyncio.run(consolidate(cand, provider=provider))
    assert result.content == "extracted fact"


def test_consolidator_rejects_overlong_response():
    """Refuse rewrites that are absurdly longer than the input."""
    long = "x" * 10000
    provider = _FakeProvider(f'{{"content": "{long}", "summary": "x"}}')
    cand = _candidate(content="short prompt")
    result = asyncio.run(consolidate(cand, provider=provider))
    # Falls back to original
    assert result.content == "short prompt"


def test_consolidator_handles_provider_exception():
    class _BoomProvider:
        async def chat(self, *a, **kw):
            raise RuntimeError("provider down")

    cand = _candidate()
    result = asyncio.run(consolidate(cand, provider=_BoomProvider()))
    assert result is cand
