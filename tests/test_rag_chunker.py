"""Tests for Phase 8 M2: chunker (Document / Code / Log)."""

from __future__ import annotations

from mini_claw.rag.chunker import CodeChunker, DocumentChunker, LogChunker, chunk_to_tokens


# ===================== chunk_to_tokens =====================


def test_chunk_to_tokens_short_text_single_chunk():
    text = "hello world"
    chunks = chunk_to_tokens(text, max_tokens=100, overlap_tokens=10)
    assert chunks == ["hello world"]


def test_chunk_to_tokens_splits_long_text():
    text = "a" * 10000  # 10000 chars ~ 2500 tokens
    chunks = chunk_to_tokens(text, max_tokens=200, overlap_tokens=20)
    assert len(chunks) > 1
    # Each chunk should be roughly max_tokens * 4 chars
    for c in chunks:
        assert len(c) <= 200 * 4 + 100  # some tolerance for newline-break alignment


def test_chunk_to_tokens_overlap_creates_continuity():
    text = "line1\nline2\nline3\n" * 200
    chunks = chunk_to_tokens(text, max_tokens=100, overlap_tokens=20)
    assert len(chunks) > 1


# ===================== DocumentChunker =====================


def test_document_chunker_splits_markdown_by_headers():
    text = """# Title

intro paragraph

## Section A

content A

## Section B

content B
"""
    chunker = DocumentChunker(max_tokens=800, overlap_tokens=100)
    chunks = list(chunker.chunk(text, source_path="test.md"))
    assert len(chunks) >= 2
    titles = [c["section_title"] for c in chunks]
    # At least one chunk should have "Section A" or "Section B"
    assert any(t and "Section" in t for t in titles)


def test_document_chunker_paragraph_fallback_for_plain_text():
    text = "para1 line1\npara1 line2\n\npara2 line1\n\npara3 only"
    chunker = DocumentChunker()
    chunks = list(chunker.chunk(text, source_path="notes.txt"))
    assert len(chunks) == 3


def test_document_chunker_empty_text_yields_nothing():
    chunker = DocumentChunker()
    assert list(chunker.chunk("   \n", source_path="empty.md")) == []


def test_document_chunker_records_line_numbers():
    text = "# H1\n\nbody\n\n## H2\n\nmore"
    chunker = DocumentChunker()
    chunks = list(chunker.chunk(text, source_path="doc.md"))
    # All chunks must have start_line / end_line populated
    for c in chunks:
        assert c["start_line"] >= 1
        assert c["end_line"] >= c["start_line"]


# ===================== CodeChunker =====================


def test_code_chunker_splits_by_def_and_class():
    text = """import x

def foo():
    return 1

class Bar:
    def baz(self):
        return 2

def standalone():
    pass
"""
    chunker = CodeChunker()
    chunks = list(chunker.chunk(text, source_path="test.py"))
    # Should detect at least def foo, class Bar, def standalone
    symbols = [c["symbol_name"] for c in chunks if c["symbol_name"]]
    assert any("foo" in s for s in symbols)
    assert any("Bar" in s for s in symbols)


def test_code_chunker_detects_language_from_extension():
    chunker = CodeChunker()
    chunks = list(chunker.chunk("def x(): pass", source_path="a.py"))
    assert chunks[0]["language"] == "python"
    chunks = list(chunker.chunk("function y(){}", source_path="a.js"))
    assert chunks[0]["language"] == "javascript"


def test_code_chunker_handles_no_definitions():
    text = "# just comments\n# nothing else"
    chunker = CodeChunker()
    chunks = list(chunker.chunk(text, source_path="empty.py"))
    # Whole file becomes a single chunk with no symbol
    assert len(chunks) == 1
    assert chunks[0]["symbol_name"] is None


# ===================== LogChunker =====================


def test_log_chunker_splits_traceback_blocks():
    text = """
INFO: starting up

Traceback (most recent call last):
  File "a.py", line 1, in <module>
    raise ValueError("bad")
ValueError: bad

INFO: continuing

Traceback (most recent call last):
  File "b.py", line 2
    raise IOError
IOError
"""
    chunker = LogChunker()
    chunks = list(chunker.chunk(text, source_path="run.log"))
    traceback_chunks = [c for c in chunks if c["section_title"] == "Traceback"]
    assert len(traceback_chunks) == 2


def test_log_chunker_splits_error_lines():
    text = """
INFO: ok
ERROR: something failed
WARN: minor issue
ERROR: another failure
"""
    chunker = LogChunker()
    chunks = list(chunker.chunk(text, source_path="app.log"))
    error_chunks = [c for c in chunks if c["section_title"] == "ERROR"]
    warn_chunks = [c for c in chunks if c["section_title"] == "WARN"]
    assert len(error_chunks) == 2
    assert len(warn_chunks) == 1
