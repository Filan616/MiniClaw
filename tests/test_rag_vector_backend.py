"""Tests for Phase 8 M4: vector backend (NoneBackend always; ChromaBackend when installed)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mini_claw.config import RagConfig
from mini_claw.rag.vector_backend import (
    ChromaBackend,
    NoneBackend,
    VectorBackend,
    VectorBackendError,
    VectorBackendHealth,
    VectorHit,
    build_vector_backend,
)


# ===================== NoneBackend (always available) =====================


def test_none_backend_satisfies_protocol():
    b = NoneBackend()
    assert isinstance(b, VectorBackend)
    assert b.name == "none"


def test_none_backend_search_returns_empty():
    b = NoneBackend()
    results = b.search([0.1, 0.2, 0.3], namespace="context", top_k=5)
    assert results == []


def test_none_backend_upsert_is_noop():
    b = NoneBackend()
    chunk = SimpleNamespace(
        chunk_id="c1", item_id="i1", chunk_index=0, version=1,
        content="hello", section_title=None, symbol_name=None, language=None,
    )
    b.upsert_chunks([chunk], [[0.1, 0.2]], namespace="context", source_type="document")


def test_none_backend_health_is_always_healthy():
    b = NoneBackend()
    health = b.health_check()
    assert isinstance(health, VectorBackendHealth)
    assert health.healthy is True
    assert health.backend == "none"


def test_none_backend_delete_is_noop():
    b = NoneBackend()
    b.delete_chunks(["c1", "c2"])
    b.delete_item("i1")


# ===================== Factory =====================


def test_build_vector_backend_default_returns_none():
    cfg = RagConfig()
    b = build_vector_backend(cfg)
    assert isinstance(b, NoneBackend)


def test_build_vector_backend_milvus_falls_back_to_none():
    cfg = RagConfig()
    cfg.backend.vector_backend = "milvus"
    b = build_vector_backend(cfg)
    assert isinstance(b, NoneBackend)


def test_build_vector_backend_chroma_returns_chroma_or_falls_back(tmp_path: Path):
    cfg = RagConfig()
    cfg.backend.vector_backend = "chroma"
    cfg.chroma.persist_dir = str(tmp_path / "chroma")
    b = build_vector_backend(cfg)
    # If chromadb is installed, ChromaBackend; otherwise NoneBackend fallback
    assert isinstance(b, (ChromaBackend, NoneBackend))


# ===================== ChromaBackend (skip if not installed) =====================


def _have_chromadb() -> bool:
    try:
        import chromadb  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _have_chromadb(), reason="chromadb not installed")
def test_chroma_backend_upsert_and_search(tmp_path: Path):
    b = ChromaBackend(
        persist_dir=str(tmp_path / "chroma"),
        collection_prefix="testminiclaw",
    )

    chunks = [
        SimpleNamespace(
            chunk_id="c-auth-1",
            item_id="i-auth",
            chunk_index=0,
            version=1,
            content="how to log in with bearer token",
            section_title="Authentication",
            symbol_name=None,
            language=None,
        ),
        SimpleNamespace(
            chunk_id="c-pay-1",
            item_id="i-pay",
            chunk_index=0,
            version=1,
            content="charge a credit card via Stripe API",
            section_title="Payments",
            symbol_name=None,
            language=None,
        ),
    ]
    # Tiny embeddings — 4 dims, deterministic
    vecs = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]
    b.upsert_chunks(chunks, vecs, namespace="context", source_type="document")

    # Query close to the first vector → should rank c-auth-1 first
    results = b.search([0.9, 0.1, 0.0, 0.0], namespace="context", top_k=2)
    assert results
    assert results[0].chunk_id == "c-auth-1"


@pytest.mark.skipif(not _have_chromadb(), reason="chromadb not installed")
def test_chroma_backend_delete_item(tmp_path: Path):
    b = ChromaBackend(
        persist_dir=str(tmp_path / "chroma_del"),
        collection_prefix="testminiclaw",
    )
    chunk = SimpleNamespace(
        chunk_id="c-x", item_id="i-doomed",
        chunk_index=0, version=1, content="anything",
        section_title=None, symbol_name=None, language=None,
    )
    b.upsert_chunks([chunk], [[0.5, 0.5, 0.0, 0.0]],
                    namespace="context", source_type="document")
    b.delete_item("i-doomed")
    # Search still returns nothing for doomed item
    results = b.search([0.5, 0.5, 0.0, 0.0], namespace="context", top_k=5)
    ids = {r.chunk_id for r in results}
    assert "c-x" not in ids


def test_chroma_health_when_chromadb_missing():
    """Even when chromadb isn't installed, health_check returns a struct."""
    b = ChromaBackend(persist_dir="/nonexistent/should-not-init")
    # Don't trigger ensure_client on this call — health_check is pure read
    health = b.health_check()
    assert isinstance(health, VectorBackendHealth)
