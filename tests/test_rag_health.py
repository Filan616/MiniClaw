"""Tests for Phase 8 M4.5: RAG health observability + degradation surfacing."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mini_claw.config import AppConfig, RagConfig
from mini_claw.permissions.policy import PermissionPolicy
from mini_claw.rag.health import RagHealthManager
from mini_claw.rag.manager import RagManager
from mini_claw.rag.models import RagComponentStatus, RagStatus
from mini_claw.rag.vector_backend import (
    NoneBackend,
    VectorBackendHealth,
)
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
    return Database(tmp_path / "health.db")


@pytest.fixture
def manager(storage, config) -> RagManager:
    return RagManager(storage, config, PermissionPolicy(AppConfig().permissions))


def _ctx(workspace_dir: Path, agent_id: str = "agent-a") -> dict:
    return {
        "agent_id": agent_id,
        "workspace_dir": str(workspace_dir),
        "sandbox_mode": "safe",
        "chat_id": "chat-1",
        "session_id": "sess-1",
        "channel_name": "cli",
    }


def _index(manager: RagManager, tmp_path: Path, name: str = "doc.md") -> str:
    p = tmp_path / name
    p.write_text("# title\nbody\n", encoding="utf-8")
    item_id, _ = manager.index_context(str(p), ctx=_ctx(tmp_path))
    return item_id


# ===================== check_fts =====================


def test_check_fts_ok_on_clean_index(manager: RagManager, tmp_path: Path):
    _index(manager, tmp_path)
    status = manager.health.check_fts()
    assert status.status == "ok"
    assert status.last_ok_at is not None
    assert status.details["rag_chunks_active"] == status.details["rag_chunks_fts"]


def test_check_fts_degraded_on_row_mismatch(
    manager: RagManager, storage: Database, tmp_path: Path
):
    """Drop one FTS row to simulate divergence."""
    item_id = _index(manager, tmp_path)
    chunks = manager.store.get_chunks(item_id)
    storage.execute(
        "DELETE FROM rag_chunks_fts WHERE chunk_id = ?", (chunks[0].chunk_id,)
    )
    status = manager.health.check_fts()
    assert status.status == "degraded"
    assert "mismatch" in (status.last_error or "").lower()


# ===================== check_vector_backend =====================


def test_check_vector_backend_none_is_ok(manager: RagManager):
    """NoneBackend (default) reports healthy."""
    status = manager.health.check_vector_backend()
    assert status.status == "ok"
    assert status.component == "none"


def test_check_vector_backend_handles_raising_backend(
    storage: Database, config: RagConfig
):
    """If the backend's health_check raises, report failed gracefully."""

    class _BoomBackend:
        name = "boom"

        def health_check(self):
            raise RuntimeError("kaboom")

        def upsert_chunks(self, *a, **kw):
            return None

        def search(self, *a, **kw):
            return []

        def delete_chunks(self, *a, **kw):
            return None

        def delete_item(self, *a, **kw):
            return None

    h = RagHealthManager(storage, config, _BoomBackend(), embedder=None)
    status = h.check_vector_backend()
    assert status.status == "failed"
    assert "kaboom" in (status.last_error or "")


def test_check_vector_backend_degraded_when_unhealthy(
    storage: Database, config: RagConfig
):
    """Backend reports unhealthy → mapped to degraded."""

    class _SickBackend:
        name = "chroma"

        def health_check(self):
            return VectorBackendHealth(
                healthy=False,
                last_error="chromadb upsert failed: connection refused",
                backend="chroma",
            )

        def upsert_chunks(self, *a, **kw):
            return None

        def search(self, *a, **kw):
            return []

        def delete_chunks(self, *a, **kw):
            return None

        def delete_item(self, *a, **kw):
            return None

    h = RagHealthManager(storage, config, _SickBackend(), embedder=None)
    status = h.check_vector_backend()
    assert status.status == "degraded"
    assert "connection refused" in (status.last_error or "")


# ===================== check_embedding =====================


def test_check_embedding_ok_when_disabled(manager: RagManager):
    """embedding.enabled=False → ok by design."""
    status = manager.health.check_embedding()
    assert status.status == "ok"
    assert status.details.get("enabled") is False


def test_check_embedding_failed_when_provider_raises(
    storage: Database, config: RagConfig
):
    """A real embedder that raises EmbeddingError → status='failed'."""
    config.embedding.enabled = True
    config.embedding.provider = "local"

    from mini_claw.rag.embeddings import EmbeddingError

    class _BoomEmbedder:
        model = "boom"
        dim = 4

        def embed_query(self, q):
            raise EmbeddingError("model file missing")

        def embed_texts(self, texts):
            raise EmbeddingError("model file missing")

    h = RagHealthManager(storage, config, NoneBackend(), embedder=_BoomEmbedder())
    status = h.check_embedding()
    assert status.status == "failed"
    assert "model file missing" in (status.last_error or "")


def test_check_embedding_ok_when_provider_works(
    storage: Database, config: RagConfig
):
    config.embedding.enabled = True

    class _FakeEmbedder:
        model = "fake-mini"
        dim = 4

        def embed_query(self, q):
            return [0.1, 0.2, 0.3, 0.4]

        def embed_texts(self, texts):
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    h = RagHealthManager(storage, config, NoneBackend(), embedder=_FakeEmbedder())
    status = h.check_embedding()
    assert status.status == "ok"
    assert status.details["dim"] == 4


