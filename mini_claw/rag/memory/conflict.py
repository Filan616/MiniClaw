"""Phase 9 M9.6: Memory conflict detector.

Pure text-based rules: detect memories with overlapping topics but contradictory
polarity (e.g., "enable X" vs "disable X", "use Y" vs "avoid Y").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ConflictPair:
    """Pair of conflicting memory items."""

    item_id_a: str
    item_id_b: str
    reason: str
    confidence: float


# Negation/contradiction markers
_NEGATION_WORDS = {"not", "no", "never", "avoid", "disable", "don't", "shouldn't", "can't"}
_AFFIRMATION_WORDS = {"enable", "use", "allow", "should", "must", "always", "prefer"}


def find_conflicts(
    items: list[dict[str, Any]],
    threshold: float = 0.55,
) -> list[ConflictPair]:
    """Find conflicting memories using topic overlap + polarity mismatch.

    Args:
        items: List of memory items with 'item_id', 'content', 'memory_type', 'scope_type', 'scope_id'
        threshold: Topic overlap threshold (Jaccard similarity on keywords)

    Returns:
        List of ConflictPair for memories in same scope+type with topic overlap but opposite polarity
    """
    if len(items) < 2:
        return []

    conflicts = []

    # Group by (scope_type, scope_id, memory_type)
    groups: dict[tuple, list[dict]] = {}
    for item in items:
        key = (item.get("scope_type"), item.get("scope_id"), item.get("memory_type"))
        groups.setdefault(key, []).append(item)

    # Check within each group
    for group in groups.values():
        if len(group) < 2:
            continue

        # Extract keywords and polarity for each item
        parsed = []
        for item in group:
            content = item.get("content", "").lower()
            keywords = _extract_keywords(content)
            polarity = _detect_polarity(content)
            parsed.append((item["item_id"], keywords, polarity))

        # Find pairs with topic overlap but opposite polarity
        for i in range(len(parsed)):
            id_a, keywords_a, polarity_a = parsed[i]
            for j in range(i + 1, len(parsed)):
                id_b, keywords_b, polarity_b = parsed[j]

                # Check topic overlap
                overlap = _jaccard_similarity(keywords_a, keywords_b)
                if overlap < threshold:
                    continue

                # Check polarity mismatch
                if polarity_a == polarity_b:
                    continue  # Same polarity, no conflict

                if polarity_a == "neutral" or polarity_b == "neutral":
                    continue  # Need clear polarity on both sides

                # Found conflict
                conflicts.append(
                    ConflictPair(
                        item_id_a=id_a,
                        item_id_b=id_b,
                        reason=f"Topic overlap {overlap:.2f}, opposite polarity ({polarity_a} vs {polarity_b})",
                        confidence=overlap,
                    )
                )

    return conflicts


def _extract_keywords(text: str) -> set[str]:
    """Extract significant keywords (nouns/verbs, skip common words)."""
    import re
    tokens = re.findall(r'\w+', text.lower())
    # Skip stop words and very short tokens
    stop_words = {"the", "a", "an", "is", "are", "was", "were", "in", "on", "at", "to", "for", "of", "and", "or"}
    keywords = {t for t in tokens if len(t) > 2 and t not in stop_words}
    return keywords


def _detect_polarity(text: str) -> str:
    """Detect polarity: affirmative/negative/neutral."""
    has_negation = any(word in text for word in _NEGATION_WORDS)
    has_affirmation = any(word in text for word in _AFFIRMATION_WORDS)

    if has_negation and not has_affirmation:
        return "negative"
    elif has_affirmation and not has_negation:
        return "affirmative"
    else:
        return "neutral"


def _jaccard_similarity(set_a: set, set_b: set) -> float:
    """Jaccard similarity: |A ∩ B| / |A ∪ B|."""
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0
