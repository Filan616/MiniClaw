"""Tests for Phase 8 M4: embeddings provider abstraction and cache.

Heavy deps (sentence-transformers, openai) are NOT loaded here; tests use
a fake provider so the suite stays fast and works without ``[rag-vector]``
installed.
"""

from __future__ import annotations

from typing import Any

import pytest

from mini_claw.config import RagConfig
from mini_claw.rag.embeddings import (
    EmbeddingError,
    EmbeddingProvider,
    LocalSentenceTransformerProvider,
    OpenAIEmbeddingProvider,
    clear_query_cache,
    embed_with_cache,
    get_embedding_provider,
)


class _FakeEmbedder:
    """Deterministic stand-in for an EmbeddingProvider."""

    model = "fake-mini"
    dim = 4

    def __init__(self) -> None:
        self.calls = 0

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[float(len(t)), float(t.count(" ")), 0.1, 0.0] for t in texts]

    def embed_query(self, query: str) -> list[float]:
        return self.embed_texts([query])[0] if query else []


# ===================== Protocol conformance =====================


def test_fake_embedder_satisfies_protocol():
    fake = _FakeEmbedder()
    assert isinstance(fake, EmbeddingProvider)


def test_local_provider_construct_does_not_load_model():
    """LocalSentenceTransformerProvider must lazy-load: just constructing it
    cannot raise even if sentence-transformers is not installed."""
    p = LocalSentenceTransformerProvider(model="fake-model")
    assert p.model == "fake-model"
    # accessing dim triggers load — we don't call it in this test


def test_openai_provider_construct_does_not_call_api():
    p = OpenAIEmbeddingProvider(model="text-embedding-3-small", dim=1536)
    assert p.model == "text-embedding-3-small"
    assert p.dim == 1536


def test_openai_provider_missing_api_key_raises_on_first_use(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    p = OpenAIEmbeddingProvider(api_key_env="OPENAI_API_KEY")
    with pytest.raises(EmbeddingError):
        p.embed_query("hello")


# ===================== Factory =====================


def test_factory_returns_none_when_disabled():
    cfg = RagConfig()  # embedding.enabled defaults to False
    assert get_embedding_provider(cfg) is None


def test_factory_local_returns_provider():
    cfg = RagConfig()
    cfg.embedding.enabled = True
    cfg.embedding.provider = "local"
    cfg.embedding.model = "any-model"
    p = get_embedding_provider(cfg)
    assert isinstance(p, LocalSentenceTransformerProvider)
    assert p.model == "any-model"


def test_factory_openai_returns_provider():
    cfg = RagConfig()
    cfg.embedding.enabled = True
    cfg.embedding.provider = "openai"
    cfg.embedding.model = "text-embedding-3-small"
    cfg.embedding.dim = 1536
    p = get_embedding_provider(cfg)
    assert isinstance(p, OpenAIEmbeddingProvider)


def test_factory_unknown_provider_raises():
    cfg = RagConfig()
    cfg.embedding.enabled = True
    cfg.embedding.provider = "totally-fake"
    with pytest.raises(EmbeddingError):
        get_embedding_provider(cfg)


# ===================== Query cache =====================


def test_embed_with_cache_hits_after_first_call():
    clear_query_cache()
    fake = _FakeEmbedder()
    v1 = embed_with_cache(fake, "find the auth bug")
    v2 = embed_with_cache(fake, "find the auth bug")
    assert v1 == v2
    # Second call should NOT increment provider.calls
    assert fake.calls == 1


def test_embed_with_cache_keyed_by_model_and_query():
    clear_query_cache()
    a = _FakeEmbedder()
    b = _FakeEmbedder()
    b.model = "different-model"
    v_a = embed_with_cache(a, "test")
    v_b = embed_with_cache(b, "test")
    # Both providers produced 1 call each (cache key includes model)
    assert a.calls == 1
    assert b.calls == 1
    # Same input → cache returns same vector for same provider
    assert embed_with_cache(a, "test") == v_a


def test_embed_with_cache_empty_query_returns_empty():
    clear_query_cache()
    fake = _FakeEmbedder()
    assert embed_with_cache(fake, "") == []
    assert fake.calls == 0


def test_embed_with_cache_eviction_under_pressure():
    clear_query_cache()
    fake = _FakeEmbedder()
    # Fill more than _QUERY_CACHE_MAX (256) to force eviction
    for i in range(300):
        embed_with_cache(fake, f"query-{i}")
    # Cache must not grow unbounded
    from mini_claw.rag.embeddings import _QUERY_CACHE, _QUERY_CACHE_MAX
    assert len(_QUERY_CACHE) <= _QUERY_CACHE_MAX
