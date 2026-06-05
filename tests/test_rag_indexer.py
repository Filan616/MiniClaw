"""Tests for Phase 8 M2: indexer (RagIndexer) including dedup, redaction, sensitivity."""

from __future__ import annotations

from pathlib import Path

import pytest

from mini_claw.config import AppConfig, RagConfig
from mini_claw.permissions.policy import PermissionPolicy
from mini_claw.rag.indexer import RagIndexer
from mini_claw.rag.redaction import count_secret_hits, redact_for_rag
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
    return Database(tmp_path / "indexer.db")


@pytest.fixture
def policy() -> PermissionPolicy:
    return PermissionPolicy(AppConfig().permissions)


@pytest.fixture
def indexer(storage: Database, config: RagConfig, policy: PermissionPolicy) -> RagIndexer:
    return RagIndexer(RagStore(storage), config, policy)


def _ctx(workspace_dir: Path, agent_id: str = "agent-a") -> dict:
    return {
        "agent_id": agent_id,
        "workspace_dir": str(workspace_dir),
        "sandbox_mode": "safe",
        "chat_id": "chat-1",
        "session_id": "sess-1",
        "channel_name": "cli",
    }


# ===================== Redaction helpers =====================


def test_redact_for_rag_strips_secrets_and_paths():
    text = "Authorization: Bearer abc123\nSee /Users/foo/secret.env"
    redacted, was_redacted = redact_for_rag(text)
    assert was_redacted is True
    assert "abc123" not in redacted
    assert "/Users/foo" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_for_rag_no_match_returns_original():
    text = "just normal text with no secrets"
    redacted, was_redacted = redact_for_rag(text)
    assert was_redacted is False
    assert redacted == text


def test_count_secret_hits():
    text = "api_key=foo\ntoken=bar\npassword=baz"
    assert count_secret_hits(text) == 3


# ===================== Indexer happy path =====================


def test_indexer_creates_item_and_chunks(indexer: RagIndexer, tmp_path: Path):
    md = tmp_path / "doc.md"
    md.write_text("# Title\n\nSome content here.\n", encoding="utf-8")

    item_id, error = indexer.index_path(str(md), ctx=_ctx(tmp_path))
    assert error == ""
    assert item_id is not None

    item = indexer.store.get_item(item_id)
    assert item.namespace == "context"
    assert item.source_type == "document"
    assert item.status == "active"
    assert item.content_hash

    chunks = indexer.store.get_chunks(item_id)
    assert len(chunks) >= 1


def test_indexer_accepts_path_workspace_dir(indexer: RagIndexer, tmp_path: Path):
    md = tmp_path / "doc.md"
    md.write_text("# Title\n\nSome content here.\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    ctx["workspace_dir"] = tmp_path

    item_id, error = indexer.index_path(str(md), ctx=ctx)

    assert error == ""
    assert item_id is not None
    item = indexer.store.get_item(item_id)
    assert isinstance(item.workspace_dir, str)
    assert isinstance(item.source_path, str)


def test_indexer_dedup_skips_duplicate(indexer: RagIndexer, tmp_path: Path):
    md = tmp_path / "doc.md"
    md.write_text("# Title\n\nSame content.\n", encoding="utf-8")

    item_id1, _ = indexer.index_path(str(md), ctx=_ctx(tmp_path))
    item_id2, msg = indexer.index_path(str(md), ctx=_ctx(tmp_path))
    assert item_id1 == item_id2
    assert "already indexed" in msg


def test_indexer_marks_high_sensitivity_when_secrets_detected(
    indexer: RagIndexer, tmp_path: Path
):
    py = tmp_path / "config.py"
    py.write_text(
        "api_key=abc123\ntoken=xyz\npassword=qwerty\nMORE_SECRET=hidden\n",
        encoding="utf-8",
    )
    item_id, error = indexer.index_path(str(py), ctx=_ctx(tmp_path))
    assert error == ""
    item = indexer.store.get_item(item_id)
    assert item.sensitivity_level in {"high", "medium"}


def test_indexer_detects_source_type_from_extension(indexer: RagIndexer, tmp_path: Path):
    py = tmp_path / "code.py"
    py.write_text("def foo():\n    return 1\n", encoding="utf-8")
    item_id, error = indexer.index_path(str(py), ctx=_ctx(tmp_path))
    assert error == ""
    assert indexer.store.get_item(item_id).source_type == "code"


# ===================== Indexer rejects =====================


def test_indexer_rejects_path_outside_workspace(
    indexer: RagIndexer, tmp_path: Path
):
    other = tmp_path.parent / "outside.md"
    other.write_text("not in workspace", encoding="utf-8")
    item_id, error = indexer.index_path(str(other), ctx=_ctx(tmp_path))
    assert item_id is None
    assert "outside workspace" in error or "workspace" in error.lower()


def test_indexer_rejects_in_bypass_mode(
    indexer: RagIndexer, tmp_path: Path, config: RagConfig
):
    md = tmp_path / "doc.md"
    md.write_text("# x", encoding="utf-8")
    ctx = _ctx(tmp_path)
    ctx["sandbox_mode"] = "bypass"
    item_id, error = indexer.index_path(str(md), ctx=ctx)
    assert item_id is None
    assert "bypass" in error.lower()


def test_indexer_rejects_nonexistent_file(indexer: RagIndexer, tmp_path: Path):
    item_id, error = indexer.index_path(
        str(tmp_path / "ghost.md"), ctx=_ctx(tmp_path)
    )
    assert item_id is None
    assert "not found" in error.lower()


def test_indexer_rejects_directory(indexer: RagIndexer, tmp_path: Path):
    sub = tmp_path / "sub"
    sub.mkdir()
    item_id, error = indexer.index_path(str(sub), ctx=_ctx(tmp_path))
    assert item_id is None
    assert "directory" in error.lower()


def test_indexer_rejects_oversized_file(
    indexer: RagIndexer, tmp_path: Path, config: RagConfig
):
    config.chunk.max_file_size_mb = 1  # 1 MB limit
    big = tmp_path / "big.md"
    big.write_bytes(b"x" * (2 * 1024 * 1024))  # 2 MB
    item_id, error = indexer.index_path(str(big), ctx=_ctx(tmp_path))
    assert item_id is None
    assert "max size" in error.lower() or "exceeds" in error.lower()


def test_indexer_rejects_binary_file(
    indexer: RagIndexer, tmp_path: Path, config: RagConfig
):
    binary = tmp_path / "data.bin"
    binary.write_bytes(b"\x00\x01\x02\x03" * 100)
    item_id, error = indexer.index_path(str(binary), ctx=_ctx(tmp_path))
    assert item_id is None
    assert "binary" in error.lower()
