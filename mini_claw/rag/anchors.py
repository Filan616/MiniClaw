"""Anchor extraction for RAG incremental reindex.

Code contexts use Tree-sitter when the optional ``rag-code`` extra is
installed. Document/log contexts use stable line/title/content anchors.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

__all__ = [
    "ANCHOR_SCHEMA_VERSION",
    "CHUNKER_VERSION",
    "AnchorExtraction",
    "AnchorExtractor",
    "content_hash",
    "similarity",
]

CHUNKER_VERSION = "chunker.v1"
ANCHOR_SCHEMA_VERSION = "anchor.v1"

_CODE_EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".java": "java",
    ".go": "go",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".c": "c",
    ".rs": "rust",
    ".sh": "bash",
}

_SYMBOL_NODE_TYPES = {
    "class_definition": ("class", "name"),
    "function_definition": ("function", "name"),
    "method_definition": ("method", "name"),
    "function_declaration": ("function", "name"),
    "method_declaration": ("method", "name"),
    "lexical_declaration": ("symbol", None),
    "variable_declarator": ("symbol", "name"),
    "arrow_function": ("function", None),
}


@dataclass(slots=True)
class AnchorExtraction:
    parser_backend: str
    parser_status: str
    language: str | None
    tree_sitter_version: str | None = None
    tree_sitter_language_version: str | None = None
    parse_error_ratio: float = 0.0
    reason: str | None = None
    chunk_metadata: list[dict[str, Any]] = field(default_factory=list)

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "parser_backend": self.parser_backend,
            "parser_status": self.parser_status,
            "tree_sitter_version": self.tree_sitter_version,
            "tree_sitter_language": self.language,
            "tree_sitter_language_version": self.tree_sitter_language_version,
            "parse_error_ratio": self.parse_error_ratio,
            "reason": self.reason,
        }


class AnchorExtractor:
    """Generate stable anchors for chunks."""

    def __init__(
        self,
        *,
        chunker_version: str = CHUNKER_VERSION,
        anchor_schema_version: str = ANCHOR_SCHEMA_VERSION,
        parse_error_ratio_threshold: float = 0.20,
    ) -> None:
        self.chunker_version = chunker_version
        self.anchor_schema_version = anchor_schema_version
        self.parse_error_ratio_threshold = parse_error_ratio_threshold

    def enrich_chunks(
        self,
        chunks: list[dict[str, Any]],
        *,
        path: str,
        source_type: str,
        content: str,
    ) -> AnchorExtraction:
        language = _language_for_path(path)
        if source_type == "code" and language:
            extraction = self._tree_sitter_extract(content, language)
            if extraction.parser_status == "ok":
                metas = [
                    self._metadata_for_code_chunk(chunk, extraction.chunk_metadata, path)
                    for chunk in chunks
                ]
            else:
                metas = [
                    self._metadata_for_document_chunk(chunk, path, "degraded")
                    for chunk in chunks
                ]
            extraction.chunk_metadata = self._dedupe_anchors(metas)
            return extraction

        metas = [self._metadata_for_document_chunk(chunk, path, "none") for chunk in chunks]
        return AnchorExtraction(
            parser_backend="none",
            parser_status="ok",
            language=language,
            chunk_metadata=self._dedupe_anchors(metas),
        )

    def _tree_sitter_extract(self, content: str, language: str) -> AnchorExtraction:
        try:
            from tree_sitter_language_pack import get_parser
        except Exception as exc:  # noqa: BLE001
            return AnchorExtraction(
                parser_backend="tree_sitter",
                parser_status="parser_unavailable",
                language=language,
                tree_sitter_version=_package_version("tree-sitter"),
                reason=str(exc),
            )

        try:
            parser = get_parser(language)
        except Exception as exc:  # noqa: BLE001
            return AnchorExtraction(
                parser_backend="tree_sitter",
                parser_status="language_unsupported",
                language=language,
                tree_sitter_version=_package_version("tree-sitter"),
                tree_sitter_language_version=_package_version("tree-sitter-language-pack"),
                reason=str(exc),
            )

        try:
            tree = parser.parse(content.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            return AnchorExtraction(
                parser_backend="tree_sitter",
                parser_status="parse_failed",
                language=language,
                tree_sitter_version=_package_version("tree-sitter"),
                tree_sitter_language_version=_package_version("tree-sitter-language-pack"),
                reason=str(exc),
            )

        root = tree.root_node
        total_nodes, error_nodes = _count_nodes(root)
        ratio = (error_nodes / total_nodes) if total_nodes else 0.0
        status = "ok" if ratio <= self.parse_error_ratio_threshold else "parse_error_high"
        symbols = []
        if status == "ok":
            symbols = self._collect_symbols(root, content.encode("utf-8"))

        return AnchorExtraction(
            parser_backend="tree_sitter",
            parser_status=status,
            language=language,
            tree_sitter_version=_package_version("tree-sitter"),
            tree_sitter_language_version=_package_version("tree-sitter-language-pack"),
            parse_error_ratio=ratio,
            reason=None if status == "ok" else f"parse_error_ratio={ratio:.3f}",
            chunk_metadata=symbols,
        )

    def _collect_symbols(self, root: Any, source_bytes: bytes) -> list[dict[str, Any]]:
        symbols: list[dict[str, Any]] = []

        def walk(node: Any, parents: list[dict[str, Any]]) -> None:
            info = _symbol_info(node, source_bytes)
            next_parents = parents
            if info:
                parent_names = [p["name"] for p in parents if p.get("name")]
                qualified = ".".join([*parent_names, info["name"]]) if info["name"] else ""
                symbol = {
                    "symbol_kind": info["kind"],
                    "symbol_name": info["name"],
                    "qualified_name": qualified,
                    "parent_symbol": ".".join(parent_names) or None,
                    "start_line": int(node.start_point[0]) + 1,
                    "end_line": int(node.end_point[0]) + 1,
                }
                symbols.append(symbol)
                next_parents = [*parents, symbol]
            for child in getattr(node, "children", []) or []:
                walk(child, next_parents)

        walk(root, [])
        return symbols

    def _metadata_for_code_chunk(
        self, chunk: dict[str, Any], symbols: list[dict[str, Any]], path: str
    ) -> dict[str, Any]:
        start = int(chunk.get("start_line") or 0)
        end = int(chunk.get("end_line") or start)
        best = _best_symbol_for_range(symbols, start, end)
        if best:
            base = "|".join(
                [
                    str(Path(path).as_posix()),
                    str(best.get("symbol_kind") or ""),
                    str(best.get("qualified_name") or ""),
                    str(best.get("parent_symbol") or ""),
                ]
            )
            anchor_id = _short_hash(base)
            return {
                **best,
                "anchor_id": anchor_id,
                "parser_backend": "tree_sitter",
                "match_basis": "symbol",
            }
        return self._metadata_for_document_chunk(chunk, path, "tree_sitter")

    def _metadata_for_document_chunk(
        self, chunk: dict[str, Any], path: str, backend: str
    ) -> dict[str, Any]:
        start = int(chunk.get("start_line") or 0)
        title = str(chunk.get("section_title") or chunk.get("symbol_name") or "")
        text_hash = content_hash(str(chunk.get("content") or ""))
        base = f"{Path(path).as_posix()}|{title}|{start}|{text_hash[:12]}"
        return {
            "anchor_id": _short_hash(base),
            "parser_backend": backend,
            "symbol_kind": "section" if title else "chunk",
            "symbol_name": title or None,
            "qualified_name": title or None,
            "parent_symbol": None,
            "match_basis": "line_hash",
        }

    def _dedupe_anchors(self, metas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: dict[str, int] = {}
        out: list[dict[str, Any]] = []
        for meta in metas:
            anchor = str(meta.get("anchor_id") or "")
            count = seen.get(anchor, 0)
            seen[anchor] = count + 1
            if count:
                meta = dict(meta)
                meta["anchor_id"] = f"{anchor}:occurrence_{count + 1}"
                meta["occurrence_index"] = count + 1
            out.append(meta)
        return out


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def _language_for_path(path: str) -> str | None:
    return _CODE_EXTENSIONS.get(Path(path).suffix.lower())


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _count_nodes(root: Any) -> tuple[int, int]:
    total = 0
    errors = 0
    stack = [root]
    while stack:
        node = stack.pop()
        total += 1
        if getattr(node, "is_error", False) or getattr(node, "type", "") == "ERROR":
            errors += 1
        stack.extend(getattr(node, "children", []) or [])
    return total, errors


def _symbol_info(node: Any, source_bytes: bytes) -> dict[str, str] | None:
    kind_name = _SYMBOL_NODE_TYPES.get(getattr(node, "type", ""))
    if not kind_name:
        return None
    kind, field_name = kind_name
    name = ""
    if field_name:
        name_node = node.child_by_field_name(field_name)
        if name_node is not None:
            name = source_bytes[name_node.start_byte:name_node.end_byte].decode(
                "utf-8", errors="replace"
            )
    if not name:
        text = source_bytes[node.start_byte:node.end_byte].decode(
            "utf-8", errors="replace"
        )
        name = _guess_symbol_name(text)
    if not name:
        return None
    return {"kind": kind, "name": name}


def _guess_symbol_name(text: str) -> str:
    patterns = [
        r"\bfunction\s+([A-Za-z_$][\w$]*)",
        r"\bclass\s+([A-Za-z_$][\w$]*)",
        r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)",
        r"\bdef\s+([A-Za-z_]\w*)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return ""


def _best_symbol_for_range(
    symbols: list[dict[str, Any]], start: int, end: int
) -> dict[str, Any] | None:
    containing = [
        s
        for s in symbols
        if int(s.get("start_line") or 0) <= start
        and int(s.get("end_line") or 0) >= end
    ]
    if containing:
        return min(
            containing,
            key=lambda s: int(s.get("end_line") or 0) - int(s.get("start_line") or 0),
        )
    overlapping = [
        s
        for s in symbols
        if not (int(s.get("end_line") or 0) < start or int(s.get("start_line") or 0) > end)
    ]
    if overlapping:
        return min(overlapping, key=lambda s: abs(int(s.get("start_line") or 0) - start))
    return None


def _normalize(text: str) -> str:
    return json.dumps(text.split(), ensure_ascii=True)
