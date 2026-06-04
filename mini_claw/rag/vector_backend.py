"""Vector backend abstraction (Phase 8 M4).

Provides a thin Protocol over vector stores so RagManager can swap between:
- ``NoneBackend`` (default; vector-disabled, pure FTS)
- ``ChromaBackend`` (lazy-import chromadb)
- ``MilvusBackend`` (future; placeholder)

Collection naming: ``{prefix}_{namespace}_{source_type}`` so context and
memory are stored in physically separate collections (RAG.md §11.3).

Health: backends never raise from a query path — failures should set
``healthy=False`` and let the caller fall back to FTS-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "VectorHit",
    "VectorBackendHealth",
    "VectorBackend",
    "NoneBackend",
    "ChromaBackend",
    "VectorBackendError",
    "build_vector_backend",
]


class VectorBackendError(Exception):
    """Raised when a vector backend cannot be constructed (e.g. import fail)."""


@dataclass(slots=True)
class VectorHit:
    """One result from ``VectorBackend.search``."""

    chunk_id: str
    item_id: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class VectorBackendHealth:
    """Backend status surface for ``RagHealthManager`` (M4.5)."""

    healthy: bool
    last_ok_at: int | None = None
    last_error: str | None = None
    backend: str = "none"


@runtime_checkable
class VectorBackend(Protocol):
    """Vector store interface."""

    name: str

    def upsert_chunks(
        self,
        chunks: list[Any],
        embeddings: list[list[float]],
        *,
        namespace: str,
        source_type: str,
    ) -> None: ...

    def search(
        self,
        query_embedding: list[float],
        *,
        namespace: str,
        top_k: int,
        scope_filter: dict[str, Any] | None = None,
    ) -> list[VectorHit]: ...

    def delete_chunks(self, chunk_ids: list[str]) -> None: ...

    def delete_item(self, item_id: str) -> None: ...

    def health_check(self) -> VectorBackendHealth: ...


# ====================================================================
# NoneBackend (always-available stub)
# ====================================================================


class NoneBackend:
    """No-op backend used when ``rag.backend.vector_backend == 'none'``.

    Every method is a noop; ``search`` returns an empty list. Lets callers
    write the vector-aware code path unconditionally and have it degrade
    gracefully when vectors are off.
    """

    name = "none"

    def upsert_chunks(self, chunks, embeddings, *, namespace, source_type):
        return None

    def search(self, query_embedding, *, namespace, top_k, scope_filter=None):
        return []

    def delete_chunks(self, chunk_ids):
        return None

    def delete_item(self, item_id):
        return None

    def health_check(self) -> VectorBackendHealth:
        return VectorBackendHealth(healthy=True, backend="none")


# ====================================================================
# ChromaBackend
# ====================================================================


class ChromaBackend:
    """Chroma persistent client backend.

    Collections are auto-created on first upsert. Failed initialization
    does not raise from query path — instead ``health_check`` reports
    the error, and ``search`` returns an empty list.
    """

    name = "chroma"

    def __init__(
        self,
        persist_dir: str = "./data/chroma",
        collection_prefix: str = "miniclaw",
    ):
        self.persist_dir = persist_dir
        self.collection_prefix = collection_prefix
        self._client: Any = None
        self._collections: dict[str, Any] = {}
        self._healthy = True
        self._last_error: str | None = None
        self._last_ok_at: int | None = None

    def _ensure_client(self) -> None:
        if self._client is not None:
            return
        try:
            import chromadb
        except ImportError as exc:
            self._healthy = False
            self._last_error = f"chromadb not installed: {exc}"
            raise VectorBackendError(self._last_error) from exc
        try:
            self._client = chromadb.PersistentClient(path=self.persist_dir)
        except Exception as exc:  # noqa: BLE001
            self._healthy = False
            self._last_error = f"chromadb init failed: {exc}"
            raise VectorBackendError(self._last_error) from exc

    def _collection_name(self, namespace: str, source_type: str) -> str:
        # Normalize: chroma collection names must match [a-zA-Z0-9._-]
        ns = "".join(c if c.isalnum() else "_" for c in namespace)
        st = "".join(c if c.isalnum() else "_" for c in source_type)
        return f"{self.collection_prefix}_{ns}_{st}"

    def _get_collection(self, namespace: str, source_type: str):
        name = self._collection_name(namespace, source_type)
        if name in self._collections:
            return self._collections[name]
        self._ensure_client()
        try:
            coll = self._client.get_or_create_collection(name=name)
            self._collections[name] = coll
            return coll
        except Exception as exc:  # noqa: BLE001
            self._healthy = False
            self._last_error = f"get_or_create_collection({name}) failed: {exc}"
            raise VectorBackendError(self._last_error) from exc

    def upsert_chunks(
        self,
        chunks: list[Any],
        embeddings: list[list[float]],
        *,
        namespace: str,
        source_type: str,
    ) -> None:
        if not chunks or not embeddings or len(chunks) != len(embeddings):
            return
        try:
            coll = self._get_collection(namespace, source_type)
        except VectorBackendError:
            # Already recorded in health; do not raise upward
            return

        ids = [c.chunk_id for c in chunks]
        documents = [c.content for c in chunks]
        metadatas = [
            {
                "item_id": c.item_id,
                "chunk_index": c.chunk_index,
                "version": c.version,
                "chunk_id": c.chunk_id,
                "anchor_id": getattr(c, "anchor_id", "") or "",
                "source_path": getattr(c, "source_path", "") or "",
                "section_title": c.section_title or "",
                "symbol_name": c.symbol_name or "",
                "language": c.language or "",
            }
            for c in chunks
        ]
        try:
            coll.upsert(
                ids=ids,
                embeddings=embeddings,
                documents=documents,
                metadatas=metadatas,
            )
            import time
            self._healthy = True
            self._last_ok_at = int(time.time())
        except Exception as exc:  # noqa: BLE001
            self._healthy = False
            self._last_error = f"chroma upsert failed: {exc}"

    def search(
        self,
        query_embedding: list[float],
        *,
        namespace: str,
        top_k: int,
        scope_filter: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        if not query_embedding:
            return []
        # Search every source_type collection in the namespace and merge.
        # Cheap because per-collection lookups are bounded; we don't know
        # source_type at query time.
        results: list[VectorHit] = []
        try:
            self._ensure_client()
        except VectorBackendError:
            return []
        for source_type in ("document", "code", "log"):
            try:
                coll = self._get_collection(namespace, source_type)
            except VectorBackendError:
                continue
            try:
                where = self._build_where(scope_filter)
                resp = coll.query(
                    query_embeddings=[query_embedding],
                    n_results=top_k,
                    where=where if where else None,
                )
            except Exception as exc:  # noqa: BLE001
                self._healthy = False
                self._last_error = f"chroma query failed: {exc}"
                continue
            ids_lists = resp.get("ids") or []
            dist_lists = resp.get("distances") or []
            meta_lists = resp.get("metadatas") or []
            if not ids_lists:
                continue
            for chunk_id, dist, meta in zip(
                ids_lists[0], dist_lists[0] if dist_lists else [],
                meta_lists[0] if meta_lists else [],
            ):
                # Chroma returns L2 distances; convert to similarity score.
                # Lower distance = higher score. Use 1/(1+d) for stable [0,1].
                score = 1.0 / (1.0 + float(dist or 0))
                results.append(
                    VectorHit(
                        chunk_id=str(chunk_id),
                        item_id=str(meta.get("item_id", "")) if meta else "",
                        score=score,
                        metadata=dict(meta) if meta else {},
                    )
                )
        # Re-rank merged results by score, keep top_k overall
        results.sort(key=lambda h: h.score, reverse=True)
        if self._healthy:
            import time
            self._last_ok_at = int(time.time())
        return results[:top_k]

    def _build_where(self, scope_filter: dict[str, Any] | None) -> dict[str, Any]:
        """Translate scope filter to Chroma ``where`` clause.

        Chroma requires a single ``$and`` wrapper when combining multiple
        equality predicates, so we handle the 1-key vs N-key cases distinctly.
        """
        if not scope_filter:
            return {}
        clauses: list[dict[str, Any]] = []
        # Note: rag_items columns aren't stored in Chroma metadata directly,
        # so item-level filters (owner_agent_id, status) can't be applied here.
        # We rely on the SQLite path to filter; vector results are then
        # intersected with FTS results at the hybrid layer.
        if not clauses:
            return {}
        if len(clauses) == 1:
            return clauses[0]
        return {"$and": clauses}

    def delete_chunks(self, chunk_ids: list[str]) -> None:
        if not chunk_ids:
            return
        try:
            self._ensure_client()
        except VectorBackendError:
            return
        # Delete from every namespace/source_type collection (cheap per call)
        for coll in self._collections.values():
            try:
                coll.delete(ids=chunk_ids)
            except Exception as exc:  # noqa: BLE001
                self._healthy = False
                self._last_error = f"chroma delete failed: {exc}"

    def delete_item(self, item_id: str) -> None:
        try:
            self._ensure_client()
        except VectorBackendError:
            return
        for coll in self._collections.values():
            try:
                coll.delete(where={"item_id": item_id})
            except Exception as exc:  # noqa: BLE001
                self._healthy = False
                self._last_error = f"chroma delete_item failed: {exc}"

    def health_check(self) -> VectorBackendHealth:
        return VectorBackendHealth(
            healthy=self._healthy,
            last_ok_at=self._last_ok_at,
            last_error=self._last_error,
            backend="chroma",
        )


# ====================================================================
# Factory
# ====================================================================


def build_vector_backend(config: Any) -> VectorBackend:
    """Construct a backend from ``RagConfig.backend`` settings.

    Always returns SOMETHING — falls back to NoneBackend if the requested
    backend cannot be constructed, so calling code can stay flat.
    """
    backend_kind = (
        getattr(config.backend, "vector_backend", "none") or "none"
    ).lower()
    if backend_kind == "none":
        return NoneBackend()
    if backend_kind == "chroma":
        try:
            chroma_cfg = getattr(config, "chroma", None)
            if chroma_cfg is None:
                return ChromaBackend()
            return ChromaBackend(
                persist_dir=chroma_cfg.persist_dir,
                collection_prefix=chroma_cfg.collection_prefix,
            )
        except VectorBackendError:
            return NoneBackend()
    if backend_kind in {"milvus", "sqlite_vec"}:
        # Placeholder; not implemented in M4
        return NoneBackend()
    return NoneBackend()
