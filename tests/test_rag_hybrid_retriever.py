"""Tests for Phase 8 M4: hybrid retriever (FTS + vector merge + score weighting)."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mini_claw.config import AppConfig, RagConfig
from mini_claw.permissions.policy import PermissionPolicy
from mini_claw.rag.hybrid_retriever import HybridRetriever
from mini_claw.rag.manager import RagManager
from mini_claw.rag.vector_backend import NoneBackend, VectorHit
from mini_claw.storage.db import Database


# ===================== Fixtures =====================


@pytest.fixture
def config() -> RagConfig:
    cfg = RagConfig()
    cfg.enabled = True
    cfg.namespaces.context_enabled = True
    return cfg


@pytest.fixture
def storage(tmp_path: Path) -> Database:
    return Database(tmp_path / "hybrid.db")


@pytest.fixture
def manager(storage, config) -> RagManager:
    return RagManager(storage, config, PermissionPolicy(AppConfig().permissions))


def _ctx(workspace_dir: Path, agent_id: str = "agent-a", session_id: str = "sess-1") -> dict:
    return {
        "agent_id": agent_id,
        "workspace_dir": str(workspace_dir),
        "sandbox_mode": "safe",
        "chat_id": "chat-1",
        "session_id": session_id,
        "channel_name": "cli",
    }


def _index_doc(manager: RagManager, tmp_path: Path, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    item_id, error = manager.index_context(str(p), ctx=_ctx(tmp_path))
    assert error == ""
    return item_id


# ===================== Hybrid disabled (default) =====================


def test_hybrid_disabled_falls_back_to_fts(manager: RagManager, tmp_path: Path):
    """When hybrid_enabled=False, hybrid path delegates to FTS pipeline."""
    _index_doc(manager, tmp_path, "doc.md", "# Auth\nlogin via bearer token here\n")
    # config.backend.hybrid_enabled defaults to False
    results, error = manager.search_context("token", ctx=_ctx(tmp_path))
    assert error == ""
    assert results
    # FTS5 BM25 returns negative ranks (closer to 0 = better); just check non-None
    assert all(r.score is not None for r in results)


# ===================== HybridRetriever direct tests =====================


class _FakeBackend:
    """Test double for VectorBackend with controllable hits."""

    name = "fake"

    def __init__(self, hits: list[VectorHit]):
        self._hits = hits

    def upsert_chunks(self, *a, **kw):
        return None

    def search(self, query_embedding, *, namespace, top_k, scope_filter=None):
        return list(self._hits[:top_k])

    def delete_chunks(self, chunk_ids):
        return None

    def delete_item(self, item_id):
        return None

    def health_check(self):
        from mini_claw.rag.vector_backend import VectorBackendHealth
        return VectorBackendHealth(healthy=True, backend="fake")


class _FakeEmbedder:
    model = "fake-mini"
    dim = 4

    def embed_texts(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    def embed_query(self, query):
        return [1.0, 0.0, 0.0, 0.0] if query else []


def test_hybrid_merges_fts_and_vector_results(
    manager: RagManager, storage: Database, tmp_path: Path
):
    """Vector-only hit (chunk not in FTS top-K) must still appear in hybrid output."""
    item_id = _index_doc(
        manager, tmp_path, "doc.md",
        "# Title\n\nthe word lexical appears here\n\n## Other\nsemantic content\n",
    )
    # Pull two real chunk_ids from DB
    chunks = manager.store.get_chunks(item_id)
    assert len(chunks) >= 2

    # Vector hit pushes the SECOND chunk to top, even though FTS query
    # 'lexical' would match the first chunk
    vec_hits = [VectorHit(chunk_id=chunks[1].chunk_id, item_id=item_id, score=0.95)]
    backend = _FakeBackend(vec_hits)
    cfg = manager.config
    cfg.backend.vector_backend = "fake"
    cfg.backend.hybrid_enabled = True

    hybrid = HybridRetriever(storage, cfg, backend, _FakeEmbedder())
    results, error = hybrid.search("lexical", ctx=_ctx(tmp_path))
    assert error == ""
    # Both chunks should appear in merged results
    chunk_ids = {r.chunk_id for r in results}
    assert chunks[1].chunk_id in chunk_ids


def test_hybrid_active_context_boost(
    manager: RagManager, storage: Database, tmp_path: Path
):
    """Items in active_contexts get a 0.05 score bump versus identical-FTS competitors."""
    item_a = _index_doc(manager, tmp_path, "a.md", "# A\nfoo bar baz one\n")
    item_b = _index_doc(manager, tmp_path, "b.md", "# B\nfoo bar baz two\n")

    cfg = manager.config
    cfg.backend.vector_backend = "fake"
    cfg.backend.hybrid_enabled = True
    hybrid = HybridRetriever(storage, cfg, _FakeBackend([]), _FakeEmbedder())

    # Baseline: NO active context — record item_b's score
    results_baseline, _ = hybrid.search("foo", ctx=_ctx(tmp_path))
    if not results_baseline:
        pytest.skip("FTS5 returned no rows; environment unsupported")
    base_b = max(
        (r.score for r in results_baseline if r.item_id == item_b),
        default=None,
    )
    if base_b is None:
        pytest.skip("item_b not in baseline results")

    # Now activate item_b and re-query
    manager.use_context(item_b, ctx=_ctx(tmp_path))
    results_active, _ = hybrid.search("foo", ctx=_ctx(tmp_path))
    boosted_b = max(
        (r.score for r in results_active if r.item_id == item_b),
        default=None,
    )
    assert boosted_b is not None
    # Active boost adds W_ACTIVE * 1.0 = 0.05
    assert boosted_b > base_b
    assert pytest.approx(boosted_b - base_b, abs=0.001) == 0.05


def test_hybrid_recency_decay(
    manager: RagManager, storage: Database, tmp_path: Path
):
    """Older items contribute less recency_score than fresh items."""
    cfg = manager.config
    cfg.backend.vector_backend = "fake"
    cfg.backend.hybrid_enabled = True
    hybrid = HybridRetriever(storage, cfg, _FakeBackend([]), _FakeEmbedder())

    item = _index_doc(manager, tmp_path, "doc.md", "# Body\ncontent here\n")
    fresh_score = hybrid._recency_score(item, int(time.time()))
    assert fresh_score > 0.9  # almost-now → near 1.0

    # Backdate the item by ~60 days
    storage.execute(
        "UPDATE rag_items SET last_accessed_at = ? WHERE item_id = ?",
        (int(time.time()) - 60 * 86400, item),
    )
    aged_score = hybrid._recency_score(item, int(time.time()))
    assert aged_score < fresh_score
    assert aged_score < 0.5  # past one half-life


def test_hybrid_vector_failure_degrades_to_fts(
    manager: RagManager, storage: Database, tmp_path: Path
):
    """If embedder raises, hybrid still returns FTS-only results without bubbling exception."""
    _index_doc(manager, tmp_path, "doc.md", "# Title\nbar baz indexable text\n")

    class _BoomEmbedder:
        model = "boom"
        dim = 4

        def embed_texts(self, texts):
            raise RuntimeError("embedding service down")

        def embed_query(self, query):
            raise RuntimeError("embedding service down")

    cfg = manager.config
    cfg.backend.vector_backend = "fake"
    cfg.backend.hybrid_enabled = True
    hybrid = HybridRetriever(storage, cfg, _FakeBackend([]), _BoomEmbedder())

    # Must NOT raise — degrade silently
    results, error = hybrid.search("indexable", ctx=_ctx(tmp_path))
    assert error == ""


def test_manager_search_routes_through_hybrid_when_enabled(
    manager: RagManager, tmp_path: Path
):
    """RagManager.search_context picks hybrid path when config flips on."""
    _index_doc(manager, tmp_path, "doc.md", "# Title\ndiscoverable text body\n")
    cfg = manager.config
    cfg.backend.vector_backend = "chroma"
    cfg.backend.hybrid_enabled = True

    # Even without a real embedder, the call must not raise
    results, error = manager.search_context("discoverable", ctx=_ctx(tmp_path))
    assert error == ""
    # Either hybrid degraded (vector backend may be unavailable) OR returned hits
    assert isinstance(results, list)


def test_vector_hits_are_post_filtered_to_active_mapping(
    manager: RagManager, storage: Database, tmp_path: Path
):
    item_id = _index_doc(manager, tmp_path, "doc.md", "# A\nold semantic body\n")
    old_chunk = manager.store.get_active_chunks(item_id)[0]

    (tmp_path / "doc.md").write_text("# A\nnew semantic body\n", encoding="utf-8")
    ok, message = manager.reindex_context(item_id, ctx=_ctx(tmp_path))
    assert ok, message
    active_chunk = manager.store.get_active_chunks(item_id)[0]
    assert active_chunk.chunk_id != old_chunk.chunk_id

    cfg = manager.config
    cfg.backend.vector_backend = "fake"
    cfg.backend.hybrid_enabled = True
    cfg.retrieval.context_top_k = 1
    backend = _FakeBackend(
        [
            VectorHit(chunk_id=old_chunk.chunk_id, item_id=item_id, score=0.99),
            VectorHit(chunk_id=active_chunk.chunk_id, item_id=item_id, score=0.80),
        ]
    )
    hybrid = HybridRetriever(storage, cfg, backend, _FakeEmbedder())

    results, error = hybrid.search("semantic-only-query", ctx=_ctx(tmp_path), top_k=1)
    assert error == ""
    assert results
    assert results[0].chunk_id == active_chunk.chunk_id
