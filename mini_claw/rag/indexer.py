"""RAG indexer (Phase 8 M2).

RagIndexer takes a file path, chunks it, applies redaction, computes hashes for
dedup, determines sensitivity level, and writes to:
- rag_items (metadata)
- rag_chunks (content)
- rag_chunks_fts (FTS5 virtual table, try/except for SQLite builds without FTS5)

M4 will extend this to also write rag_embeddings + vector backend.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

from mini_claw.config import RagConfig
from mini_claw.permissions.policy import PermissionPolicy
from mini_claw.rag.anchors import AnchorExtractor, content_hash as chunk_content_hash
from mini_claw.rag.chunker import CodeChunker, DocumentChunker, LogChunker
from mini_claw.rag.models import RagChunk, RagItem, RagItemChunkVersion
from mini_claw.rag.permissions import check_index_permission
from mini_claw.rag.redaction import count_secret_hits, redact_for_rag
from mini_claw.rag.store import RagStore

__all__ = ["RagIndexer"]


class RagIndexer:
    """Index files into RAG storage."""

    def __init__(
        self,
        store: RagStore,
        config: RagConfig,
        policy: PermissionPolicy,
        *,
        vector_backend: Any = None,
        embedder: Any = None,
    ):
        self.store = store
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

    def index_path(
        self,
        path: str,
        *,
        ctx: dict[str, Any],
        namespace: str = "context",
        source_type: str | None = None,
        scope_type: str = "workspace",
        scope_id: str | None = None,
        title: str | None = None,
    ) -> tuple[str | None, str]:
        """Index a file.

        Returns ``(item_id | None, error_message)``.

        Steps:
        1. Permission check (workspace / sensitive / bypass / size / binary)
        2. Read file content
        3. Compute content_hash for dedup
        4. Check if already indexed (same path + hash)
        5. Chunk content
        6. Redact chunks
        7. Determine sensitivity_level (count secret hits)
        8. Write rag_items
        9. Write rag_chunks
        10. Write rag_chunks_fts (try/except)
        """
        # 1. Permission check
        allowed, deny_reason = check_index_permission(path, ctx, self.config, self.policy)
        if not allowed:
            return None, deny_reason

        # 2. Read file
        file_path = Path(path)
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return None, f"cannot read file: {exc}"

        # 3. Content hash for dedup
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

        # 4. Dedup check
        existing = self.store.list_by_scope(
            namespace=namespace,
            owner_agent_id=ctx.get("agent_id"),
            status="active",
            limit=1000,
        )
        for item in existing:
            if item.source_path == path and item.content_hash == content_hash:
                return item.item_id, f"already indexed (item_id={item.item_id})"

        # 5. Chunk
        chunks_data = list(self._chunk_content(content, path))
        if not chunks_data:
            return None, "no chunks generated (empty file or unsupported format)"

        source_type_value = source_type or self._detect_source_type(path)
        extraction = self._anchors.enrich_chunks(
            chunks_data,
            path=path,
            source_type=source_type_value,
            content=content,
        )

        # 6. Redact
        redacted_chunks: list[tuple[dict, bool]] = []  # (chunk_dict, was_redacted)
        for chunk_dict in chunks_data:
            text, was_red = redact_for_rag(chunk_dict["content"])
            chunk_dict["content"] = text
            redacted_chunks.append((chunk_dict, was_red))

        # 7. Sensitivity level
        secret_hits = sum(count_secret_hits(c[0]["content"]) for c in redacted_chunks)
        is_sensitive_path = self.policy.is_sensitive_path(path)
        if is_sensitive_path or secret_hits >= 3:
            sensitivity_level = "high"
        elif secret_hits >= 1:
            sensitivity_level = "medium"
        else:
            sensitivity_level = "low"

        # 8. Write rag_items
        now = int(time.time())
        item_id = uuid.uuid4().hex
        item = RagItem(
            item_id=item_id,
            namespace=namespace,
            source_type=source_type_value,
            scope_type=scope_type,
            scope_id=scope_id or ctx.get("workspace_dir", "unknown"),
            owner_agent_id=ctx.get("agent_id", "unknown"),
            session_id=ctx.get("session_id"),
            chat_id=ctx.get("chat_id"),
            channel_name=ctx.get("channel_name"),
            workspace_dir=ctx.get("workspace_dir"),
            source_path=path,
            title=title or Path(path).name,
            content_hash=content_hash,
            status="active",
            created_at=now,
            updated_at=now,
            indexed_by_agent_id=ctx.get("agent_id"),
            indexed_by_chat_id=ctx.get("chat_id"),
            indexed_by_channel=ctx.get("channel_name"),
            active_version=1,
            sensitivity_level=sensitivity_level,
            chunker_version=self.config.reindex.chunker_version,
            anchor_schema_version=self.config.reindex.anchor_schema_version,
            embedding_model=getattr(self.embedder, "model", None),
            metadata_json=json.dumps(extraction.metadata, ensure_ascii=False),
        )
        self.store.insert_item(item)

        # 9. Write rag_chunks
        chunks: list[RagChunk] = []
        mappings: list[RagItemChunkVersion] = []
        for i, (chunk_dict, _) in enumerate(redacted_chunks):
            chunk_id = f"{item_id}-{i}"
            anchor_meta = extraction.chunk_metadata[i] if i < len(extraction.chunk_metadata) else {}
            metadata = {**anchor_meta, "redacted": bool(redacted_chunks[i][1])}
            chunks.append(
                RagChunk(
                    chunk_id=chunk_id,
                    item_id=item_id,
                    chunk_index=i,
                    content=chunk_dict["content"],
                    token_count=len(chunk_dict["content"]) // 4,  # rough estimate
                    start_line=chunk_dict.get("start_line"),
                    end_line=chunk_dict.get("end_line"),
                    section_title=chunk_dict.get("section_title"),
                    symbol_name=chunk_dict.get("symbol_name"),
                    language=chunk_dict.get("language"),
                    content_hash=chunk_content_hash(chunk_dict["content"]),
                    metadata_json=json.dumps(metadata, ensure_ascii=False),
                    version=1,
                    anchor_id=anchor_meta.get("anchor_id"),
                    chunk_hash=chunk_content_hash(chunk_dict["content"]),
                    chunker_version=self.config.reindex.chunker_version,
                    anchor_schema_version=self.config.reindex.anchor_schema_version,
                )
            )
            mappings.append(
                RagItemChunkVersion(
                    item_id=item_id,
                    version=1,
                    chunk_id=chunk_id,
                    chunk_order=i,
                    anchor_id=anchor_meta.get("anchor_id"),
                    is_reused=0,
                    created_at=now,
                )
            )
        self.store.insert_chunks(chunks)
        self.store.insert_chunk_versions(mappings)

        # 10. Write FTS5 (try/except for SQLite builds without FTS5)
        try:
            fts_rows = [
                (
                    c.chunk_id,
                    c.item_id,
                    c.content,
                    c.section_title or "",
                    c.symbol_name or "",
                )
                for c in chunks
            ]
            self.store.storage.executemany(
                "INSERT INTO rag_chunks_fts(chunk_id, item_id, content, section_title, symbol_name) "
                "VALUES (?, ?, ?, ?, ?)",
                fts_rows,
            )
        except Exception:
            # FTS5 not available; M2 retriever will fall back to LIKE search
            pass

        # 11. Phase 8 M4: vector backend upsert (best-effort, optional).
        # Embeddings are computed only when both embedder and a non-None
        # backend are configured AND embedding.enabled is True.
        if (
            self.embedder is not None
            and self.vector_backend is not None
            and getattr(self.vector_backend, "name", "none") != "none"
            and self.config.embedding.enabled
        ):
            try:
                texts = [c.content for c in chunks]
                vectors = self.embedder.embed_texts(texts)
                if vectors and len(vectors) == len(chunks):
                    self.vector_backend.upsert_chunks(
                        chunks,
                        vectors,
                        namespace=item.namespace,
                        source_type=item.source_type,
                    )
                    # Write per-chunk embedding metadata so M4.5 health
                    # check can verify FTS/chroma row parity.
                    backend_name = getattr(self.vector_backend, "name", "unknown")
                    coll_prefix = getattr(
                        self.config.chroma, "collection_prefix", "miniclaw"
                    )
                    coll_name = f"{coll_prefix}_{item.namespace}_{item.source_type}"
                    rows = [
                        (
                            c.chunk_id,
                            item_id,
                            backend_name,
                            coll_name,
                            self.embedder.model,
                            self.embedder.dim,
                            c.chunk_id,  # vector_id == chunk_id by convention
                            now,
                            None,
                        )
                        for c in chunks
                    ]
                    self.store.storage.executemany(
                        "INSERT OR REPLACE INTO rag_embeddings ("
                        "chunk_id, item_id, backend, collection_name, "
                        "embedding_model, dim, vector_id, created_at, metadata_json"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        rows,
                    )
            except Exception:
                # Vector failures must never block FTS indexing path.
                # Health is tracked on the backend itself for M4.5.
                pass

        return item_id, ""

    def _chunk_content(self, content: str, path: str) -> list[dict]:
        """Dispatch to appropriate chunker based on file extension."""
        p = Path(path)
        ext = p.suffix.lower()

        # Code files
        if ext in {".py", ".js", ".ts", ".java", ".go", ".cpp", ".c", ".rs", ".sh", ".jsx", ".tsx"}:
            return list(self._code_chunker.chunk(content, path))

        # Log files
        if ext in {".log", ".txt"} or "log" in p.name.lower():
            # Heuristic: if contains "ERROR" or "Traceback", treat as log
            if "ERROR" in content or "Traceback" in content or "WARN" in content:
                return list(self._log_chunker.chunk(content, path))

        # Document files (default)
        return list(self._doc_chunker.chunk(content, path))

    def _detect_source_type(self, path: str) -> str:
        """Detect source_type from file extension."""
        ext = Path(path).suffix.lower()
        if ext in {".py", ".js", ".ts", ".java", ".go", ".cpp", ".c", ".rs", ".sh", ".jsx", ".tsx"}:
            return "code"
        if ext in {".log"}:
            return "log"
        if ext in {".md", ".txt", ".rst", ".html", ".json", ".yaml", ".yml"}:
            return "document"
        return "document"  # default
