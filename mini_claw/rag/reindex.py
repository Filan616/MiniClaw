"""Incremental RAG reindex with active-version chunk mapping."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from mini_claw.config import RagConfig
from mini_claw.permissions.policy import PermissionPolicy
from mini_claw.rag.anchors import AnchorExtractor, content_hash as chunk_content_hash, similarity
from mini_claw.rag.chunker import CodeChunker, DocumentChunker, LogChunker
from mini_claw.rag.models import (
    RagChunk,
    RagItem,
    RagItemChunkVersion,
    RagReindexDiff,
    RagReindexDiffChunk,
)
from mini_claw.rag.permissions import check_index_permission
from mini_claw.rag.redaction import count_secret_hits, redact_for_rag
from mini_claw.rag.store import RagStore
from mini_claw.storage.db import Database

__all__ = ["RagReindexer"]


class RagIndexLock:
    """Process-local item lock for v1; SQLite lock table is available for v2."""

    _locks: dict[str, threading.Lock] = {}
    _guard = threading.Lock()

    def __init__(self, item_id: str) -> None:
        self.item_id = item_id
        with self._guard:
            self._lock = self._locks.setdefault(item_id, threading.Lock())

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._lock.release()
        return False


class RagReindexer:
    """Mapping-aware incremental reindex.

    New versions are represented by ``rag_item_chunk_versions``. Unchanged
    chunks are reused by mapping to the old chunk_id; changed chunks are
    inserted with new chunk_ids. Old rows remain query-invisible because
    retrieval joins through the active mapping.
    """

    def __init__(
        self,
        store: RagStore,
        storage: Database,
        config: RagConfig,
        policy: PermissionPolicy,
        *,
        vector_backend: Any = None,
        embedder: Any = None,
    ):
        self.store = store
        self.storage = storage
        self.config = config
        self.policy = policy
        self.vector_backend = vector_backend
        self.embedder = embedder
        self._doc_chunker = DocumentChunker(
            max_tokens=config.chunk.max_tokens,
            overlap_tokens=config.chunk.overlap_tokens,
        )
        self._code_chunker = CodeChunker(
            max_tokens=config.chunk.max_tokens,
            overlap_tokens=config.chunk.overlap_tokens,
        )
        self._log_chunker = LogChunker(
            max_tokens=config.chunk.max_tokens,
            overlap_tokens=config.chunk.overlap_tokens,
        )
        self._anchors = AnchorExtractor(
            chunker_version=config.reindex.chunker_version,
            anchor_schema_version=config.reindex.anchor_schema_version,
            parse_error_ratio_threshold=config.reindex.parse_error_ratio_threshold,
        )

    def reindex(
        self,
        item_id: str,
        *,
        ctx: dict[str, Any],
        dry_run: bool = False,
    ) -> tuple[bool, str]:
        with RagIndexLock(item_id):
            return self._reindex_locked(item_id, ctx=ctx, dry_run=dry_run)

    def _reindex_locked(
        self,
        item_id: str,
        *,
        ctx: dict[str, Any],
        dry_run: bool,
    ) -> tuple[bool, str]:
        item = self.store.get_item(item_id)
        if item is None:
            return False, "item not found"
        if item.owner_agent_id != ctx.get("agent_id"):
            return False, "cannot reindex item owned by another agent"
        if not item.source_path:
            return False, "item has no source_path; cannot reindex"

        allowed, deny_reason = check_index_permission(
            item.source_path, ctx, self.config, self.policy
        )
        if not allowed:
            return False, deny_reason

        try:
            content = Path(item.source_path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return False, f"cannot read file: {exc}"

        started = int(time.time())
        start_ms = int(time.time() * 1000)
        new_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        old_version = item.active_version
        new_version = old_version + 1
        diff_id = uuid.uuid4().hex

        chunks_data = list(self._chunk_content(content, item.source_path))
        if not chunks_data:
            return False, "no chunks generated"

        extraction = self._anchors.enrich_chunks(
            chunks_data,
            path=item.source_path,
            source_type=item.source_type,
            content=content,
        )
        redacted_pairs: list[tuple[dict[str, Any], bool]] = []
        for chunk_dict in chunks_data:
            text, was_red = redact_for_rag(chunk_dict["content"])
            chunk_dict["content"] = text
            redacted_pairs.append((chunk_dict, was_red))

        old_chunks = self.store.get_active_chunks(item_id)
        can_incremental, fallback_reason = self._can_incremental(item, old_chunks, extraction)
        mode = "incremental" if can_incremental else "full_reindex"
        if dry_run and not can_incremental:
            diff = self._build_diff(
                item,
                diff_id,
                old_version,
                new_version,
                started,
                mode="requires_full_reindex",
                reason=fallback_reason,
                old_chunks=old_chunks,
                new_specs=[],
            )
            return True, self._diff_message(diff, fallback_reason)

        new_specs = self._build_new_specs(
            item,
            new_version,
            redacted_pairs,
            extraction.chunk_metadata,
        )
        diff, diff_chunks, mappings, chunks_to_insert = self._classify(
            item,
            diff_id,
            old_version,
            new_version,
            started,
            mode,
            fallback_reason,
            old_chunks,
            new_specs,
            can_incremental=can_incremental,
        )
        if dry_run:
            return True, self._diff_message(diff, fallback_reason)

        sensitivity_level = self._sensitivity(item, redacted_pairs)
        vector_cleanup_status = "none"
        vector_upserted = False
        try:
            self.store.insert_chunks(chunks_to_insert)
            self._insert_fts(chunks_to_insert)

            if self._vector_enabled() and chunks_to_insert:
                texts = [c.content for c in chunks_to_insert]
                vectors = self.embedder.embed_texts(texts)
                if vectors and len(vectors) == len(chunks_to_insert):
                    self.vector_backend.upsert_chunks(
                        chunks_to_insert,
                        vectors,
                        namespace=item.namespace,
                        source_type=item.source_type,
                    )
                    vector_upserted = True
                    self._insert_embedding_rows(item, chunks_to_insert)

            self.store.insert_chunk_versions(mappings)
            now = int(time.time())
            finished_ms = int(time.time() * 1000)
            diff.status = "completed"
            diff.finished_at = now
            diff.duration_ms = finished_ms - start_ms
            diff.vector_cleanup_status = vector_cleanup_status
            diff.metadata_json = json.dumps(extraction.metadata, ensure_ascii=False)
            self.store.insert_reindex_diff(diff, diff_chunks)

            self.storage.execute(
                """
                UPDATE rag_items
                SET active_version = ?, content_hash = ?, sensitivity_level = ?,
                    updated_at = ?, status = 'active', chunker_version = ?,
                    anchor_schema_version = ?, embedding_model = ?,
                    metadata_json = ?, last_reindex_diff_id = ?,
                    last_reindex_diff_json = ?
                WHERE item_id = ?
                """,
                (
                    new_version,
                    new_hash,
                    sensitivity_level,
                    now,
                    self.config.reindex.chunker_version,
                    self.config.reindex.anchor_schema_version,
                    getattr(self.embedder, "model", None),
                    json.dumps(extraction.metadata, ensure_ascii=False),
                    diff_id,
                    self._diff_json(diff),
                    item_id,
                ),
            )
            return True, self._diff_message(diff, fallback_reason)
        except Exception as exc:  # noqa: BLE001
            vector_cleanup_status = "orphan_vectors" if vector_upserted else "not_needed"
            self._mark_abandoned(item_id, new_version, chunks_to_insert)
            failed = diff
            failed.status = "failed"
            failed.finished_at = int(time.time())
            failed.duration_ms = int(time.time() * 1000) - start_ms
            failed.reason = str(exc)
            failed.vector_cleanup_status = vector_cleanup_status
            self.store.insert_reindex_diff(failed, diff_chunks)
            return False, f"reindex failed; old active_version kept: {exc}"

    def rebind(
        self,
        item_id: str,
        new_path: str,
        *,
        ctx: dict[str, Any],
    ) -> tuple[bool, str]:
        item = self.store.get_item(item_id)
        if item is None:
            return False, "item not found"
        if item.owner_agent_id != ctx.get("agent_id"):
            return False, "cannot rebind item owned by another agent"

        allowed, deny_reason = check_index_permission(new_path, ctx, self.config, self.policy)
        if not allowed:
            return False, deny_reason

        try:
            content = Path(new_path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return False, f"cannot read new path: {exc}"

        new_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        if new_hash == item.content_hash:
            self.storage.execute(
                "UPDATE rag_items SET source_path = ?, status = 'active', "
                "updated_at = ? WHERE item_id = ?",
                (new_path, int(time.time()), item_id),
            )
            return True, "rebound (hash matches)"
        return False, (
            f"new path hash {new_hash} differs from indexed hash "
            f"{item.content_hash}; run reindex_context to refresh"
        )

    def last_diff(self, item_id: str) -> tuple[RagReindexDiff | None, list[RagReindexDiffChunk]]:
        item = self.store.get_item(item_id)
        if not item or not item.last_reindex_diff_id:
            return None, []
        return self.store.get_reindex_diff(item.last_reindex_diff_id)

    def _build_new_specs(
        self,
        item: RagItem,
        version: int,
        redacted_pairs: list[tuple[dict[str, Any], bool]],
        anchors: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        specs = []
        for i, (chunk_dict, was_redacted) in enumerate(redacted_pairs):
            anchor = anchors[i] if i < len(anchors) else {}
            text = chunk_dict["content"]
            metadata = {**anchor, "redacted": bool(was_redacted)}
            chunk_id = f"{item.item_id}-v{version}-{i}"
            specs.append(
                {
                    "chunk": RagChunk(
                        chunk_id=chunk_id,
                        item_id=item.item_id,
                        chunk_index=i,
                        content=text,
                        token_count=len(text) // 4,
                        start_line=chunk_dict.get("start_line"),
                        end_line=chunk_dict.get("end_line"),
                        section_title=chunk_dict.get("section_title"),
                        symbol_name=chunk_dict.get("symbol_name"),
                        language=chunk_dict.get("language"),
                        content_hash=chunk_content_hash(text),
                        metadata_json=json.dumps(metadata, ensure_ascii=False),
                        version=version,
                        anchor_id=anchor.get("anchor_id"),
                        chunk_hash=chunk_content_hash(text),
                        chunker_version=self.config.reindex.chunker_version,
                        anchor_schema_version=self.config.reindex.anchor_schema_version,
                    ),
                    "anchor": anchor,
                }
            )
        return specs

    def _classify(
        self,
        item: RagItem,
        diff_id: str,
        old_version: int,
        new_version: int,
        started: int,
        mode: str,
        fallback_reason: str | None,
        old_chunks: list[RagChunk],
        new_specs: list[dict[str, Any]],
        *,
        can_incremental: bool,
    ) -> tuple[RagReindexDiff, list[RagReindexDiffChunk], list[RagItemChunkVersion], list[RagChunk]]:
        old_by_anchor = {c.anchor_id: c for c in old_chunks if c.anchor_id}
        unmatched_old = {c.chunk_id: c for c in old_chunks}
        mappings: list[RagItemChunkVersion] = []
        diff_chunks: list[RagReindexDiffChunk] = []
        chunks_to_insert: list[RagChunk] = []
        counts = {"added": 0, "updated": 0, "deleted": 0, "reused": 0, "uncertain": 0}

        for order, spec in enumerate(new_specs):
            new_chunk: RagChunk = spec["chunk"]
            old = old_by_anchor.get(new_chunk.anchor_id) if can_incremental else None
            strategy = "anchor_id" if old else None
            confidence = 1.0 if old else None
            rename = 0

            if old is None and can_incremental:
                old, strategy, confidence, rename = self._fuzzy_match(new_chunk, unmatched_old)

            if old and old.chunk_hash == new_chunk.chunk_hash:
                mapped_chunk = old
                is_reused = 1
                change_type = "reused"
                counts["reused"] += 1
            elif old:
                mapped_chunk = new_chunk
                is_reused = 0
                change_type = "updated"
                counts["updated"] += 1
                chunks_to_insert.append(new_chunk)
            else:
                mapped_chunk = new_chunk
                is_reused = 0
                change_type = "added"
                counts["added"] += 1
                chunks_to_insert.append(new_chunk)

            if old:
                unmatched_old.pop(old.chunk_id, None)
            mappings.append(
                RagItemChunkVersion(
                    item_id=item.item_id,
                    version=new_version,
                    chunk_id=mapped_chunk.chunk_id,
                    chunk_order=order,
                    anchor_id=mapped_chunk.anchor_id or new_chunk.anchor_id,
                    status="active",
                    is_reused=is_reused,
                    created_at=started,
                )
            )
            diff_chunks.append(
                RagReindexDiffChunk(
                    row_id=uuid.uuid4().hex,
                    diff_id=diff_id,
                    item_id=item.item_id,
                    old_chunk_id=old.chunk_id if old else None,
                    new_chunk_id=mapped_chunk.chunk_id,
                    chunk_order=order,
                    anchor_id=mapped_chunk.anchor_id or new_chunk.anchor_id,
                    change_type=change_type,
                    match_strategy=strategy,
                    match_confidence=confidence,
                    rename_detected=rename,
                )
            )

        for old in unmatched_old.values():
            counts["deleted"] += 1
            diff_chunks.append(
                RagReindexDiffChunk(
                    row_id=uuid.uuid4().hex,
                    diff_id=diff_id,
                    item_id=item.item_id,
                    old_chunk_id=old.chunk_id,
                    chunk_order=old.chunk_index,
                    anchor_id=old.anchor_id,
                    change_type="deleted",
                    match_strategy="unmatched_old",
                )
            )

        diff = RagReindexDiff(
            diff_id=diff_id,
            item_id=item.item_id,
            old_version=old_version,
            new_version=new_version,
            status="pending",
            mode=mode,
            started_at=started,
            fallback_reason=fallback_reason,
            added_count=counts["added"],
            updated_count=counts["updated"],
            deleted_count=counts["deleted"],
            reused_count=counts["reused"],
            uncertain_count=counts["uncertain"],
        )
        return diff, diff_chunks, mappings, chunks_to_insert

    def _fuzzy_match(
        self, new_chunk: RagChunk, candidates: dict[str, RagChunk]
    ) -> tuple[RagChunk | None, str | None, float | None, int]:
        best: tuple[RagChunk, float] | None = None
        new_meta = _json(new_chunk.metadata_json)
        for old in candidates.values():
            old_meta = _json(old.metadata_json)
            same_parent = old_meta.get("parent_symbol") == new_meta.get("parent_symbol")
            same_file_symbol = (
                old_meta.get("symbol_kind") == new_meta.get("symbol_kind")
                and old_meta.get("qualified_name") == new_meta.get("qualified_name")
            )
            score = similarity(old.content, new_chunk.content)
            if same_file_symbol:
                score += 0.10
            if same_parent:
                score += 0.05
            score = min(score, 1.0)
            if best is None or score > best[1]:
                best = (old, score)
        if not best:
            return None, None, None, 0
        old, score = best
        threshold = self.config.reindex.rename_similarity_threshold
        if score >= threshold:
            old_meta = _json(old.metadata_json)
            new_meta = _json(new_chunk.metadata_json)
            rename = int(
                old_meta.get("qualified_name")
                and new_meta.get("qualified_name")
                and old_meta.get("qualified_name") != new_meta.get("qualified_name")
            )
            return old, "body_similarity", score, rename
        return None, None, None, 0

    def _build_diff(
        self,
        item: RagItem,
        diff_id: str,
        old_version: int,
        new_version: int,
        started: int,
        *,
        mode: str,
        reason: str | None,
        old_chunks: list[RagChunk],
        new_specs: list[dict[str, Any]],
    ) -> RagReindexDiff:
        return RagReindexDiff(
            diff_id=diff_id,
            item_id=item.item_id,
            old_version=old_version,
            new_version=new_version,
            status="dry_run",
            mode=mode,
            started_at=started,
            reason=reason,
            deleted_count=len(old_chunks) if not new_specs else 0,
            fallback_reason=reason,
        )

    def _can_incremental(
        self, item: RagItem, old_chunks: list[RagChunk], extraction: Any
    ) -> tuple[bool, str | None]:
        if item.chunker_version != self.config.reindex.chunker_version:
            return False, "chunker_version changed or missing"
        if item.anchor_schema_version != self.config.reindex.anchor_schema_version:
            return False, "anchor_schema_version changed or missing"
        if not old_chunks:
            return False, "no active chunks"
        if any(not c.anchor_id or not c.chunk_hash or not c.chunker_version for c in old_chunks):
            return False, "old active chunks missing anchor_id/chunk_hash/chunker_version"
        if extraction.parser_status in {"parser_unavailable", "parse_failed"}:
            return False, extraction.parser_status
        if extraction.parser_status == "parse_error_high":
            return False, "parse_error_ratio high"
        return True, None

    def _insert_fts(self, chunks: list[RagChunk]) -> None:
        if not chunks:
            return
        try:
            self.storage.executemany(
                "INSERT INTO rag_chunks_fts(chunk_id, item_id, content, section_title, symbol_name) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (c.chunk_id, c.item_id, c.content, c.section_title or "", c.symbol_name or "")
                    for c in chunks
                ],
            )
        except Exception:
            pass

    def _insert_embedding_rows(self, item: RagItem, chunks: list[RagChunk]) -> None:
        now = int(time.time())
        backend_name = getattr(self.vector_backend, "name", "unknown")
        coll_prefix = getattr(self.config.chroma, "collection_prefix", "miniclaw")
        coll_name = f"{coll_prefix}_{item.namespace}_{item.source_type}"
        self.storage.executemany(
            "INSERT OR REPLACE INTO rag_embeddings ("
            "chunk_id, item_id, backend, collection_name, embedding_model, dim, "
            "vector_id, created_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    c.chunk_id,
                    item.item_id,
                    backend_name,
                    coll_name,
                    getattr(self.embedder, "model", "unknown"),
                    getattr(self.embedder, "dim", None),
                    c.chunk_id,
                    now,
                    json.dumps({"version": c.version, "anchor_id": c.anchor_id}),
                )
                for c in chunks
            ],
        )

    def _mark_abandoned(self, item_id: str, version: int, chunks: list[RagChunk]) -> None:
        try:
            self.storage.execute(
                "UPDATE rag_item_chunk_versions SET status = 'abandoned' "
                "WHERE item_id = ? AND version = ?",
                (item_id, version),
            )
            for c in chunks:
                try:
                    self.storage.execute("DELETE FROM rag_chunks_fts WHERE chunk_id = ?", (c.chunk_id,))
                except Exception:
                    pass
        except Exception:
            pass

    def _vector_enabled(self) -> bool:
        return (
            self.embedder is not None
            and self.vector_backend is not None
            and getattr(self.vector_backend, "name", "none") != "none"
            and self.config.embedding.enabled
        )

    def _sensitivity(self, item: RagItem, chunks: list[tuple[dict[str, Any], bool]]) -> str:
        secret_hits = sum(count_secret_hits(c[0]["content"]) for c in chunks)
        is_sensitive_path = bool(item.source_path and self.policy.is_sensitive_path(item.source_path))
        if is_sensitive_path or secret_hits >= 3:
            return "high"
        if secret_hits >= 1:
            return "medium"
        return "low"

    def _chunk_content(self, content: str, path: str) -> list[dict[str, Any]]:
        p = Path(path)
        ext = p.suffix.lower()
        if ext in {".py", ".js", ".ts", ".java", ".go", ".cpp", ".c", ".rs", ".sh", ".jsx", ".tsx"}:
            return list(self._code_chunker.chunk(content, path))
        if ext in {".log", ".txt"} or "log" in p.name.lower():
            if "ERROR" in content or "Traceback" in content or "WARN" in content:
                return list(self._log_chunker.chunk(content, path))
        return list(self._doc_chunker.chunk(content, path))

    def _diff_message(self, diff: RagReindexDiff, reason: str | None) -> str:
        bits = [
            f"mode={diff.mode}",
            f"added={diff.added_count}",
            f"updated={diff.updated_count}",
            f"deleted={diff.deleted_count}",
            f"reused={diff.reused_count}",
            f"uncertain={diff.uncertain_count}",
        ]
        if reason:
            bits.append(f"reason={reason}")
        return "; ".join(bits)

    def _diff_json(self, diff: RagReindexDiff) -> str:
        return json.dumps(
            {
                "diff_id": diff.diff_id,
                "mode": diff.mode,
                "status": diff.status,
                "added": diff.added_count,
                "updated": diff.updated_count,
                "deleted": diff.deleted_count,
                "reused": diff.reused_count,
                "uncertain": diff.uncertain_count,
                "fallback_reason": diff.fallback_reason,
            },
            ensure_ascii=False,
        )


def _json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        data = json.loads(value)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}