# ===================== Counters =====================


def test_count_stale_orphan(manager: RagManager, tmp_path: Path):
    a = _index(manager, tmp_path, "a.md")
    b = _index(manager, tmp_path, "b.md")
    manager.store.mark_stale(a)
    manager.store.mark_orphan(b)
    stale, orphan = manager.health.count_stale_orphan()
    assert stale == 1
    assert orphan == 1


def test_count_pending_candidates(manager: RagManager, storage: Database):
    """memory_candidates rows count toward pending."""
    storage.execute(
        "INSERT INTO memory_candidates ("
        "candidate_id, content, memory_type, scope_type, scope_id, "
        "source_type, source_chain_json, created_by_agent_id, created_from_chat_id, "
        "status, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, 0)",
        ("cand-1", "x", "user_preference", "user", "u1", "explicit", "{}", "a", "c"),
    )
    assert manager.health.count_pending_candidates() == 1


def test_count_abandoned_reindex_versions(
    manager: RagManager, storage: Database, tmp_path: Path
):
    """Chunks with version != active_version are abandoned orphans."""
    item_id = _index(manager, tmp_path)
    # Forge a chunk on version 9 while active_version stays at 1
    storage.execute(
        "INSERT INTO rag_chunks (chunk_id, item_id, chunk_index, content, version) "
        "VALUES (?, ?, ?, ?, ?)",
        (f"{item_id}-v9-0", item_id, 0, "stale body", 9),
    )
    assert manager.health.count_abandoned_reindex_versions() >= 1


def test_count_delete_failed(manager: RagManager, tmp_path: Path):
    item = _index(manager, tmp_path)
    manager.store.mark_status(item, "delete_failed", error="vector down")
    assert manager.health.count_delete_failed() == 1


# ===================== summarize / render / dict =====================


def test_summarize_returns_complete_status(manager: RagManager, tmp_path: Path):
    _index(manager, tmp_path)
    s = manager.status()
    assert isinstance(s, RagStatus)
    assert s.enabled is True
    assert s.fts.status == "ok"
    assert isinstance(s.chroma, RagComponentStatus)
    assert isinstance(s.embedding, RagComponentStatus)


def test_render_text_includes_all_sections(manager: RagManager, tmp_path: Path):
    _index(manager, tmp_path)
    text = manager.status_text()
    assert "RAG Status" in text
    assert "FTS5" in text
    assert "Vector backend" in text
    assert "Embedding" in text
    assert "Stale items" in text
    assert "Active fallback" in text


def test_status_dict_is_json_serializable(manager: RagManager, tmp_path: Path):
    import json
    _index(manager, tmp_path)
    payload = manager.status_dict()
    encoded = json.dumps(payload)
    decoded = json.loads(encoded)
    assert decoded["enabled"] is True
    assert "fts" in decoded
    assert "vector_backend" in decoded
    assert "embedding" in decoded
    assert "active_fallback" in decoded


# ===================== active_fallback inference =====================


def test_active_fallback_says_disabled_when_no_vector_backend(manager: RagManager):
    """Default config: vector_backend='none' → fallback message says disabled."""
    s = manager.status()
    assert "FTS-only" in s.active_fallback
    assert "disabled" in s.active_fallback


def test_active_fallback_says_hybrid_when_everything_ok(
    storage: Database, config: RagConfig
):
    """vector_backend != none + healthy backend + working embedder → hybrid."""
    config.backend.vector_backend = "chroma"

    class _FineBackend:
        name = "chroma"

        def health_check(self):
            return VectorBackendHealth(healthy=True, backend="chroma", last_ok_at=1)

        def upsert_chunks(self, *a, **kw):
            return None

        def search(self, *a, **kw):
            return []

        def delete_chunks(self, *a, **kw):
            return None

        def delete_item(self, *a, **kw):
            return None

    h = RagHealthManager(storage, config, _FineBackend(), embedder=None)
    s = h.summarize()
    assert "hybrid" in s.active_fallback.lower()


def test_active_fallback_says_fts_only_when_vector_degraded(
    storage: Database, config: RagConfig
):
    """vector_backend configured but unhealthy → FTS-only with reason."""
    config.backend.vector_backend = "chroma"

    class _SickBackend:
        name = "chroma"

        def health_check(self):
            return VectorBackendHealth(
                healthy=False, backend="chroma",
                last_error="connection refused",
            )

        def upsert_chunks(self, *a, **kw):
            return None

        def search(self, *a, **kw):
            return []

        def delete_chunks(self, *a, **kw):
            return None

        def delete_item(self, *a, **kw):
            return None

    h = RagHealthManager(storage, config, _SickBackend(), embedder=None)
    s = h.summarize()
    assert "FTS-only" in s.active_fallback
    assert "connection refused" in s.active_fallback


# ===================== Disabled state =====================


def test_status_when_rag_disabled(tmp_path: Path):
    """RAG disabled → status still works, enabled=False."""
    cfg = RagConfig()  # enabled=False default
    db = Database(tmp_path / "off.db")
    mgr = RagManager(db, cfg, PermissionPolicy(AppConfig().permissions))
    s = mgr.status()
    assert s.enabled is False
