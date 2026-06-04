"""Embedding provider abstraction (Phase 8 M4).

Pluggable embedder. M4 ships:
- LocalSentenceTransformerProvider (lazy import sentence-transformers)
- OpenAIEmbeddingProvider (lazy import openai)

Use ``get_embedding_provider(config)`` to construct from RagConfig.

Caching: all providers share an in-memory LRU on ``embed_query`` to avoid
recomputing the same query embedding multiple times within a session
(common with auto-retrieval). ``embed_texts`` is intended for batch
indexing and is not cached (typically called once per chunk).
"""

from __future__ import annotations

import functools
import hashlib
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "EmbeddingProvider",
    "LocalSentenceTransformerProvider",
    "OpenAIEmbeddingProvider",
    "get_embedding_provider",
    "EmbeddingError",
]


class EmbeddingError(Exception):
    """Raised when an embedding provider fails (network, missing dep, etc.)."""


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Protocol for embedders. ``model`` and ``dim`` are introspected by store."""

    model: str
    dim: int

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Batch-embed multiple texts; returns one vector per input."""
        ...

    def embed_query(self, query: str) -> list[float]:
        """Embed a single query string. Cached by callers via embed_with_cache()."""
        ...


# ====================================================================
# Local sentence-transformers
# ====================================================================


class LocalSentenceTransformerProvider:
    """Local sentence-transformers backend.

    Lazy-imports ``sentence_transformers`` so MiniClaw default install
    does not require this dependency. Install via ``pip install -e .[rag-vector]``.
    """

    def __init__(self, model: str = "sentence-transformers/all-MiniLM-L6-v2"):
        self.model = model
        self._st_model: Any = None
        self._dim: int | None = None

    def _ensure_loaded(self) -> None:
        if self._st_model is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise EmbeddingError(
                "sentence-transformers not installed. "
                "Install with: pip install -e '.[rag-vector]'"
            ) from exc
        try:
            self._st_model = SentenceTransformer(self.model)
            # Probe dim with a 1-token encode
            sample = self._st_model.encode(["hi"], convert_to_numpy=True)
            self._dim = int(sample.shape[1])
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingError(
                f"failed to load sentence-transformers model {self.model!r}: {exc}"
            ) from exc

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._ensure_loaded()
        return self._dim or 0

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        self._ensure_loaded()
        try:
            arr = self._st_model.encode(texts, convert_to_numpy=True)
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingError(f"embed_texts failed: {exc}") from exc
        return arr.tolist()

    def embed_query(self, query: str) -> list[float]:
        return self.embed_texts([query])[0] if query else []


# ====================================================================
# OpenAI
# ====================================================================


class OpenAIEmbeddingProvider:
    """OpenAI text-embedding-3-* backend.

    Lazy-imports ``openai`` so MiniClaw default install does not require it.
    Reads API key from the environment variable referenced in config.
    """

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        dim: int = 1536,
        api_key_env: str = "OPENAI_API_KEY",
    ):
        self.model = model
        self.dim = dim
        self._api_key_env = api_key_env
        self._client: Any = None

    def _ensure_client(self) -> None:
        if self._client is not None:
            return
        try:
            import os
            from openai import OpenAI
        except ImportError as exc:
            raise EmbeddingError(
                "openai SDK not installed. Install with: pip install openai"
            ) from exc
        api_key = os.environ.get(self._api_key_env)
        if not api_key:
            raise EmbeddingError(
                f"environment variable {self._api_key_env} is empty; "
                "OpenAI embeddings require an API key"
            )
        self._client = OpenAI(api_key=api_key)

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        self._ensure_client()
        try:
            resp = self._client.embeddings.create(input=texts, model=self.model)
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingError(f"OpenAI embed_texts failed: {exc}") from exc
        return [d.embedding for d in resp.data]

    def embed_query(self, query: str) -> list[float]:
        return self.embed_texts([query])[0] if query else []


# ====================================================================
# Factory + cache
# ====================================================================


def get_embedding_provider(config: Any) -> EmbeddingProvider | None:
    """Build a provider from ``RagConfig.embedding`` or return None if disabled.

    Failures during construction are deferred to first use — we don't want
    a flaky model load to crash app startup.
    """
    emb = getattr(config, "embedding", None)
    if emb is None or not getattr(emb, "enabled", False):
        return None
    provider_kind = (emb.provider or "local").lower()
    if provider_kind == "local":
        return LocalSentenceTransformerProvider(model=emb.model)
    if provider_kind == "openai":
        return OpenAIEmbeddingProvider(model=emb.model, dim=emb.dim)
    raise EmbeddingError(f"unknown embedding provider: {provider_kind!r}")


@functools.lru_cache(maxsize=512)
def _cached_query_hash(provider_id: str, query_hash: str) -> None:
    """LRU placeholder; actual cache logic is in ``embed_with_cache``."""
    return None


_QUERY_CACHE: dict[tuple[str, str], list[float]] = {}
_QUERY_CACHE_MAX = 256


def embed_with_cache(provider: EmbeddingProvider, query: str) -> list[float]:
    """Memoize ``provider.embed_query(query)`` keyed on ``(model, hash(query))``.

    Used by retriever for repeated queries within a session. The cache is
    process-local; restart clears it. Bounded by ``_QUERY_CACHE_MAX``.
    """
    if not query:
        return []
    key = (provider.model, hashlib.sha256(query.encode("utf-8")).hexdigest()[:16])
    cached = _QUERY_CACHE.get(key)
    if cached is not None:
        return cached
    vec = provider.embed_query(query)
    if len(_QUERY_CACHE) >= _QUERY_CACHE_MAX:
        # Evict oldest insertion (dict in Py3.7+ preserves order)
        _QUERY_CACHE.pop(next(iter(_QUERY_CACHE)))
    _QUERY_CACHE[key] = vec
    return vec


def clear_query_cache() -> None:
    """Test helper: reset the in-memory query embedding cache."""
    _QUERY_CACHE.clear()
