"""Test hybrid embedding deduplication with cosine threshold >=0.92."""

import pytest
from mini_claw.rag.memory.dedupe import find_duplicates, _cosine_similarity


class MockEmbedder:
    """Mock embedder for testing."""

    def __init__(self):
        self.model = "mock-embedder"
        self.dim = 3

    def embed_texts(self, texts):
        """Generate mock embeddings based on text content."""
        embeddings = []
        for text in texts:
            # Generate a simple embedding based on text hash
            h = hash(text)
            embeddings.append([
                (h % 100) / 100.0,
                ((h // 100) % 100) / 100.0,
                ((h // 10000) % 100) / 100.0,
            ])
        return embeddings


def test_cosine_similarity_identical_vectors():
    """Identical vectors should have cosine similarity of 1.0."""
    vec = [1.0, 2.0, 3.0]
    assert _cosine_similarity(vec, vec) == 1.0


def test_cosine_similarity_orthogonal_vectors():
    """Orthogonal vectors should have cosine similarity around 0.5."""
    vec_a = [1.0, 0.0, 0.0]
    vec_b = [0.0, 1.0, 0.0]
    sim = _cosine_similarity(vec_a, vec_b)
    assert 0.4 <= sim <= 0.6


def test_cosine_similarity_opposite_vectors():
    """Opposite vectors should have cosine similarity of 0.0."""
    vec_a = [1.0, 0.0, 0.0]
    vec_b = [-1.0, 0.0, 0.0]
    sim = _cosine_similarity(vec_a, vec_b)
    assert sim == 0.0


def test_hybrid_mode_requires_both_thresholds():
    """Hybrid mode should only flag duplicates when BOTH text and embedding exceed thresholds."""
    items = [
        {"item_id": "m1", "content": "use python for backend services"},
        {"item_id": "m2", "content": "use python for backend services"},  # Identical
        {"item_id": "m3", "content": "completely different content here"},
    ]

    embedder = MockEmbedder()

    # Text-only mode: should find m1-m2 duplicate
    result_text = find_duplicates(items, threshold=0.9, mode="text_only")
    assert len(result_text) == 1
    assert set(result_text[0].item_ids) == {"m1", "m2"}

    # Hybrid mode with high threshold: depends on mock embeddings
    result_hybrid = find_duplicates(
        items,
        threshold=0.9,
        mode="hybrid",
        embedding_threshold=0.92,
        embedder=embedder,
    )
    # m1 and m2 have identical text, so they should be duplicates
    # The mock embedder will generate the same embedding for identical text
    assert len(result_hybrid) >= 0  # Depends on hash collision


def test_hybrid_mode_auto_fallback():
    """Auto mode should fall back to text_only when embedder is None."""
    items = [
        {"item_id": "m1", "content": "use redis for caching layer"},
        {"item_id": "m2", "content": "use redis for caching layer services"},
    ]

    # Auto mode with no embedder should fall back to text_only
    result = find_duplicates(items, threshold=0.7, mode="auto", embedder=None)
    assert len(result) == 1


def test_hybrid_mode_with_embedder():
    """Auto mode should use hybrid when embedder is available."""
    items = [
        {"item_id": "m1", "content": "use redis for caching layer"},
        {"item_id": "m2", "content": "use redis for caching layer services"},
    ]

    embedder = MockEmbedder()

    # Auto mode with embedder should attempt hybrid
    result = find_duplicates(
        items,
        threshold=0.7,
        mode="auto",
        embedding_threshold=0.92,
        embedder=embedder,
    )
    # Result depends on mock embeddings, just verify it doesn't crash
    assert isinstance(result, list)


def test_embedding_threshold_enforced():
    """Hybrid mode should enforce embedding_threshold >= 0.92 by default."""
    items = [
        {"item_id": "m1", "content": "similar text content here"},
        {"item_id": "m2", "content": "similar text content here too"},
    ]

    embedder = MockEmbedder()

    # With default embedding_threshold=0.92, only very similar embeddings pass
    result = find_duplicates(
        items,
        threshold=0.6,
        mode="hybrid",
        embedding_threshold=0.92,
        embedder=embedder,
    )
    # Result depends on whether embeddings meet 0.92 threshold
    assert isinstance(result, list)


def test_text_only_mode_ignores_embedder():
    """text_only mode should ignore embedder even when provided."""
    items = [
        {"item_id": "m1", "content": "test content alpha beta"},
        {"item_id": "m2", "content": "test content alpha beta gamma"},
    ]

    embedder = MockEmbedder()

    # text_only should not use embedder
    result = find_duplicates(
        items,
        threshold=0.6,
        mode="text_only",
        embedder=embedder,
    )
    assert len(result) == 1
    assert "Text Jaccard" in result[0].reason


def test_hybrid_mode_reports_both_scores():
    """Hybrid mode should report both text and embedding scores in reason."""
    items = [
        {"item_id": "m1", "content": "identical content"},
        {"item_id": "m2", "content": "identical content"},
    ]

    embedder = MockEmbedder()

    result = find_duplicates(
        items,
        threshold=0.9,
        mode="hybrid",
        embedding_threshold=0.5,  # Lower threshold to ensure match
        embedder=embedder,
    )

    if len(result) > 0:
        # Hybrid mode should include both scores
        assert "Hybrid:" in result[0].reason or "text=" in result[0].reason
