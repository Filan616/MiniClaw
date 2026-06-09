"""Phase 10 M10.1: ReActUserUpdate generation, sanitization, persistence.

This module replaces the standalone Phase 9.7 prelude pipeline. New runs
emit user-visible process messages exclusively as ``ReActUserUpdate``
records with ``message_kind='react_update'``. Legacy ``message_kind='prelude'``
rows are not migrated; the trace layer maps them as legacy
``action_planned`` updates.

Contracts (see plans/ReAct.md):

- P5  独立 prelude 机制不再新增。
- P7  action_planned fallback 不能调 LLM —— 规则模板兜底。
- P8  plugin/custom tool 走通用模板。
- P19 text_hash 指向最终发送文本的 hash —— raw candidate 永不落库。
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from typing import Any, Iterable

from mini_claw.agent.react_models import (
    ReActUserUpdate,
    UpdateEventType,
    VisibleLevel,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# action_planned templates
# ---------------------------------------------------------------------------

ACTION_PLANNED_TEMPLATES: dict[str, str] = {
    "read_file": "好的，我先读取这个文件并查看内容。",
    "write_file": "好的，我先准备写入这个文件；如果需要权限确认，我会继续提示你。",
    "list_directory": "好的，我先查看这个目录下的文件。",
    "run_shell": "好的，我先准备运行这个命令；如果需要审批，我会等待你的确认。",
    "search_context": "好的，我先在上下文索引里检索相关内容。",
    "index_context": "好的，我先为这个文件建立上下文索引。",
    "reindex_context": "好的，我先检查并更新这个上下文索引。",
    "search_memory": "好的，我先检索相关长期记忆。",
    "search_chat": "好的，我先搜索相关历史对话。",
    "open_app": "好的，我先尝试打开这个白名单应用。",
    "delete_file": "好的，我先准备删除这个文件；如果需要审批，我会等待你的确认。",
    "current_time": "好的，我先获取当前时间。",
}

# Read-only / introspection tools that are safe to describe with a single
# parallel-friendly summary.
READONLY_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "list_directory",
        "search_context",
        "search_memory",
        "search_chat",
        "current_time",
    }
)

GENERIC_ACTION_PLANNED = "好的，我先处理这个操作。"
GENERIC_PARALLEL_READONLY = "好的，我先并行查看相关信息。"
GENERIC_MIXED_SEQUENTIAL = (
    "好的，我先按顺序处理这些操作；涉及高风险步骤时会继续提示你确认。"
)


# ---------------------------------------------------------------------------
# Sanitize / completion-claim guard
# ---------------------------------------------------------------------------

# Mirror of legacy _sanitize_prelude completion phrases — kept in sync
# so the migrated pipeline rejects the same strings.
_COMPLETION_PHRASES: tuple[str, ...] = (
    "已完成",
    "已创建",
    "已修改",
    "已删除",
    "已写入",
    "已读取",
    "已运行",
    "已执行",
    "已索引",
    "测试通过",
    "已找到",
    "结果是",
    "已生成",
    "成功创建",
    "成功修改",
    "成功删除",
    "completed",
    "created",
    "modified",
    "deleted",
    "written",
    "test passed",
    "tests passed",
    "found the",
    "result is",
    "successfully created",
    "successfully modified",
)


def _strip_code_blocks(text: str) -> str:
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"`[^`]+`", "", text)
    return text


def _normalize_whitespace(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def contains_completion_claim(text: str) -> bool:
    if not text:
        return False
    lower = text.lower()
    return any(phrase in lower for phrase in _COMPLETION_PHRASES)


def sanitize_react_update_text(
    text: str,
    *,
    max_chars: int = 160,
    event_type: UpdateEventType = "action_planned",
) -> str | None:
    """Return sanitized text or ``None`` if the candidate must be dropped."""
    if not text:
        return None
    cleaned = _strip_code_blocks(text)
    cleaned = _normalize_whitespace(cleaned)
    if not cleaned:
        return None
    if event_type == "action_planned" and contains_completion_claim(cleaned):
        return None
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + "..."
    if len(cleaned.strip()) < 2:
        return None
    return cleaned


def redact_sensitive_text(text: str) -> str:
    """Lightweight redaction for user-visible update text.

    The full security redaction pipeline already runs at the audit layer.
    Here we only mask trivially-sensitive substrings so the *displayed*
    text never quotes obvious secrets verbatim.
    """
    if not text:
        return text
    # Common secret-looking tokens (long hex/base64-ish strings).
    text = re.sub(r"\b[A-Fa-f0-9]{32,}\b", "***", text)
    text = re.sub(r"\b(?:sk|pk|tok|key|secret)[-_=:][A-Za-z0-9_\-]{16,}\b", "***", text, flags=re.IGNORECASE)
    return text


def hash_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:32]


def prepare_react_update_text(
    candidate_text: str,
    *,
    max_chars: int,
    event_type: UpdateEventType,
) -> tuple[str, str] | None:
    """Pipeline: sanitize → redact → hash. Returns ``(final_text, text_hash)`` or None."""
    sanitized = sanitize_react_update_text(
        candidate_text, max_chars=max_chars, event_type=event_type
    )
    if sanitized is None:
        return None
    final_text = redact_sensitive_text(sanitized)
    return final_text, hash_text(final_text)


# ---------------------------------------------------------------------------
# action_planned generation (rule-based, no LLM)
# ---------------------------------------------------------------------------


def _is_readonly_tool(name: str) -> bool:
    return name in READONLY_TOOLS


def generate_action_planned_from_tools(tool_names: Iterable[str]) -> str:
    """Deterministic action_planned text derived from a list of tool names.

    Never raises. plugin/custom tools without a template fall through to
    the generic line.
    """
    names = [n for n in tool_names if n]
    if not names:
        return ""
    if len(names) == 1:
        return ACTION_PLANNED_TEMPLATES.get(names[0], GENERIC_ACTION_PLANNED)
    if all(_is_readonly_tool(n) for n in names):
        return GENERIC_PARALLEL_READONLY
    return GENERIC_MIXED_SEQUENTIAL


# ---------------------------------------------------------------------------
# MODE policy
# ---------------------------------------------------------------------------

_MODE_EVENT_POLICY: dict[str, dict[str, Any]] = {
    "silent": {"events": set(), "important_decision_summary": False},
    "normal": {"events": {"action_planned"}, "important_decision_summary": True},
    "verbose": {
        "events": {"action_planned", "observation_summary"},
        "important_decision_summary": True,
    },
    "debug": {
        "events": {
            "action_planned",
            "observation_summary",
            "reflection_summary",
            "decision_summary",
        },
        "important_decision_summary": True,
    },
}


def should_send_update(update: ReActUserUpdate, mode: str) -> bool:
    policy = _MODE_EVENT_POLICY.get(mode, _MODE_EVENT_POLICY["normal"])
    if update.event_type in policy["events"]:
        return True
    if (
        update.event_type == "decision_summary"
        and update.is_important
        and policy["important_decision_summary"]
    ):
        return True
    return False


# ---------------------------------------------------------------------------
# Construction & persistence
# ---------------------------------------------------------------------------


def make_update(
    *,
    step_id: str,
    run_id: str,
    chat_id: str,
    agent_id: str,
    event_type: UpdateEventType,
    final_text: str,
    text_hash: str,
    visible_level: VisibleLevel = "normal",
    is_important: bool = False,
) -> ReActUserUpdate:
    return ReActUserUpdate(
        update_id=f"upd-{uuid.uuid4().hex[:12]}",
        step_id=step_id,
        run_id=run_id,
        chat_id=chat_id,
        agent_id=agent_id,
        event_type=event_type,
        text=final_text,
        text_hash=text_hash,
        visible_level=visible_level,
        is_important=is_important,
        send_status="pending",
        created_at=int(time.time()),
    )


def store_react_update(storage: Any, update: ReActUserUpdate, *, store_redacted_text: bool = True) -> None:
    """Persist an update row to ``react_user_updates``.

    ``raw candidate text`` never reaches storage: only the final
    redacted text — and only if ``store_redacted_text`` is enabled.
    """
    if storage is None:
        return
    redacted = update.text if store_redacted_text else None
    try:
        storage.execute(
            "INSERT OR REPLACE INTO react_user_updates "
            "(update_id, step_id, run_id, chat_id, agent_id, event_type, "
            " visible_level, is_important, text_hash, redacted_text, "
            " send_status, channel_message_id, error, created_at, sent_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                update.update_id,
                update.step_id,
                update.run_id,
                update.chat_id,
                update.agent_id,
                update.event_type,
                update.visible_level,
                1 if update.is_important else 0,
                update.text_hash,
                redacted,
                update.send_status,
                update.channel_message_id,
                update.error,
                update.created_at or int(time.time()),
                update.sent_at,
            ),
        )
    except Exception:
        logger.warning("store_react_update failed", exc_info=True)
