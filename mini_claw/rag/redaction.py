"""RAG-specific redaction (Phase 8 M2).

Layers on top of ``mini_claw.workflow.prompt_compiler.redact_prompt_text`` (5
secret patterns) and adds path relativization so absolute filesystem paths
inside indexed content do not leak the host directory layout.

Used by:
- ``RagIndexer`` before writing chunks to the FTS5 / chunk store.
- ``RagRetriever`` when assembling search results returned to the LLM.
"""

from __future__ import annotations

from mini_claw.workflow.prompt_compiler import (
    _ABSOLUTE_PATH_PATTERNS,
    SECRET_PATTERNS,
    redact_prompt_text,
)


def redact_for_rag(text: str) -> tuple[str, bool]:
    """Apply secret-pattern redaction and absolute-path relativization.

    Returns ``(redacted_text, was_redacted)``. The boolean is True if any
    pattern (secret OR path) matched, so callers can set ``rag_items.metadata``
    or audit ``redacted=True``.
    """
    text, secret_redacted = redact_prompt_text(text)
    path_redacted = False
    for pattern in _ABSOLUTE_PATH_PATTERNS:
        text, count = pattern.subn("<workspace>/...", text)
        if count > 0:
            path_redacted = True
    return text, secret_redacted or path_redacted


def count_secret_hits(text: str) -> int:
    """Count how many secret-pattern matches a piece of text contains.

    Drives :class:`mini_claw.rag.indexer.RagIndexer`'s sensitivity heuristic:
    files with several secret hits are marked ``sensitivity_level='high'``
    even if their path is not on the sensitive allowlist.
    """
    total = 0
    for pattern in SECRET_PATTERNS:
        total += len(pattern.findall(text))
    return total
