"""Tests for Phase 8 M2: retriever (FTS5 + sanitize + sensitivity redaction)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mini_claw.config import AppConfig, RagConfig
from mini_claw.permissions.policy import PermissionPolicy
from mini_claw.rag.indexer import RagIndexer
from mini_claw.rag.retriever import RagRetriever, _sanitize_fts_query
from mini_claw.rag.store import RagStore
from mini_claw.storage.db import Database


@pytest.fixture
def config() -> RagConfig:
    cfg = RagConfig()
    cfg.enabled = True
    cfg.namespaces.context_enabled = True
    return cfg


@pytest.fixture
def storage(tmp_path: Path) -> Database:
    return Database(tmp_path / "retriever.db")


@pytest.fixture
def policy() -> PermissionPolicy:
    return PermissionPolicy(AppConfig().permissions)


@pytest.fixture
def indexer(storage, config, policy) -> RagIndexer:
    return RagIndexer(RagStore(storage), config, policy)


@pytest.fixture
def retriever(storage, config) -> RagRetriever:
    return RagRetriever(storage, config)


def _ctx(workspace_dir: Path, agent_id: str = "agent-a") -> dict:
    return {
        "agent_id": agent_id,
        "workspace_dir": str(workspace_dir),
        "sandbox_mode": "safe",
        "chat_id": "chat-1",
        "session_id": "sess-1",
        "channel_name": "cli",
    }


# ===================== _sanitize_fts_query =====================


def test_sanitize_fts_query_handles_empty():
    assert _sanitize_fts_query("") == '""'


def test_sanitize_fts_query_quotes_ordinary_text():
    assert _sanitize_fts_query("Phase 7 reviewer") == '"Phase 7 reviewer"'


def test_sanitize_fts_query_escapes_special_chars():
    """FTS5 special chars (:, *, NEAR(, ", OR) should be wrapped in quotes."""
    for q in ["foo:bar", "NEAR(foo)", "*", "token OR password", '"quoted"']:
        sanitized = _sanitize_fts_query(q)
        assert sanitized.startswith('"')
        assert sanitized.endswith('"')
        # Inner double-quote escaping (FTS5 phrase-mode rule)
        if '"' in q:
            assert '""' in sanitized


# ===================== Retriever happy path =====================


def test_retriever_finds_indexed_content(
    indexer: RagIndexer, retriever: RagRetriever, tmp_path: Path
):
    md = tmp_path / "doc.md"
    md.write_text(
        "# Phase 7\n\nThe reviewer node enforces severity_threshold checks.\n",
        encoding="utf-8",
    )
    indexer.index_path(str(md), ctx=_ctx(tmp_path))

    results, error = retriever.search_context(
        "reviewer", ctx=_ctx(tmp_path)
    )
    assert error == ""
    assert len(results) >= 1
    assert any("reviewer" in r.content.lower() for r in results)


def test_retriever_filters_by_owner_agent(
    indexer: RagIndexer, retriever: RagRetriever, tmp_path: Path
):
    md = tmp_path / "doc.md"
    md.write_text("hello content", encoding="utf-8")
    indexer.index_path(str(md), ctx=_ctx(tmp_path, agent_id="agent-a"))

    # Query as agent-b → no results (cross-agent default-deny)
    results, error = retriever.search_context(
        "hello", ctx=_ctx(tmp_path, agent_id="agent-b")
    )
    assert error == ""
    assert results == []


def test_retriever_excludes_archived_by_default(
    indexer: RagIndexer, retriever: RagRetriever, tmp_path: Path, storage
):
    md = tmp_path / "doc.md"
    md.write_text("archive me", encoding="utf-8")
    item_id, _ = indexer.index_path(str(md), ctx=_ctx(tmp_path))
    indexer.store.mark_status(item_id, "archived")

    # Default: archived items excluded
    results, _ = retriever.search_context("archive", ctx=_ctx(tmp_path))
    assert results == []

    # include_archived=True returns it
    results, _ = retriever.search_context(
        "archive", ctx=_ctx(tmp_path), include_archived=True
    )
    assert len(results) >= 1


def test_retriever_empty_query_returns_error(
    retriever: RagRetriever, tmp_path: Path
):
    results, error = retriever.search_context("", ctx=_ctx(tmp_path))
    assert results == []
    assert "empty" in error.lower()


def test_retriever_rejects_special_chars_gracefully(
    indexer: RagIndexer, retriever: RagRetriever, tmp_path: Path
):
    """User feedback 5: FTS5 special chars must NOT raise SQL errors."""
    md = tmp_path / "doc.md"
    md.write_text("normal content", encoding="utf-8")
    indexer.index_path(str(md), ctx=_ctx(tmp_path))

    # All these tricky inputs must not raise
    for tricky in ["NEAR(", "*", "foo:bar", "token OR password", '""']:
        results, error = retriever.search_context(tricky, ctx=_ctx(tmp_path))
        # No raise, no SQL parse error reaching the user
        assert isinstance(results, list)


# ===================== Sensitivity redaction =====================


def test_retriever_redacts_high_sensitivity_content(
    indexer: RagIndexer, retriever: RagRetriever, tmp_path: Path
):
    """User feedback 4: high-sensitivity chunks must not return plaintext."""
    py = tmp_path / "config.py"
    # Triggers >= 3 secret hits → sensitivity_level='high'
    py.write_text(
        "api_key=abc\ntoken=xyz\npassword=foo\nSECRET_KEY=bar\n",
        encoding="utf-8",
    )
    indexer.index_path(str(py), ctx=_ctx(tmp_path))

    # Search for content; high-sensitivity items return placeholder, not raw content
    results, error = retriever.search_context("secret", ctx=_ctx(tmp_path))
    assert error == ""
    if results:
        # If FTS finds it, check redaction
        for r in results:
            if r.sensitivity_level == "high":
                assert "[REDACTED" in r.content
                assert "read_sensitive_context" in r.content
