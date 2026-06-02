"""Regex-based fact extraction from agent message history.

Used during history compaction to harvest a compact list of "key facts"
that should survive when older messages are truncated. The extractor is
intentionally cheap and deterministic: it runs a handful of regexes
against message text plus the structured ``tool_calls`` field on
assistant messages, then dedupes and trims the result.

The output feeds :class:`mini_claw.agent.task_state.TaskState.key_facts`.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Per-fact character cap. Long facts waste prompt budget and rarely add value.
_MAX_FACT_LEN = 200

# How many facts to keep in total. We bias toward the most recent messages
# because they are likeliest to reflect the current task focus.
_MAX_FACTS = 20

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

# 1) File paths the agent has touched. Captures the path token (group 1).
_RE_FILE_PATH = re.compile(
    r"(?:read|wrote|created|modified)\s+(\S+\.(?:py|js|ts|md|json|yml|yaml|txt))",
    re.IGNORECASE,
)

# 2) Error / denial markers. We keep the whole line as the fact so the
#    surrounding context (which file, which tool) survives.
_RE_ERROR = re.compile(r"\[ERROR\][^\n]+|\[denied\][^\n]+")

# 3) User decisions / requirements ("we should ...", "I need to ...").
_RE_DECISION = re.compile(
    r"(?:should|need to|must)\s+([^.\n]+)",
    re.IGNORECASE,
)

# 4) Constraints / negative requirements ("do not ...", "avoid ...").
_RE_CONSTRAINT = re.compile(
    r"(?:do not|don't|avoid)\s+([^.\n]+)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _coerce_text(content: Any) -> str:
    """Flatten a message ``content`` field into plain text.

    DeepSeek/OpenAI-style messages can carry either a string or a list of
    content parts (``{"type": "text", "text": ...}``). Tool results may
    also surface here. We concatenate any text-bearing pieces and drop the
    rest so regexes get a single string to scan.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                # Common shapes: {"type": "text", "text": "..."} or {"text": "..."}.
                text = part.get("text") or part.get("content")
                if isinstance(text, str):
                    chunks.append(text)
        return "\n".join(chunks)
    return str(content)


def _truncate(fact: str) -> str:
    """Trim a fact to ``_MAX_FACT_LEN`` characters, collapsing whitespace."""
    cleaned = " ".join(fact.split())
    if len(cleaned) > _MAX_FACT_LEN:
        cleaned = cleaned[: _MAX_FACT_LEN - 1].rstrip() + "…"
    return cleaned


def _iter_tool_call_facts(message: dict) -> Iterable[str]:
    """Yield one fact per tool call recorded on an assistant message."""
    if message.get("role") != "assistant":
        return
    tool_calls = message.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        return
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        # OpenAI-style: {"function": {"name": "...", "arguments": "..."}}
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = fn.get("name") or call.get("name") or ""
        if not name:
            continue
        args = fn.get("arguments") or call.get("arguments") or ""
        if isinstance(args, dict):
            # Render a short key=value preview without dumping huge blobs.
            preview = ", ".join(f"{k}={v}" for k, v in list(args.items())[:3])
        else:
            preview = str(args)
        if preview:
            yield f"called tool {name}({preview})"
        else:
            yield f"called tool {name}"


def _extract_from_text(text: str) -> list[str]:
    """Run the regex battery against a single text blob."""
    if not text:
        return []
    facts: list[str] = []

    for match in _RE_FILE_PATH.finditer(text):
        # match.group(0) keeps the verb ("read foo.py") which is more useful
        # than just the bare path.
        facts.append(f"file: {match.group(0).strip()}")

    for match in _RE_ERROR.finditer(text):
        facts.append(f"error: {match.group(0).strip()}")

    for match in _RE_DECISION.finditer(text):
        body = match.group(1).strip()
        if body:
            facts.append(f"decision: {body}")

    for match in _RE_CONSTRAINT.finditer(text):
        body = match.group(1).strip()
        if body:
            facts.append(f"constraint: {body}")

    return facts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_facts_from_messages(messages: list[dict]) -> list[str]:
    """Extract a deduplicated list of key facts from ``messages``.

    Walks the message list from newest to oldest so that, when we hit the
    ``_MAX_FACTS`` cap, the surviving facts reflect the most recent state
    of the conversation. Within each message, facts are recorded in their
    natural reading order. The final list is returned in chronological
    order (oldest fact first) for human readability.
    """
    if not messages:
        return []

    seen: set[str] = set()
    collected: list[str] = []  # newest-first while collecting

    for message in reversed(messages):
        if not isinstance(message, dict):
            continue

        per_message: list[str] = []

        text = _coerce_text(message.get("content"))
        per_message.extend(_extract_from_text(text))
        per_message.extend(_iter_tool_call_facts(message))

        for raw in per_message:
            fact = _truncate(raw)
            if not fact or fact in seen:
                continue
            seen.add(fact)
            collected.append(fact)
            if len(collected) >= _MAX_FACTS:
                break

        if len(collected) >= _MAX_FACTS:
            break

    # Flip back to chronological order so older context reads first.
    collected.reverse()
    return collected
