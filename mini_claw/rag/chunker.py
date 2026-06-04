"""RAG content chunking (Phase 8 M2).

Three chunker types:
- DocumentChunker: markdown / text / html / json / yaml
- CodeChunker: py / js / ts / java / go / cpp / c / rs / sh (M2: regex-based)
- LogChunker: traceback / ERROR blocks

All chunkers respect max_tokens and overlap_tokens from config.
"""

from __future__ import annotations

import re
from typing import Iterator

__all__ = ["DocumentChunker", "CodeChunker", "LogChunker", "chunk_to_tokens"]


def chunk_to_tokens(text: str, max_tokens: int, overlap_tokens: int) -> list[str]:
    """Naive token-based chunking (char/4 ≈ token estimate).

    Used as fallback when semantic chunking (markdown headers, def/class)
    cannot split a large block.
    """
    # Rough token estimate: 1 token ≈ 4 chars (OpenAI rule of thumb)
    chars_per_chunk = max_tokens * 4
    overlap_chars = overlap_tokens * 4

    if len(text) <= chars_per_chunk:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + chars_per_chunk, len(text))
        # Try to break at newline within last 20% of chunk
        if end < len(text):
            search_start = max(start, end - chars_per_chunk // 5)
            newline_pos = text.rfind("\n", search_start, end)
            if newline_pos > start:
                end = newline_pos + 1

        chunks.append(text[start:end])
        if end >= len(text):
            break
        # Advance start: ensure forward progress even when overlap is large
        next_start = end - overlap_chars if overlap_chars > 0 else end
        # Guarantee at least 1 char of progress to prevent infinite loop
        if next_start <= start:
            next_start = start + max(1, chars_per_chunk // 2)
        start = next_start

    return chunks


class DocumentChunker:
    """Chunker for markdown / text / html / json / yaml."""

    def __init__(self, max_tokens: int = 800, overlap_tokens: int = 100):
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens

    def chunk(self, text: str, source_path: str | None = None) -> Iterator[dict]:
        """Yield chunk dicts with keys: content, section_title, start_line, end_line."""
        if not text.strip():
            return

        # Markdown header-based chunking
        if source_path and source_path.endswith((".md", ".markdown")):
            yield from self._chunk_markdown(text)
        # Fallback: split by double newlines (paragraphs), then by token limit
        else:
            yield from self._chunk_paragraphs(text)

    def _chunk_markdown(self, text: str) -> Iterator[dict]:
        """Split by markdown headers (# / ## / ###), then by token limit."""
        # Match markdown headers: ^#{1,6} ...
        header_pattern = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
        lines = text.splitlines(keepends=True)
        sections: list[tuple[str | None, int, str]] = []  # (title, start_line, content)

        current_title: str | None = None
        current_start = 0
        current_content: list[str] = []

        for i, line in enumerate(lines, start=1):
            match = header_pattern.match(line)
            if match:
                # Save previous section
                if current_content:
                    sections.append(
                        (current_title, current_start, "".join(current_content))
                    )
                # Start new section
                current_title = match.group(2).strip()
                current_start = i
                current_content = [line]
            else:
                current_content.append(line)

        # Save final section
        if current_content:
            sections.append((current_title, current_start, "".join(current_content)))

        # Now split large sections by token limit
        for title, start_line, content in sections:
            # Estimate tokens
            if len(content) <= self.max_tokens * 4:
                yield {
                    "content": content.strip(),
                    "section_title": title,
                    "start_line": start_line,
                    "end_line": start_line + content.count("\n"),
                }
            else:
                # Section too large, split by token limit
                sub_chunks = chunk_to_tokens(
                    content, self.max_tokens, self.overlap_tokens
                )
                line_offset = start_line
                for sub in sub_chunks:
                    yield {
                        "content": sub.strip(),
                        "section_title": title,
                        "start_line": line_offset,
                        "end_line": line_offset + sub.count("\n"),
                    }
                    line_offset += sub.count("\n") - self.overlap_tokens // 4

    def _chunk_paragraphs(self, text: str) -> Iterator[dict]:
        """Split by double newlines, then by token limit."""
        paragraphs = re.split(r"\n\n+", text)
        line_offset = 1

        for para in paragraphs:
            if not para.strip():
                continue
            if len(para) <= self.max_tokens * 4:
                yield {
                    "content": para.strip(),
                    "section_title": None,
                    "start_line": line_offset,
                    "end_line": line_offset + para.count("\n"),
                }
                line_offset += para.count("\n") + 2  # +2 for paragraph separator
            else:
                # Paragraph too large, split by token limit
                sub_chunks = chunk_to_tokens(
                    para, self.max_tokens, self.overlap_tokens
                )
                for sub in sub_chunks:
                    yield {
                        "content": sub.strip(),
                        "section_title": None,
                        "start_line": line_offset,
                        "end_line": line_offset + sub.count("\n"),
                    }
                    line_offset += sub.count("\n") - self.overlap_tokens // 4


class CodeChunker:
    """Chunker for code files (py/js/ts/java/go/cpp/c/rs/sh).

    M2: regex-based function/class detection.
    M3+: AST-based chunking for tighter boundaries.
    """

    def __init__(self, max_tokens: int = 800, overlap_tokens: int = 100):
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens
        # Match Python/JS/Java function/class definitions
        self._def_pattern = re.compile(
            r"^(def\s+\w+|class\s+\w+|function\s+\w+|const\s+\w+\s*=\s*\(|"
            r"public\s+\w+\s+\w+\(|private\s+\w+\s+\w+\(|protected\s+\w+\s+\w+\(|"
            r"func\s+\w+|fn\s+\w+)\b",
            re.MULTILINE,
        )

    def chunk(self, text: str, source_path: str | None = None) -> Iterator[dict]:
        """Yield chunk dicts with keys: content, symbol_name, language, start_line, end_line."""
        if not text.strip():
            return

        language = self._detect_language(source_path) if source_path else None
        lines = text.splitlines(keepends=True)
        symbols: list[tuple[str | None, int, str]] = []  # (symbol_name, start_line, content)

        current_symbol: str | None = None
        current_start = 0
        current_content: list[str] = []

        for i, line in enumerate(lines, start=1):
            match = self._def_pattern.match(line)
            if match:
                # Save previous symbol
                if current_content:
                    symbols.append(
                        (current_symbol, current_start, "".join(current_content))
                    )
                # Start new symbol
                current_symbol = match.group(1).strip()
                current_start = i
                current_content = [line]
            else:
                current_content.append(line)

        # Save final symbol
        if current_content:
            symbols.append((current_symbol, current_start, "".join(current_content)))

        # Split large symbols by token limit
        for symbol, start_line, content in symbols:
            if len(content) <= self.max_tokens * 4:
                yield {
                    "content": content.strip(),
                    "symbol_name": symbol,
                    "language": language,
                    "start_line": start_line,
                    "end_line": start_line + content.count("\n"),
                }
            else:
                # Symbol too large, split by token limit
                sub_chunks = chunk_to_tokens(
                    content, self.max_tokens, self.overlap_tokens
                )
                line_offset = start_line
                for sub in sub_chunks:
                    yield {
                        "content": sub.strip(),
                        "symbol_name": symbol,
                        "language": language,
                        "start_line": line_offset,
                        "end_line": line_offset + sub.count("\n"),
                    }
                    line_offset += sub.count("\n") - self.overlap_tokens // 4

    def _detect_language(self, path: str) -> str | None:
        """Detect language from file extension."""
        ext_map = {
            ".py": "python",
            ".js": "javascript",
            ".ts": "typescript",
            ".java": "java",
            ".go": "go",
            ".cpp": "cpp",
            ".c": "c",
            ".rs": "rust",
            ".sh": "shell",
        }
        for ext, lang in ext_map.items():
            if path.endswith(ext):
                return lang
        return None


class LogChunker:
    """Chunker for log files / traceback blocks."""

    def __init__(self, max_tokens: int = 800, overlap_tokens: int = 100):
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens
        # Match Python tracebacks, ERROR/WARN blocks
        self._traceback_pattern = re.compile(
            r"^Traceback \(most recent call last\):", re.MULTILINE
        )
        self._error_pattern = re.compile(r"^(ERROR|WARN|WARNING|CRITICAL):", re.MULTILINE)

    def chunk(self, text: str, source_path: str | None = None) -> Iterator[dict]:
        """Yield chunk dicts with keys: content, section_title, start_line, end_line."""
        if not text.strip():
            return

        lines = text.splitlines(keepends=True)
        blocks: list[tuple[str | None, int, str]] = []  # (title, start_line, content)

        current_title: str | None = None
        current_start = 0
        current_content: list[str] = []

        for i, line in enumerate(lines, start=1):
            # Check for traceback start
            if self._traceback_pattern.match(line):
                if current_content:
                    blocks.append(
                        (current_title, current_start, "".join(current_content))
                    )
                current_title = "Traceback"
                current_start = i
                current_content = [line]
            # Check for ERROR/WARN
            elif self._error_pattern.match(line):
                if current_content:
                    blocks.append(
                        (current_title, current_start, "".join(current_content))
                    )
                match = self._error_pattern.match(line)
                current_title = match.group(1) if match else "ERROR"
                current_start = i
                current_content = [line]
            else:
                current_content.append(line)

        # Save final block
        if current_content:
            blocks.append((current_title, current_start, "".join(current_content)))

        # Split large blocks by token limit
        for title, start_line, content in blocks:
            if len(content) <= self.max_tokens * 4:
                yield {
                    "content": content.strip(),
                    "section_title": title,
                    "start_line": start_line,
                    "end_line": start_line + content.count("\n"),
                }
            else:
                # Block too large, split by token limit
                sub_chunks = chunk_to_tokens(
                    content, self.max_tokens, self.overlap_tokens
                )
                line_offset = start_line
                for sub in sub_chunks:
                    yield {
                        "content": sub.strip(),
                        "section_title": title,
                        "start_line": line_offset,
                        "end_line": line_offset + sub.count("\n"),
                    }
                    line_offset += sub.count("\n") - self.overlap_tokens // 4
