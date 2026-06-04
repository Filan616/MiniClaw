"""Query routing for RAG (Phase 8 M3, user feedback 10).

Decides whether a user message should trigger context retrieval, memory
retrieval, both, or neither. Pure keyword-based v1 — no LLM call.

Future M3+: optional LLM fallback for ambiguous queries (mirrors Phase 7
``decide_auto_intent`` two-tier pattern).
"""

from __future__ import annotations

from typing import Literal

__all__ = [
    "decide_query_route",
    "QueryRoute",
]

QueryRoute = Literal["context", "memory", "both", "none"]


# Phrases that point to material the user just brought in (documents,
# code, logs). Bilingual.
_CONTEXT_PHRASES: tuple[str, ...] = (
    "this document",
    "this code",
    "this file",
    "this log",
    "this output",
    "in it",
    "above",
    "the snippet",
    "the traceback",
    "这个文档",
    "这份文档",
    "它里面",
    "这段代码",
    "这个文件",
    "上面那个",
    "上面那段",
    "这个 log",
    "这个日志",
    "这段日志",
    "这个 traceback",
    "这段输出",
    "上面提到",
    "刚才那个",
)

# Phrases that point to long-term project rules / preferences / decisions.
_MEMORY_PHRASES: tuple[str, ...] = (
    "we decided",
    "we agreed",
    "i prefer",
    "my preference",
    "long-term rule",
    "project rule",
    "previously",
    "earlier we",
    "back then",
    "remember when",
    "之前我们",
    "我们之前",
    "之前定的",
    "之前怎么定",
    "我的偏好",
    "项目长期",
    "项目规则",
    "长期原则",
    "之前为什么",
    "以前为什么",
    "曾经决定",
    "约定",
)

# Phrases that explicitly combine current material with prior context.
_COMBO_PHRASES: tuple[str, ...] = (
    "based on our",
    "using our",
    "according to our",
    "combine this with",
    "evaluate this against",
    "结合这个",
    "结合之前",
    "按照之前",
    "按照规则",
    "用我之前的",
    "根据我之前",
    "按之前的偏好",
)


def decide_query_route(user_text: str) -> QueryRoute:
    """Classify *user_text* into one of: context | memory | both | none.

    Detection is order-sensitive:
    1. Check combo phrases first (overrides context-only / memory-only)
    2. Check both context and memory phrases independently
    3. If both hit, return ``both``
    4. Otherwise return whichever hit, or ``none`` if neither.
    """
    if not user_text:
        return "none"
    text = user_text.lower()

    has_combo = any(phrase in text for phrase in _COMBO_PHRASES)
    has_context = any(phrase in text for phrase in _CONTEXT_PHRASES)
    has_memory = any(phrase in text for phrase in _MEMORY_PHRASES)

    if has_combo or (has_context and has_memory):
        return "both"
    if has_context:
        return "context"
    if has_memory:
        return "memory"
    return "none"
