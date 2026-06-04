"""Phase 9 M9.6: Memory deduplication detector.

Text-only baseline using Jaccard similarity on tokenized content.
Hybrid mode combines Jaccard text similarity with embedding cosine similarity.

Configuration:
- mode="text_only": Only use Jaccard similarity (dedupe_text_threshold)
- mode="hybrid": Require BOTH text similarity AND embedding similarity
- mode="auto": Use hybrid if embeddings available, else text_only
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["DuplicateGroup", "find_duplicates"]


@dataclass
class DuplicateGroup:
    """Group of duplicate memory items."""

    item_ids: list[str]
    similarity: float
    reason: str


def find_duplicates(
    items: list[dict[str, Any]],
    threshold: float = 0.75,
    mode: str = "text_only",
    embedding_threshold: float = 0.92,
    embedder: Any = None,
) -> list[DuplicateGroup]:
    """Find duplicate memories using text and/or embedding similarity.

    Args:
        items: List of memory items with 'item_id' and 'content' fields
        threshold: Text Jaccard similarity threshold (default 0.75)
        mode: "text_only" | "hybrid" | "auto"
        embedding_threshold: Cosine similarity threshold for hybrid mode (default 0.92)
        embedder: Optional embedding provider for hybrid mode

    Returns:
        List of DuplicateGroup, each containing 2+ similar items
    """
    if len(items) < 2:
        return []

    # Determine effective mode
    effective_mode = mode
    if mode == "auto":
        effective_mode = "hybrid" if embedder is not None else "text_only"

    # Tokenize all items
    tokenized = []
    for item in items:
        content = item.get("content", "")
        tokens = set(_tokenize(content))
        tokenized.append((item["item_id"], tokens, content))

    # Embed all items if hybrid mode
    embeddings: dict[str, list[float]] = {}
    if effective_mode == "hybrid" and embedder is not None:
        texts = [content for _, _, content in tokenized]
        try:
            vectors = embedder.embed_texts(texts)
            for (item_id, _, _), vec in zip(tokenized, vectors):
                embeddings[item_id] = vec
        except Exception:
            # Fall back to text_only on embedding failure
            effective_mode = "text_only"

    # Find pairs with high similarity
    duplicates = []
    seen_pairs = set()

    for i in range(len(tokenized)):
        id_a, tokens_a, _ = tokenized[i]
        for j in range(i + 1, len(tokenized)):
            id_b, tokens_b, _ = tokenized[j]

            if (id_a, id_b) in seen_pairs:
                continue

            # Text similarity check
            text_sim = _jaccard_similarity(tokens_a, tokens_b)
            if text_sim < threshold:
                continue

            # Hybrid mode: require BOTH text AND embedding similarity
            if effective_mode == "hybrid":
                if id_a not in embeddings or id_b not in embeddings:
                    continue
                emb_sim = _cosine_similarity(embeddings[id_a], embeddings[id_b])
                if emb_sim < embedding_threshold:
                    continue
                # Both thresholds met
                seen_pairs.add((id_a, id_b))
                duplicates.append(
                    DuplicateGroup(
                        item_ids=[id_a, id_b],
                        similarity=(text_sim + emb_sim) / 2,
                        reason=f"Hybrid: text={text_sim:.2f} embedding={emb_sim:.2f}",
                    )
                )
            else:
                # Text-only mode
                seen_pairs.add((id_a, id_b))
                duplicates.append(
                    DuplicateGroup(
                        item_ids=[id_a, id_b],
                        similarity=text_sim,
                        reason=f"Text Jaccard similarity {text_sim:.2f}",
                    )
                )

    return duplicates


def _tokenize(text: str) -> list[str]:
    """Simple tokenization: lowercase + split on whitespace/punctuation."""
    import re
    text = text.lower()
    # Split on non-alphanumeric
    tokens = re.findall(r'\w+', text)
    return tokens


def _jaccard_similarity(set_a: set, set_b: set) -> float:
    """Jaccard similarity: |A ∩ B| / |A ∪ B|."""
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0


def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """Cosine similarity between two embedding vectors.

    Returns value in [0, 1] range, where 1 is identical.
    """
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0

    # Compute dot product and magnitudes
    dot_product = sum(a * b for a, b in zip(vec_a, vec_b))
    mag_a = sum(a * a for a in vec_a) ** 0.5
    mag_b = sum(b * b for b in vec_b) ** 0.5

    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0

    # Cosine similarity in [-1, 1], normalize to [0, 1]
    cosine = dot_product / (mag_a * mag_b)
    return (cosine + 1.0) / 2.0
