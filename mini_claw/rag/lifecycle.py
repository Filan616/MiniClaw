"""RAG lifecycle management (Phase 8 M3).

State transitions (driven by ``last_accessed_at`` / ``updated_at``):

    active ----- warm_after_days ------> warm
    warm ------- archive_after_days ---> archived
    archived --- cold_after_days ------> cold
    cold ------- delete_after_days ----> deleted (chunks gone, item kept as tombstone)

Special:
- ``log`` source_type bypasses warm/archived and is deleted after log_ttl_days
- ``pinned=1`` items are NEVER auto-transitioned (user reviewed protection)
- Files that disappear on disk get ``status='orphan'``
- Files whose hash changed get ``status='stale'``
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from mini_claw.config import RagConfig
from mini_claw.rag.store import RagStore
from mini_claw.storage.db import Database

__all__ = ["RagLifecycle"]


class RagLifecycle:
    """Periodic RAG state-transition manager."""

    def __init__(self, storage: Database, config: RagConfig):
        self.storage = storage
        self.config = config
        self.store = RagStore(storage)

    def cleanup_expired(self, now: int | None = None) -> dict[str, int]:
        """Run a single pass of state transitions.

        Returns a dict with counts of transitions performed:
        ``{warm, archived, cold, deleted, log_deleted, stale, orphan}``.

        **Pinned items are excluded from every state transition** (user
        feedback 7). Tombstones (status='deleted') are also excluded so a
        repeat run does not double-delete.
        """
        now = int(now if now is not None else time.time())
        lc = self.config.lifecycle

        warm_threshold = now - lc.warm_after_days * 86400
        archive_threshold = now - lc.archive_after_days * 86400
        cold_threshold = now - lc.cold_after_days * 86400
        delete_threshold = now - lc.delete_after_days * 86400
        log_threshold = now - lc.log_ttl_days * 86400

        counts = {
            "warm": 0,
            "archived": 0,
            "cold": 0,
            "deleted": 0,
            "log_deleted": 0,
            "stale": 0,
            "orphan": 0,
        }

        # ALWAYS guard with pinned=0 first — never auto-transition pinned items.
        # active → warm
        cur = self.storage.execute(
            "UPDATE rag_items SET status = 'warm', updated_at = ? "
            "WHERE pinned = 0 AND status = 'active' "
            "AND COALESCE(last_accessed_at, updated_at) < ? "
            "AND source_type != 'log'",
            (now, warm_threshold),
        )
        counts["warm"] = cur.rowcount or 0

        # warm → archived
        cur = self.storage.execute(
            "UPDATE rag_items SET status = 'archived', updated_at = ? "
            "WHERE pinned = 0 AND status = 'warm' "
            "AND COALESCE(last_accessed_at, updated_at) < ? "
            "AND source_type != 'log'",
            (now, archive_threshold),
        )
        counts["archived"] = cur.rowcount or 0

        # archived → cold
        cur = self.storage.execute(
            "UPDATE rag_items SET status = 'cold', updated_at = ? "
            "WHERE pinned = 0 AND status = 'archived' "
            "AND COALESCE(last_accessed_at, updated_at) < ? "
            "AND source_type != 'log'",
            (now, cold_threshold),
        )
        counts["cold"] = cur.rowcount or 0

        # cold → deleted: chunks + FTS removed, tombstone retained per config
        cold_to_delete = self.storage.fetchall(
            "SELECT item_id FROM rag_items "
            "WHERE pinned = 0 AND status = 'cold' "
            "AND COALESCE(last_accessed_at, updated_at) < ?",
            (delete_threshold,),
        )
        for row in cold_to_delete:
            self._delete_chunks_and_fts(row["item_id"])
            self.store.delete_item(
                row["item_id"], keep_tombstone=lc.keep_tombstone
            )
            counts["deleted"] += 1

        # log TTL: delete log items past their TTL regardless of state
        log_to_delete = self.storage.fetchall(
            "SELECT item_id FROM rag_items "
            "WHERE pinned = 0 AND source_type = 'log' "
            "AND status NOT IN ('deleted', 'orphan') "
            "AND COALESCE(last_accessed_at, updated_at) < ?",
            (log_threshold,),
        )
        for row in log_to_delete:
            self._delete_chunks_and_fts(row["item_id"])
            self.store.delete_item(
                row["item_id"], keep_tombstone=lc.keep_tombstone
            )
            counts["log_deleted"] += 1

        # stale / orphan detection (file system check)
        counts["stale"], counts["orphan"] = self._detect_stale_and_orphan()

        return counts

    def _delete_chunks_and_fts(self, item_id: str) -> None:
        """Remove chunks and FTS rows for an item (best-effort)."""
        try:
            self.storage.execute(
                "DELETE FROM rag_chunks_fts WHERE item_id = ?", (item_id,)
            )
        except Exception:
            # FTS5 may not be available
            pass
        try:
            self.storage.execute(
                "DELETE FROM rag_item_chunk_versions WHERE item_id = ?", (item_id,)
            )
            self.storage.execute(
                "DELETE FROM rag_reindex_diff_chunks WHERE item_id = ?", (item_id,)
            )
            self.storage.execute(
                "DELETE FROM rag_reindex_diffs WHERE item_id = ?", (item_id,)
            )
        except Exception:
            pass
        self.storage.execute("DELETE FROM rag_chunks WHERE item_id = ?", (item_id,))

    def _detect_stale_and_orphan(self) -> tuple[int, int]:
        """Detect items whose source file disappeared (orphan) or changed (stale).

        Only inspects items in active/warm/archived (cold and deleted are out of
        scope to keep per-pass cost bounded).
        """
        rows = self.storage.fetchall(
            "SELECT item_id, source_path, content_hash FROM rag_items "
            "WHERE pinned = 0 AND status IN ('active', 'warm', 'archived') "
            "AND source_path IS NOT NULL"
        )
        stale_count = 0
        orphan_count = 0
        for row in rows:
            path_str = row["source_path"]
            if not path_str:
                continue
            p = Path(path_str)
            try:
                if not p.exists():
                    self.store.mark_status(row["item_id"], "orphan")
                    orphan_count += 1
                    continue
            except OSError:
                # Cannot stat — skip rather than misclassify
                continue
            # File exists; if hash differs, mark stale.
            # Avoid reading huge files: only if file size < 5MB.
            try:
                if p.is_file() and p.stat().st_size < 5 * 1024 * 1024:
                    import hashlib

                    text = p.read_text(encoding="utf-8", errors="replace")
                    new_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
                    if row["content_hash"] and new_hash != row["content_hash"]:
                        self.store.mark_status(row["item_id"], "stale")
                        stale_count += 1
            except OSError:
                continue
        return stale_count, orphan_count

    def touch(self, item_id: str) -> None:
        """Update ``last_accessed_at`` so an item resets its lifecycle clock.

        Called by retriever on every successful hit.
        """
        now = int(time.time())
        self.storage.execute(
            "UPDATE rag_items SET last_accessed_at = ?, "
            "access_count = access_count + 1 WHERE item_id = ?",
            (now, item_id),
        )
