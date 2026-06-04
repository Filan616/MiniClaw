"""RAG health manager (Phase 8 M4.5).

Surfaces the live status of every RAG component for ``/rag status`` (chat
command) and ``mini-claw rag status`` (CLI). Drops ``RagStatus`` snapshots
the user can inspect to spot degraded states (Chroma down, embedding
model failing to load, abandoned reindex versions, pending memory
candidates queue building up, etc.).

All checks are READ-ONLY. ``check_*`` may probe external services with
small ping-style calls; failures are recorded as ``status='failed'`` and
never raise to the caller.
"""

from __future__ import annotations

import time
from typing import Any

from mini_claw.config import RagConfig
from mini_claw.rag.embeddings import EmbeddingError, EmbeddingProvider
from mini_claw.rag.models import RagComponentStatus, RagStatus
from mini_claw.rag.vector_backend import VectorBackend
from mini_claw.storage.db import Database

__all__ = ["RagHealthManager"]


class RagHealthManager:
    """Aggregate health probe for the RAG subsystem."""

    def __init__(
        self,
        storage: Database,
        config: RagConfig,
        vector_backend: VectorBackend,
        embedder: EmbeddingProvider | None,
    ):
        self.storage = storage
        self.config = config
        self.backend = vector_backend
        self.embedder = embedder

    # ------------------------------------------------------------------
    # Component checks
    # ------------------------------------------------------------------

    def check_fts(self) -> RagComponentStatus:
        """Verify FTS5 is available and ``rag_chunks_fts`` is in sync.

        Counts the rows on both sides and compares; mismatch may indicate
        an incomplete delete or an FTS5 build that silently dropped INSERTs.
        """
        try:
            chunks_n = self._scalar(
                "SELECT COUNT(*) AS n FROM rag_chunks c "
                "JOIN rag_items i ON c.item_id = i.item_id "
                "LEFT JOIN rag_item_chunk_versions m "
                "ON m.item_id = c.item_id AND m.chunk_id = c.chunk_id "
                "WHERE i.status NOT IN ('deleted', 'orphan') "
                "AND ((m.chunk_id IS NOT NULL AND m.version = i.active_version AND m.status = 'active') "
                "OR (m.chunk_id IS NULL AND c.version = i.active_version))"
            )
        except Exception as exc:  # noqa: BLE001
            return RagComponentStatus(
                component="fts",
                status="failed",
                last_error=f"rag_chunks query failed: {exc}",
            )

        try:
            fts_n = self._scalar(
                "SELECT COUNT(*) AS n FROM rag_chunks_fts f "
                "JOIN rag_chunks c ON c.chunk_id = f.chunk_id "
                "JOIN rag_items i ON i.item_id = c.item_id "
                "LEFT JOIN rag_item_chunk_versions m "
                "ON m.item_id = c.item_id AND m.chunk_id = c.chunk_id "
                "WHERE i.status NOT IN ('deleted', 'orphan') "
                "AND ((m.chunk_id IS NOT NULL AND m.version = i.active_version AND m.status = 'active') "
                "OR (m.chunk_id IS NULL AND c.version = i.active_version))"
            )
        except Exception as exc:  # noqa: BLE001
            return RagComponentStatus(
                component="fts",
                status="failed",
                last_error=f"rag_chunks_fts unavailable: {exc}",
                details={"rag_chunks_active": chunks_n},
            )

        details = {"rag_chunks_active": chunks_n, "rag_chunks_fts": fts_n}
        if chunks_n != fts_n:
            return RagComponentStatus(
                component="fts",
                status="degraded",
                last_error=f"row count mismatch: chunks={chunks_n} fts={fts_n}",
                details=details,
            )
        return RagComponentStatus(
            component="fts",
            status="ok",
            last_ok_at=int(time.time()),
            details=details,
        )

    def check_vector_backend(self) -> RagComponentStatus:
        """Probe the configured vector backend's health endpoint.

        ``NoneBackend`` is always ``ok`` (it's the documented disabled state).
        """
        try:
            health = self.backend.health_check()
        except Exception as exc:  # noqa: BLE001
            return RagComponentStatus(
                component=getattr(self.backend, "name", "unknown"),
                status="failed",
                last_error=f"health_check raised: {exc}",
            )
        if not health.healthy:
            return RagComponentStatus(
                component=health.backend,
                status="degraded",
                last_ok_at=health.last_ok_at,
                last_error=health.last_error,
            )
        return RagComponentStatus(
            component=health.backend,
            status="ok",
            last_ok_at=health.last_ok_at or int(time.time()),
        )

    def check_embedding(self) -> RagComponentStatus:
        """Embed a tiny string to verify the provider is reachable.

        When ``embedding.enabled=False`` we report ``ok`` (disabled-by-design).
        Real provider failures (model file missing, OpenAI API down, missing
        key) come back as ``failed`` with the exception message.
        """
        if not self.config.embedding.enabled or self.embedder is None:
            return RagComponentStatus(
                component="embedding",
                status="ok",
                details={"enabled": False},
            )
        try:
            vec = self.embedder.embed_query("ping")
        except EmbeddingError as exc:
            return RagComponentStatus(
                component="embedding",
                status="failed",
                last_error=str(exc),
                details={
                    "provider": self.config.embedding.provider,
                    "model": self.config.embedding.model,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return RagComponentStatus(
                component="embedding",
                status="failed",
                last_error=f"unexpected: {exc}",
            )
        if not vec:
            return RagComponentStatus(
                component="embedding",
                status="degraded",
                last_error="empty vector returned",
            )
        return RagComponentStatus(
            component="embedding",
            status="ok",
            last_ok_at=int(time.time()),
            details={
                "provider": self.config.embedding.provider,
                "model": self.config.embedding.model,
                "dim": len(vec),
            },
        )

    # ------------------------------------------------------------------
    # Aggregate counters
    # ------------------------------------------------------------------

    def count_stale_orphan(self) -> tuple[int, int]:
        """Count items with ``status='stale'`` and ``status='orphan'``."""
        try:
            stale = self._scalar(
                "SELECT COUNT(*) AS n FROM rag_items WHERE status = 'stale'"
            )
            orphan = self._scalar(
                "SELECT COUNT(*) AS n FROM rag_items WHERE status = 'orphan'"
            )
            return stale, orphan
        except Exception:
            return 0, 0

    def count_pending_candidates(self) -> int:
        """Count ``memory_candidates`` rows still awaiting approval (M5 prep)."""
        try:
            return self._scalar(
                "SELECT COUNT(*) AS n FROM memory_candidates WHERE status = 'pending'"
            )
        except Exception:
            return 0

    def count_abandoned_reindex_versions(self) -> int:
        """Count abandoned mapping rows or old non-active chunks."""
        try:
            mapped = self._scalar(
                "SELECT COUNT(*) AS n FROM rag_item_chunk_versions "
                "WHERE status IN ('abandoned', 'pending')"
            )
            legacy = self._scalar(
                "SELECT COUNT(*) AS n FROM rag_chunks c "
                "JOIN rag_items i ON c.item_id = i.item_id "
                "LEFT JOIN rag_item_chunk_versions m ON m.chunk_id = c.chunk_id "
                "WHERE c.version <> i.active_version AND m.chunk_id IS NULL"
            )
            return mapped + legacy
        except Exception:
            try:
                return self._scalar(
                    "SELECT COUNT(*) AS n FROM rag_chunks c "
                    "JOIN rag_items i ON c.item_id = i.item_id "
                    "WHERE c.version <> i.active_version"
                )
            except Exception:
                return 0

    def count_delete_failed(self) -> int:
        """Count items stuck in ``deleted_pending`` or ``delete_failed``
        (incomplete 7-step delete transactions from M2).
        """
        try:
            return self._scalar(
                "SELECT COUNT(*) AS n FROM rag_items "
                "WHERE status IN ('deleted_pending', 'delete_failed')"
            )
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Aggregate snapshot
    # ------------------------------------------------------------------

    def summarize(self) -> RagStatus:
        """Build a complete ``RagStatus`` snapshot."""
        fts = self.check_fts()
        vec = self.check_vector_backend()
        emb = self.check_embedding()
        stale, orphan = self.count_stale_orphan()
        pending = self.count_pending_candidates()
        abandoned = self.count_abandoned_reindex_versions()

        # Decide the "active fallback" string the user sees.
        active_fallback = self._infer_fallback(vec, emb)

        return RagStatus(
            enabled=self.config.enabled,
            fts=fts,
            chroma=vec,
            embedding=emb,
            active_fallback=active_fallback,
            stale_items=stale,
            orphan_items=orphan,
            pending_candidates=pending,
            abandoned_reindex_versions=abandoned,
            timestamp=int(time.time()),
        )

    def render_text(self, status: RagStatus | None = None) -> str:
        """Human-readable single-screen output (used by ``/rag status``)."""
        s = status or self.summarize()
        lines = [
            "RAG Status",
            f"  enabled        : {s.enabled}",
            f"  FTS5           : {self._render_component(s.fts)}",
            f"  Vector backend : {self._render_component(s.chroma)}",
            f"  Embedding      : {self._render_component(s.embedding)}",
            f"  Active fallback: {s.active_fallback}",
            f"  Stale items    : {s.stale_items}",
            f"  Orphan items   : {s.orphan_items}",
            f"  Pending memory candidates : {s.pending_candidates}",
            f"  Abandoned reindex versions: {s.abandoned_reindex_versions}",
            f"  Delete-failed items       : {self.count_delete_failed()}",
        ]
        return "\n".join(lines)

    def to_dict(self, status: RagStatus | None = None) -> dict[str, Any]:
        """JSON-serializable snapshot (used by ``mini-claw rag status --json``)."""
        s = status or self.summarize()
        return {
            "enabled": s.enabled,
            "fts": _component_dict(s.fts),
            "vector_backend": _component_dict(s.chroma),
            "embedding": _component_dict(s.embedding),
            "active_fallback": s.active_fallback,
            "stale_items": s.stale_items,
            "orphan_items": s.orphan_items,
            "pending_candidates": s.pending_candidates,
            "abandoned_reindex_versions": s.abandoned_reindex_versions,
            "delete_failed_items": self.count_delete_failed(),
            "timestamp": s.timestamp,
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _scalar(self, sql: str) -> int:
        row = self.storage.fetchone(sql)
        if not row:
            return 0
        # row is dict[str, Any]; we wrote AS n above
        return int(row.get("n") or list(row.values())[0] or 0)

    def _infer_fallback(
        self, vec: RagComponentStatus, emb: RagComponentStatus
    ) -> str:
        backend = (self.config.backend.vector_backend or "none").lower()
        if backend == "none":
            return "FTS-only (vector_backend disabled)"
        # Any failure on the vector path forces FTS-only behavior at runtime
        # (HybridRetriever swallows vector errors).
        if vec.status != "ok" or emb.status == "failed":
            reason = vec.last_error or emb.last_error or "vector path unavailable"
            return f"FTS-only ({reason})"
        return "hybrid (FTS + vector)"

    def _render_component(self, c: RagComponentStatus) -> str:
        body = c.status
        if c.status != "ok":
            body += f"  (last error: {c.last_error or 'unknown'})"
        if c.details:
            extras = ", ".join(f"{k}={v}" for k, v in c.details.items())
            body += f"  [{extras}]"
        return body


def _component_dict(c: RagComponentStatus) -> dict[str, Any]:
    return {
        "component": c.component,
        "status": c.status,
        "last_ok_at": c.last_ok_at,
        "last_error": c.last_error,
        "details": dict(c.details),
    }
