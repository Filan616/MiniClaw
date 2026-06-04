"""Tests for Phase 8 M1: RAG schema, RagStore CRUD, and RagConfig defaults."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from mini_claw.config import AppConfig, RagConfig
from mini_claw.rag.models import (
    AUDIT_EVENT_TYPES,
    ActiveContext,
    MemoryCandidate,
    RagChunk,
    RagItem,
)
from mini_claw.rag.store import RagStore
from mini_claw.storage.db import Database


@pytest.fixture
def storage(tmp_path: Path) -> Database:
    return Database(tmp_path / "rag_schema.db")


@pytest.fixture
def store(storage: Database) -> RagStore:
    return RagStore(storage)


# ===================== Schema existence =====================


def test_six_rag_tables_exist(storage: Database):
    """All 6 RAG tables must be created by init_tables."""
    expected = {
        "rag_items",
        "rag_chunks",
        "rag_item_chunk_versions",
        "rag_reindex_diffs",
        "rag_reindex_diff_chunks",
        "rag_locks",
        "rag_chunks_fts",
        "rag_embeddings",
        "active_contexts",
        "memory_candidates",
    }
    rows = storage.fetchall(
        "SELECT name FROM sqlite_master WHERE type IN ('table','virtual')"
    )
    names = {r["name"] for r in rows}
    missing = expected - names
    assert not missing, f"missing tables: {missing}"


def test_rag_items_has_phase8_columns(storage: Database):
    """rag_items must include active_version + sensitivity_level (M1 schema enhancement)."""
    cols = {row["name"] for row in storage.fetchall("PRAGMA table_info(rag_items)")}
    assert "active_version" in cols
    assert "sensitivity_level" in cols
    assert "chunker_version" in cols
    assert "anchor_schema_version" in cols
    assert "embedding_model" in cols
    assert "last_reindex_diff_id" in cols
    assert "namespace" in cols
    assert "scope_type" in cols
    assert "owner_agent_id" in cols
    assert "content_hash" in cols


def test_rag_chunks_has_version_column(storage: Database):
    """rag_chunks must include version column for atomic reindex (M3)."""
    cols = {row["name"] for row in storage.fetchall("PRAGMA table_info(rag_chunks)")}
    assert "version" in cols
    assert "anchor_id" in cols
    assert "chunk_hash" in cols
    assert "chunker_version" in cols
    assert "anchor_schema_version" in cols
    assert "item_id" in cols
    assert "chunk_index" in cols


def test_memory_candidates_has_full_source_chain(storage: Database):
    """memory_candidates must record complete source chain (M5 traceability)."""
    cols = {
        row["name"]
        for row in storage.fetchall("PRAGMA table_info(memory_candidates)")
    }
    expected = {
        "source_chain_json",
        "source_message_ids",
        "source_session_id",
        "source_workflow_id",
        "created_by_agent_id",
        "created_from_chat_id",
        "created_from_channel",
    }
    missing = expected - cols
    assert not missing, f"memory_candidates missing source-chain cols: {missing}"


def test_session_chain_state_has_rag_columns(storage: Database):
    """session_chain_state must be ALTERed with RAG tracking columns (M2.5 prep)."""
    cols = {
        row["name"]
        for row in storage.fetchall("PRAGMA table_info(session_chain_state)")
    }
    assert "rag_indexed_paths" in cols
    assert "rag_search_queries" in cols


def test_rag_indexes_created(storage: Database):
    """Required indexes must exist for query performance."""
    rows = storage.fetchall(
        "SELECT name FROM sqlite_master WHERE type='index'"
    )
    names = {r["name"] for r in rows}
    expected = {
        "idx_rag_items_owner",
        "idx_rag_items_scope",
        "idx_rag_items_source",
        "idx_rag_items_workspace",
        "idx_rag_chunks_item",
        "idx_rag_item_chunk_versions_active",
        "idx_rag_reindex_diffs_item",
        "idx_rag_reindex_diff_chunks_diff",
        "idx_active_contexts_session",
    }
    missing = expected - names
    assert not missing, f"missing indexes: {missing}"


def test_init_tables_is_idempotent(tmp_path: Path):
    """Calling init_tables twice on same DB must not error (CREATE IF NOT EXISTS + ALTER try/except)."""
    db_path = tmp_path / "idem.db"
    db1 = Database(db_path)
    # Manually re-init (simulates restart)
    db1.init_tables()
    db1.init_tables()
    db1.close()


# ===================== RagStore CRUD =====================


def _new_item(**overrides) -> RagItem:
    now = int(time.time())
    base = dict(
        item_id=uuid.uuid4().hex,
        namespace="context",
        source_type="document",
        scope_type="workspace",
        scope_id="ws-1",
        owner_agent_id="agent-a",
        status="active",
        created_at=now,
        updated_at=now,
        source_path="/tmp/foo.md",
        content_hash="abc123",
    )
    base.update(overrides)
    return RagItem(**base)


def test_store_insert_and_get_item(store: RagStore):
    item = _new_item()
    store.insert_item(item)
    fetched = store.get_item(item.item_id)
    assert fetched is not None
    assert fetched.item_id == item.item_id
    assert fetched.namespace == "context"
    assert fetched.active_version == 1
    assert fetched.sensitivity_level == "low"


def test_store_list_by_scope_filters(store: RagStore):
    item_a = _new_item(owner_agent_id="agent-a", namespace="context")
    item_b = _new_item(owner_agent_id="agent-b", namespace="context")
    item_m = _new_item(owner_agent_id="agent-a", namespace="memory")
    store.insert_item(item_a)
    store.insert_item(item_b)
    store.insert_item(item_m)

    only_a_context = store.list_by_scope(
        owner_agent_id="agent-a", namespace="context"
    )
    assert {i.item_id for i in only_a_context} == {item_a.item_id}


def test_store_mark_status(store: RagStore):
    item = _new_item()
    store.insert_item(item)
    store.mark_status(item.item_id, "archived")
    fetched = store.get_item(item.item_id)
    assert fetched.status == "archived"


def test_store_insert_and_get_chunks(store: RagStore):
    item = _new_item()
    store.insert_item(item)
    chunks = [
        RagChunk(
            chunk_id=f"c-{i}",
            item_id=item.item_id,
            chunk_index=i,
            content=f"chunk {i}",
            start_line=i * 10,
            end_line=i * 10 + 9,
        )
        for i in range(3)
    ]
    store.insert_chunks(chunks)
    fetched = store.get_chunks(item.item_id)
    assert len(fetched) == 3
    assert [c.chunk_index for c in fetched] == [0, 1, 2]
    assert all(c.version == 1 for c in fetched)


def test_store_chunks_filter_by_version(store: RagStore):
    """M3 reindex requires version-aware queries."""
    item = _new_item()
    store.insert_item(item)
    v1_chunk = RagChunk(
        chunk_id="v1-c0", item_id=item.item_id, chunk_index=0, content="v1", version=1
    )
    v2_chunk = RagChunk(
        chunk_id="v2-c0", item_id=item.item_id, chunk_index=0, content="v2", version=2
    )
    store.insert_chunks([v1_chunk, v2_chunk])

    v1_only = store.get_chunks(item.item_id, version=1)
    assert len(v1_only) == 1
    assert v1_only[0].content == "v1"

    v2_only = store.get_chunks(item.item_id, version=2)
    assert v2_only[0].content == "v2"


def test_store_active_context_set_and_clear(store: RagStore):
    now = int(time.time())
    ctx = ActiveContext(
        session_id="sess-1",
        agent_id="agent-a",
        context_id="item-1",
        context_type="document",
        title="LEARNING.md",
        activated_at=now,
    )
    store.set_active_context(ctx)
    fetched = store.get_active_contexts("sess-1", "agent-a")
    assert len(fetched) == 1
    assert fetched[0].context_id == "item-1"

    store.clear_active_context("sess-1", "agent-a", "item-1")
    assert store.get_active_contexts("sess-1", "agent-a") == []


def test_store_memory_candidate_lifecycle(store: RagStore):
    """Memory candidate must support pending → approved/rejected status transitions."""
    now = int(time.time())
    cand = MemoryCandidate(
        candidate_id="cand-1",
        content="user prefers Chinese responses",
        memory_type="user_preference",
        scope_type="user",
        scope_id="user-x",
        source_type="explicit",
        status="pending",
        created_at=now,
        updated_at=now,
        source_chain_json='{"source": "explicit"}',
        created_by_agent_id="agent-a",
        created_from_chat_id="chat-1",
    )
    store.insert_memory_candidate(cand)
    fetched = store.get_memory_candidate("cand-1")
    assert fetched is not None
    assert fetched.status == "pending"
    assert fetched.created_by_agent_id == "agent-a"

    store.update_candidate_status("cand-1", "approved", approval_id="ap-1")
    after = store.get_memory_candidate("cand-1")
    assert after.status == "approved"
    assert after.approval_id == "ap-1"


# ===================== Config defaults =====================


def test_rag_config_defaults_are_all_false():
    """All M1 RAG enable flags must default to False (opt-in)."""
    cfg = RagConfig()
    assert cfg.enabled is False
    assert cfg.namespaces.context_enabled is False
    assert cfg.namespaces.memory_enabled is False
    assert cfg.backend.vector_backend == "none"
    assert cfg.backend.hybrid_enabled is False
    assert cfg.retrieval.auto_context_retrieval is False
    assert cfg.retrieval.auto_memory_retrieval is False
    assert cfg.embedding.enabled is False
    assert cfg.security.allow_index_in_bypass is False
    assert cfg.security.allow_sensitive_index is False
    assert cfg.security.require_approval_for_memory_write is True
    assert cfg.sharing.allow_workspace_context_sharing is False
    assert cfg.sharing.allow_cross_agent_context is False


def test_app_config_includes_rag():
    """AppConfig must expose rag field with full default tree."""
    app = AppConfig()
    assert isinstance(app.rag, RagConfig)
    assert app.rag.enabled is False
    # Lifecycle defaults match RAG.md spec
    assert app.rag.lifecycle.warm_after_days == 7
    assert app.rag.lifecycle.delete_after_days == 180
    assert app.rag.chunk.max_tokens == 800


def test_audit_event_types_complete():
    """AUDIT_EVENT_TYPES must contain the events referenced by future milestones."""
    must_include = {
        "rag_index_completed",
        "rag_index_failed",
        "rag_search_performed",
        "rag_chain_attack_blocked",
        "memory_write_completed",
        "memory_write_policy_like_content",
        "vector_backend_error",
    }
    missing = must_include - AUDIT_EVENT_TYPES
    assert not missing, f"AUDIT_EVENT_TYPES missing: {missing}"
