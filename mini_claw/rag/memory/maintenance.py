"""Phase 9 M9.6: Memory maintenance — dedupe / conflict / archive suggestions.

Periodically scans memory items to detect:
- Duplicates (Jaccard text similarity, with optional embedding cosine)
- Conflicts (same scope+topic but contradictory wording)
- Stale candidates (low access_count + old created_at, not pinned)

NEVER auto-deletes. Only generates suggestions for user review.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mini_claw.storage.db import Database

__all__ = [
    "MemoryMaintenance",
    "DuplicateGroup",
    "ConflictPair",
    "StaleCandidate",
    "MaintenanceResult",
]


# Phase 9 M9.6: Words that flip the meaning of a memory ("must" vs "must not")
_NEGATION_TOKENS = {
    "not",
    "no",
    "never",
    "don't",
    "doesn't",
    "without",
    "禁止",
    "不",
    "无",
    "从不",
    "不要",
}


def _tokenize(text: str) -> set[str]:
    """Normalize + tokenize for Jaccard."""
    text = (text or "").lower()
    # Strip punctuation; keep CJK characters and word chars
    tokens = re.findall(r"\w+|[一-鿿]", text)
    return set(t for t in tokens if len(t) > 1)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = a & b
    union = a | b
    return len(inter) / len(union) if union else 0.0


def _has_negation(text: str) -> bool:
    """Cheap negation detector for conflict heuristic."""
    lower = (text or "").lower()
    tokens = re.findall(r"\w+|[一-鿿]+", lower)
    return any(t in _NEGATION_TOKENS for t in tokens)


@dataclass(slots=True)
class DuplicateGroup:
    """Suggested duplicate group: ``representative`` is the canonical memory_id."""

    representative_id: str
    duplicate_ids: list[str]
    similarity: float
    reason: str = "high_text_similarity"


@dataclass(slots=True)
class ConflictPair:
    """Suggested conflict pair: two memories on same topic with opposite polarity."""

    item_id_a: str
    item_id_b: str
    similarity: float
    reason: str = "negation_mismatch"


@dataclass(slots=True)
class StaleCandidate:
    """Suggested stale memory: low access + old + not pinned."""

    item_id: str
    last_accessed_at: int | None
    access_count: int
    age_days: int
    reason: str = "low_access"


@dataclass(slots=True)
class MaintenanceResult:
    """Aggregated maintenance suggestions."""

    duplicates: list[DuplicateGroup] = field(default_factory=list)
    conflicts: list[ConflictPair] = field(default_factory=list)
    stale: list[StaleCandidate] = field(default_factory=list)
    scanned_count: int = 0


class MemoryMaintenance:
    """Generates dedupe / conflict / archive suggestions from rag_items."""

    def __init__(self, storage: "Database", config: dict[str, Any] | None = None) -> None:
        self.storage = storage
        self.config = config or {}
        # Legacy threshold (for backward compatibility)
        legacy_threshold = self.config.get("dupe_threshold")
        # Phase 9 M9.6: hybrid dedupe configuration
        self.dedupe_text_threshold = float(
            self.config.get("dedupe_text_threshold", legacy_threshold if legacy_threshold else 0.85)
        )
        self.dedupe_embedding_threshold = float(self.config.get("dedupe_embedding_threshold", 0.92))
        self.dedupe_mode = self.config.get("mode", "auto")  # auto | text_only | hybrid
        self.conflict_threshold = float(self.config.get("conflict_threshold", 0.55))
        self.stale_age_days = int(self.config.get("stale_age_days", 90))
        self.stale_max_access = int(self.config.get("stale_max_access", 1))
        # Embedder for hybrid mode (optional)
        self.embedder = self.config.get("embedder")

    def run(
        self,
        *,
        ctx: dict[str, Any],
        scope: str = "agent",
    ) -> MaintenanceResult:
        """Phase 9 M9.6: Scan memory items and produce suggestions.

        Returns aggregated suggestions; NEVER mutates rag_items.
        """
        # Build scope-restricted query
        where_parts = ["namespace = 'memory'", "status = 'active'"]
        params: list[Any] = []

        agent_id = ctx.get("agent_id")
        workspace_dir = ctx.get("workspace_dir")

        if scope == "agent":
            if not agent_id:
                return MaintenanceResult()
            where_parts.append("owner_agent_id = ?")
            params.append(agent_id)
        elif scope == "workspace":
            if not workspace_dir:
                return MaintenanceResult()
            where_parts.append("workspace_dir = ?")
            params.append(str(workspace_dir))
        elif scope == "all":
            if not agent_id:
                return MaintenanceResult()
            where_parts.append("owner_agent_id = ?")
            params.append(agent_id)

        where_clause = " AND ".join(where_parts)

        # Fetch items + their first chunk content
        rows = self.storage.fetchall(
            f"""
            SELECT i.item_id, i.source_type, i.scope_type, i.scope_id,
                   i.last_accessed_at, i.access_count, i.created_at, i.pinned, i.confidence,
                   c.content
            FROM rag_items i
            LEFT JOIN rag_chunks c ON c.item_id = i.item_id AND c.chunk_index = 0
            WHERE {where_clause}
            ORDER BY i.created_at DESC
            """,
            tuple(params),
        )

        items = [dict(r) for r in rows]
        result = MaintenanceResult(scanned_count=len(items))

        # 1. Detect duplicates
        result.duplicates = self._detect_duplicates(items)

        # 2. Detect conflicts
        result.conflicts = self._detect_conflicts(items)

        # 3. Detect stale candidates
        result.stale = self._detect_stale(items)

        return result

    def _detect_duplicates(self, items: list[dict[str, Any]]) -> list[DuplicateGroup]:
        """Group items by text similarity and optionally embedding similarity.

        Phase 9 M9.6: Uses hybrid dedupe when mode="hybrid" or mode="auto" with embedder.
        """
        from mini_claw.rag.memory.dedupe import find_duplicates

        # Group by scope first — only detect duplicates within same scope
        scope_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in items:
            scope_key = (item.get("scope_type"), item.get("scope_id"))
            if scope_key not in scope_groups:
                scope_groups[scope_key] = []
            scope_groups[scope_key].append(item)

        # Run dedupe on each scope independently
        all_groups: list[DuplicateGroup] = []
        for scope_items in scope_groups.values():
            if len(scope_items) < 2:
                continue

            # Use hybrid dedupe with config-driven thresholds
            dup_groups = find_duplicates(
                scope_items,
                threshold=self.dedupe_text_threshold,
                mode=self.dedupe_mode,
                embedding_threshold=self.dedupe_embedding_threshold,
                embedder=self.embedder,
            )

            # Convert dedupe results to maintenance format
            for dg in dup_groups:
                if len(dg.item_ids) >= 2:
                    all_groups.append(
                        DuplicateGroup(
                            representative_id=dg.item_ids[0],
                            duplicate_ids=dg.item_ids[1:],
                            similarity=dg.similarity,
                            reason=dg.reason,
                        )
                    )

        return all_groups

    def _detect_conflicts(self, items: list[dict[str, Any]]) -> list[ConflictPair]:
        """Find item pairs with topic overlap but opposite polarity (negation)."""
        conflicts: list[ConflictPair] = []
        emitted_pairs: set[tuple[str, str]] = set()

        tokenized = [
            (it, _tokenize(it.get("content") or ""), _has_negation(it.get("content") or ""))
            for it in items
        ]

        for i, (item_a, tokens_a, neg_a) in enumerate(tokenized):
            if not tokens_a:
                continue
            for j in range(i + 1, len(tokenized)):
                item_b, tokens_b, neg_b = tokenized[j]
                if not tokens_b:
                    continue
                # Same scope and same memory_type for conflict to make sense
                if item_a.get("scope_type") != item_b.get("scope_type"):
                    continue
                if item_a.get("scope_id") != item_b.get("scope_id"):
                    continue

                # Topic overlap (Jaccard above conflict threshold)
                sim = _jaccard(tokens_a, tokens_b)
                if sim < self.conflict_threshold:
                    continue

                # Polarity mismatch
                if neg_a == neg_b:
                    continue

                pair_key = tuple(sorted([item_a["item_id"], item_b["item_id"]]))
                if pair_key in emitted_pairs:
                    continue
                emitted_pairs.add(pair_key)

                conflicts.append(
                    ConflictPair(
                        item_id_a=item_a["item_id"],
                        item_id_b=item_b["item_id"],
                        similarity=sim,
                    )
                )

        return conflicts

    def _detect_stale(self, items: list[dict[str, Any]]) -> list[StaleCandidate]:
        """Find low-access, old, non-pinned memories."""
        now = int(time.time())
        cutoff_age_seconds = self.stale_age_days * 86400

        stale_list: list[StaleCandidate] = []
        for item in items:
            if item.get("pinned"):
                continue
            access_count = int(item.get("access_count") or 0)
            if access_count > self.stale_max_access:
                continue
            created_at = int(item.get("created_at") or 0)
            if not created_at:
                continue
            age = now - created_at
            if age < cutoff_age_seconds:
                continue

            stale_list.append(
                StaleCandidate(
                    item_id=item["item_id"],
                    last_accessed_at=item.get("last_accessed_at"),
                    access_count=access_count,
                    age_days=age // 86400,
                )
            )

        return stale_list


# ====================================================================
# Phase 9 M9.6: Apply/reject maintenance suggestions
# ====================================================================


def apply_suggestion(
    suggestion_id: str,
    storage: Any,
    rag_manager: Any,
) -> tuple[bool, str]:
    """Apply a maintenance suggestion (dedupe/conflict/stale).

    Args:
        suggestion_id: Suggestion identifier
        storage: Database storage instance
        rag_manager: RagManager instance for memory operations

    Returns:
        (success, error_message)
    """
    import time
    import uuid

    # Load suggestion
    row = storage.fetchone(
        "SELECT * FROM memory_maintenance_suggestions WHERE suggestion_id = ?",
        (suggestion_id,),
    )
    if not row:
        return False, f"Suggestion not found: {suggestion_id}"

    if row["status"] != "pending":
        return False, f"Suggestion already {row['status']}"

    suggestion_type = row["suggestion_type"]
    item_id_a = row["item_id_a"]
    item_id_b = row.get("item_id_b")

    # Apply based on type
    try:
        if suggestion_type == "dedupe":
            # Keep item_a, archive item_b
            if not item_id_b:
                return False, "Dedupe suggestion missing item_id_b"
            # Archive duplicate (use rag_items.status, not lifecycle_stage)
            storage.execute(
                "UPDATE rag_items SET status = 'archived' WHERE item_id = ?",
                (item_id_b,),
            )
            try:
                storage._conn.commit()
            except Exception:
                pass

        elif suggestion_type == "conflict":
            # Mark both as conflicted (require manual review)
            # For now, just flag in metadata
            if not item_id_b:
                return False, "Conflict suggestion missing item_id_b"
            # Could add a conflict_flag column or metadata
            # For minimal implementation, just log and mark resolved
            pass

        elif suggestion_type == "stale":
            # Archive stale item (use rag_items.status, not lifecycle_stage)
            storage.execute(
                "UPDATE rag_items SET status = 'archived' WHERE item_id = ?",
                (item_id_a,),
            )
            try:
                storage._conn.commit()
            except Exception:
                pass

        else:
            return False, f"Unknown suggestion type: {suggestion_type}"

        # Mark suggestion as applied
        storage.execute(
            "UPDATE memory_maintenance_suggestions SET status = 'applied', resolved_at = ?, resolved_by = ? "
            "WHERE suggestion_id = ?",
            (int(time.time()), "system", suggestion_id),
        )
        try:
            storage._conn.commit()
        except Exception:
            pass
        return True, ""

    except Exception as e:
        return False, f"Apply failed: {e}"


def reject_suggestion(
    suggestion_id: str,
    storage: Any,
    reason: str = "",
) -> tuple[bool, str]:
    """Reject a maintenance suggestion.

    Args:
        suggestion_id: Suggestion identifier
        storage: Database storage instance
        reason: Optional rejection reason

    Returns:
        (success, error_message)
    """
    import time

    # Load suggestion
    row = storage.fetchone(
        "SELECT * FROM memory_maintenance_suggestions WHERE suggestion_id = ?",
        (suggestion_id,),
    )
    if not row:
        return False, f"Suggestion not found: {suggestion_id}"

    if row["status"] != "pending":
        return False, f"Suggestion already {row['status']}"

    # Mark as rejected
    storage.execute(
        "UPDATE memory_maintenance_suggestions SET status = 'rejected', resolved_at = ?, resolved_by = ? "
        "WHERE suggestion_id = ?",
        (int(time.time()), f"user:{reason}" if reason else "user", suggestion_id),
    )
    try:
        storage._conn.commit()
    except Exception:
        pass
    return True, ""
