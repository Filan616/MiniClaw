"""Agent loop: core execution engine driving LLM conversations with tool calling."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from mini_claw.agent.context import AgentContext
from mini_claw.providers.base import Provider
from mini_claw.tools.registry import ToolContext, ToolRegistry


MAX_ITERATIONS = 50
# Send progress updates to user after N iterations (0 = disabled)
PROGRESS_NOTIFY_INTERVAL = 3
logger = logging.getLogger(__name__)


async def _send_progress_update(
    run: AgentRun,
    ctx: AgentContext,
    iteration: int,
    last_tool: str | None = None,
) -> None:
    """Send a progress update to the user showing what the agent is doing."""
    if not ctx.on_progress:
        return

    progress_msg = f"🔄 正在处理中（第 {iteration} 轮）"
    if last_tool:
        progress_msg += f" - 上次调用: {last_tool}"

    try:
        await ctx.on_progress(progress_msg)
    except Exception:
        logger.warning("Progress update failed", exc_info=True)


def _detect_tool_call_loop(run: AgentRun, lookback: int = 5) -> tuple[bool, str | None]:
    """Detect if the agent is stuck in a tool call loop.

    Returns:
        (is_looping, tool_name): True if same tool called repeatedly without success.
    """
    if len(run.tool_call_history) < lookback:
        return False, None

    # Check last N calls
    recent = run.tool_call_history[-lookback:]
    tool_names = [name for name, success in recent]

    # If same tool called 3+ times in last 5 calls, and majority failed
    from collections import Counter
    tool_counts = Counter(tool_names)
    most_common_tool, count = tool_counts.most_common(1)[0]

    if count >= 3:
        # Check success rate for this tool in recent calls
        tool_results = [success for name, success in recent if name == most_common_tool]
        success_rate = sum(tool_results) / len(tool_results) if tool_results else 0

        # Loop detected if tool called 3+ times with <50% success rate
        if success_rate < 0.5:
            return True, most_common_tool

    return False, None


class RunOutcome:
    """Constants for agent run outcomes."""

    DONE = "done"
    SUSPENDED = "suspended"
    ABORTED = "aborted"


@dataclass(slots=True)
class AgentRun:
    """Represents the mutable state of a single agent run."""

    id: str
    chat_id: str
    agent_id: str
    status: str  # RunOutcome constant
    messages: list[dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    seen_calls: set[str] = field(default_factory=set)
    pending_approval_id: Optional[str] = None
    pending_tool_call: Optional[str] = None
    final_answer: Optional[str] = None
    allowed_tools: list[str] = field(default_factory=list)
    dangerous_actions: dict[str, Any] = field(default_factory=dict)
    written_scripts: dict[str, str] = field(default_factory=dict)
    rag_injected: bool = False  # Phase 8 M3: prevents double-injection across iterations
    prelude_sent: bool = False  # Phase 9.7: track if prelude already sent
    tool_call_history: list[tuple[str, bool]] = field(default_factory=list)  # Phase 9.8: (tool_name, success)
    # Phase 10 M10.0: original user goal preserved for Goal Anchor injection.
    original_goal_raw: str | None = None
    original_goal_summary: str | None = None
    # Phase 10 M10.1: ReActStep tracking. ``step_counter`` only increases.
    step_counter: int = 0
    react_steps: list[Any] = field(default_factory=list)  # ReActStep instances
    react_action_planned_sent_steps: set[str] = field(default_factory=set)
    repeated_tool_call_detected: bool = False
    hallucination_guard_triggered: bool = False
    final_reflection_json: Optional[str] = None


def _call_signature(call_name: str, call_args: dict[str, Any]) -> str:
    """Compute an MD5 signature for duplicate call detection."""
    raw = json.dumps({"name": call_name, "args": call_args}, sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()


def _build_tool_context(ctx: AgentContext) -> ToolContext:
    """Create a ToolContext from an AgentContext.

    Phase 9 M9.5: Added session_id and channel_name for scope filtering.
    Phase 9 M9.1: Added chat_search_manager for search_chat tool.
    """
    return ToolContext(
        workspace_dir=ctx.workspace_dir,
        chat_id=ctx.chat_id,
        agent_id=ctx.agent_id,
        timeout=ctx.timeout,
        sandbox_mode=ctx.sandbox_mode,
        audit_logger=ctx.audit_logger,
        chain_detector=getattr(ctx, "chain_detector", None),
        rag_manager=getattr(ctx, "rag_manager", None),
        session_id=getattr(ctx, "session_id", None),
        channel_name=getattr(ctx, "channel_name", None),
        chat_search_manager=getattr(ctx, "chat_search_manager", None),
    )


def _ctx_to_dict(ctx: AgentContext) -> dict[str, Any]:
    """Adapter: convert AgentContext to the dict shape PermissionGate expects."""
    return {
        "chat_id": ctx.chat_id,
        "agent_id": ctx.agent_id,
        "workspace_dir": ctx.workspace_dir,
        "level": getattr(ctx, "level", "L2"),
        "sandbox_mode": ctx.sandbox_mode,
    }


def _sanitize_prelude(
    text: str,
    max_length: int = 120,
    audit_callback: Callable[[str, dict], None] | None = None,
) -> str | None:
    """Sanitize prelude content before sending to user.

    Returns None if content should not be sent as prelude.

    Args:
        text: Raw prelude text from LLM
        max_length: Maximum length before truncation
        audit_callback: Optional callback(event_type, details) for rejected content
    """
    original_hash = hashlib.sha256(text.encode()).hexdigest()[:16]

    text = text.strip()
    if not text:
        return None

    # Max length
    if len(text) > max_length:
        text = text[:max_length] + "..."

    # Remove code blocks (```)
    text = re.sub(r"```[\s\S]*?```", "", text).strip()
    if not text:
        if audit_callback:
            audit_callback(
                "prelude_sanitized_rejected",
                {"reason": "code_only", "original_hash": original_hash},
            )
        return None

    # Remove inline code (`)
    text = re.sub(r"`[^`]+`", "", text).strip()

    # Reject completion claims (prelude is BEFORE execution)
    completion_phrases = [
        # Chinese
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
        # English
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
    ]
    lower = text.lower()
    if any(phrase in lower for phrase in completion_phrases):
        if audit_callback:
            detected = next((p for p in completion_phrases if p in lower), "unknown")
            audit_callback(
                "prelude_sanitized_rejected",
                {
                    "reason": "completion_claim",
                    "original_hash": original_hash,
                    "detected_phrase": detected,
                },
            )
        return None

    # Remove excessive newlines
    text = re.sub(r"\n{3,}", "\n\n", text)

    result = text.strip()
    if len(result) < 2:
        if audit_callback:
            audit_callback(
                "prelude_sanitized_rejected",
                {"reason": "too_short", "original_hash": original_hash},
            )
        return None

    return result


def _messages_for_provider(run: AgentRun, ctx: AgentContext) -> list[dict[str, Any]]:
    """Build provider messages with base and skill system prompt context."""
    prompt_parts: list[str] = []
    if ctx.system_prompt:
        prompt_parts.append(ctx.system_prompt.strip())
    prompt_parts.append(_current_time_prompt())

    if ctx.skill_manager is not None:
        fragment = ctx.skill_manager.compose_prompt_fragment(
            ctx.agent_id,
            run.allowed_tools,
        )
        if fragment:
            prompt_parts.append(fragment)

    # Phase 8 M3 + Phase 9 M9.5: opportunistic four-channel RAG injection.
    # - Only fires when relevant managers are present AND ``rag.retrieval.auto_*`` is on.
    # - Only fires once per run (``run.rag_injected`` guard).
    # - Defaults are False, so existing tests/behavior are unaffected.
    # - Four independent channels:
    #   1. auto_context_retrieval        -> [Retrieved Context]
    #   2. auto_user_memory_retrieval    -> [Retrieved User Memory] (legacy: auto_memory_retrieval)
    #   3. auto_workspace_memory_retrieval -> [Retrieved Workspace Memory]
    #   4. auto_chat_retrieval           -> [Retrieved Chat History]
    base_messages = run.messages
    rag_blocks: list[str] = []
    rag_mgr = getattr(ctx, "rag_manager", None)
    chat_search_mgr = getattr(ctx, "chat_search_manager", None)

    if (rag_mgr is not None or chat_search_mgr is not None) and not run.rag_injected:
        try:
            user_text = _last_user_text(run.messages)
            if user_text:
                from mini_claw.rag.injector import (
                    build_chat_history_block,
                    build_context_block,
                    build_memory_block,
                    build_workspace_memory_block,
                )
                from mini_claw.rag.query_router import decide_query_route

                route = decide_query_route(user_text)

                # === Channel 1: Context retrieval ===
                if rag_mgr is not None:
                    cfg = rag_mgr.config
                    want_context = (
                        cfg.retrieval.auto_context_retrieval
                        and cfg.namespaces.context_enabled
                        and route in ("context", "both")
                    )
                    if want_context:
                        chunks, _err = rag_mgr.search_context(
                            user_text,
                            ctx={
                                "agent_id": ctx.agent_id,
                                "workspace_dir": ctx.workspace_dir,
                                "session_id": getattr(ctx, "session_id", None),
                                "chat_id": ctx.chat_id,
                                "channel_name": getattr(ctx, "channel_name", None),
                            },
                        )
                        if chunks:
                            rag_blocks.append(build_context_block(chunks))

                # === Channel 2: User memory retrieval ===
                if rag_mgr is not None:
                    cfg = rag_mgr.config
                    # Honor both new and legacy config flags
                    auto_user_mem = (
                        cfg.retrieval.auto_user_memory_retrieval
                        or cfg.retrieval.auto_memory_retrieval  # legacy alias
                    )
                    want_user_memory = (
                        auto_user_mem
                        and cfg.namespaces.memory_enabled
                        and route in ("memory", "both")
                    )
                    if want_user_memory and hasattr(rag_mgr, "search_memory"):
                        memories, _err = rag_mgr.search_memory(
                            user_text,
                            ctx={
                                "agent_id": ctx.agent_id,
                                "session_id": getattr(ctx, "session_id", None),
                                "channel_name": getattr(ctx, "channel_name", None),
                            },
                            scope="agent",
                        )
                        if memories:
                            rag_blocks.append(build_memory_block(memories))

                # === Channel 3: Workspace memory retrieval ===
                if rag_mgr is not None and ctx.workspace_dir is not None:
                    cfg = rag_mgr.config
                    want_workspace_memory = (
                        cfg.retrieval.auto_workspace_memory_retrieval
                        and cfg.namespaces.memory_enabled
                        and route in ("memory", "both")
                    )
                    if want_workspace_memory and hasattr(rag_mgr, "search_memory"):
                        ws_memories, _err = rag_mgr.search_memory(
                            user_text,
                            ctx={
                                "agent_id": ctx.agent_id,
                                "workspace_dir": ctx.workspace_dir,
                                "session_id": getattr(ctx, "session_id", None),
                                "channel_name": getattr(ctx, "channel_name", None),
                            },
                            scope="workspace",
                        )
                        if ws_memories:
                            rag_blocks.append(build_workspace_memory_block(ws_memories))

                # === Channel 4: Chat history retrieval ===
                # Works independently with chat_search_mgr; rag_mgr only needed for config flag
                if chat_search_mgr is not None:
                    # Check auto_chat_retrieval flag from rag_mgr if available
                    want_chat = False
                    chat_top_k = 5  # default
                    if rag_mgr is not None:
                        cfg = rag_mgr.config
                        want_chat = cfg.retrieval.auto_chat_retrieval
                        chat_top_k = cfg.retrieval.chat_top_k

                    if want_chat:
                        try:
                            # Build a SimpleNamespace-like ctx for ChatSearchRetriever
                            from types import SimpleNamespace
                            chat_ctx = SimpleNamespace(
                                agent_id=ctx.agent_id,
                                chat_id=ctx.chat_id,
                                workspace_dir=ctx.workspace_dir,
                                session_id=getattr(ctx, "session_id", None),
                                channel_name=getattr(ctx, "channel_name", None),
                            )
                            chat_results = chat_search_mgr.search(
                                user_text,
                                scope="current_session",
                                ctx=chat_ctx,
                                top_k=chat_top_k,
                            )
                            if chat_results:
                                rag_blocks.append(build_chat_history_block(chat_results))
                        except (ValueError, AttributeError):
                            # Fail-closed scope or missing ctx fields: skip silently
                            pass

                run.rag_injected = True
        except Exception:
            # Never let auto-retrieval break the loop.
            run.rag_injected = True

    if not prompt_parts and not rag_blocks:
        return base_messages

    # Phase 10 M10.0: Goal Anchor injection (per-iteration, no LLM cost).
    goal_anchor_text = _build_goal_anchor_text(run, ctx)
    if goal_anchor_text:
        prompt_parts.append(goal_anchor_text)

    # Compose system prompt: agent system prompt + skills first, then RAG blocks.
    parts = list(prompt_parts) + rag_blocks
    system_message = {"role": "system", "content": "\n\n".join(parts)}
    if base_messages and base_messages[0].get("role") == "system":
        final_messages = [system_message, *base_messages[1:]]
    else:
        final_messages = [system_message, *base_messages]

    # Tool routing one-shot: inject a synthetic assistant+tool example when the
    # user's goal matches "打开X应用" and the allowed tools include open_app.
    # This forces the model to see a successful open_app call pattern rather
    # than defaulting to run_shell for discovery.
    final_messages = _inject_open_app_one_shot(final_messages, run, ctx)

    return final_messages


# ---------------------------------------------------------------------------
# Tool-routing one-shot injection
# ---------------------------------------------------------------------------

_OPEN_APP_KEYWORDS = ("打开", "启动", "运行", "开启", "open ", "launch ", "start ")
_OPEN_APP_ONESHOT_USER = "帮我打开微信"
_OPEN_APP_ONESHOT_ASSISTANT = (
    '好的，我直接帮你打开微信。\n\n'
    '[调用 open_app(app="微信")]'
)
_OPEN_APP_ONESHOT_TOOL_RESULT = (
    "Opened app: wechat\n"
    "Path: C:\\Program Files\\Tencent\\WeChat\\WeChat.exe\n"
    "Source: registry"
)


def _inject_open_app_one_shot(
    messages: list[dict], run: AgentRun, ctx: AgentContext
) -> list[dict]:
    """Inject a one-shot open_app example when the user goal looks like '打开X应用'.

    This teaches the model to call open_app directly instead of
    falling back to run_shell for path discovery. The one-shot is only
    injected on the FIRST iteration (to avoid polluting later rounds) and
    only when the allowed tools include open_app.
    """
    if run.iterations > 1:
        return messages
    allowed = run.allowed_tools or []
    if allowed and "open_app" not in allowed:
        return messages
    goal = run.original_goal_raw or ""
    if not goal:
        goal = _last_user_text(run.messages)
    if not any(kw in goal.lower() for kw in _OPEN_APP_KEYWORDS):
        return messages

    # Insert the one-shot pair right before the final user message so the
    # model sees: system → ... → [one-shot user] → [one-shot assistant] → real user
    one_shot = [
        {"role": "user", "content": _OPEN_APP_ONESHOT_USER},
        {"role": "assistant", "content": _OPEN_APP_ONESHOT_ASSISTANT},
        {"role": "user", "content": "(open_app 结果: " + _OPEN_APP_ONESHOT_TOOL_RESULT + ")"},
        {"role": "assistant", "content": "微信已成功打开！"},
    ]
    # Find last user message index
    last_user_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            last_user_idx = i
            break
    if last_user_idx is None:
        return messages
    return messages[:last_user_idx] + one_shot + messages[last_user_idx:]


def _build_goal_anchor_text(run: AgentRun, ctx: AgentContext) -> str:
    """Phase 10 M10.0: build the per-iteration Goal Anchor system text.

    Returns "" when goal anchoring is disabled or no original goal was
    captured. Best-effort audit emission — failures never break the loop.

    Phase 10 §6 knobs consumed here:
    - ``goal_anchor_enabled``: master switch.
    - ``goal_anchor_inject_every_iteration``: when False, only the first
      iteration of a run injects the anchor; later iterations skip it.
    - ``goal_anchor_summarization_mode``: only ``"truncate"`` is
      supported today; any other value falls back to truncate and emits
      a one-time warning.
    - ``goal_anchor_max_summary_chars`` / ``mark_untrusted`` /
      ``detect_policy``: fed into the goal_anchor builder.
    """
    if not getattr(ctx, "goal_anchor_enabled", True):
        return ""
    inject_every = getattr(ctx, "goal_anchor_inject_every_iteration", True)
    if not inject_every and run.iterations > 1:
        return ""
    mode = getattr(ctx, "goal_anchor_summarization_mode", "truncate")
    if mode != "truncate":
        logger.warning(
            "Unsupported goal_anchor.summarization_mode=%r, falling back to truncate",
            mode,
        )
    raw = run.original_goal_raw or ""
    if not raw:
        # Try to lift from the most recent user message if not already set.
        raw = _last_user_text(run.messages)
        if not raw:
            return ""
    try:
        from mini_claw.agent.goal_anchor import build_goal_anchor

        anchor = build_goal_anchor(
            raw,
            iteration=max(1, run.iterations),
            max_iterations=MAX_ITERATIONS,
            max_summary_chars=getattr(ctx, "goal_anchor_max_summary_chars", 800),
            detect_policy=getattr(ctx, "goal_anchor_detect_policy", True),
            mark_untrusted=getattr(ctx, "goal_anchor_mark_untrusted", True),
        )
    except Exception:
        logger.warning("goal anchor build failed", exc_info=True)
        return ""

    # Cache summary on first build so future iterations don't recompute.
    if not run.original_goal_summary:
        run.original_goal_summary = anchor.summary

    if ctx.audit_logger:
        try:
            ctx.audit_logger.log_security_event(
                event_type="goal_anchor_injected",
                details={
                    "iteration": run.iterations,
                    "summary_len": len(anchor.summary),
                    "truncated": anchor.truncated,
                    "policy_hits": anchor.policy_hits[:5],
                    "summarization_mode": mode,
                    "inject_every_iteration": inject_every,
                },
                chat_id=ctx.chat_id,
                agent_id=ctx.agent_id,
            )
            if anchor.policy_hits:
                ctx.audit_logger.log_security_event(
                    event_type="goal_anchor_policy_warning",
                    details={"policy_hits": anchor.policy_hits[:5]},
                    chat_id=ctx.chat_id,
                    agent_id=ctx.agent_id,
                )
        except Exception:
            pass
    return anchor.text


def _current_time_prompt() -> str:
    """Short clock context injected into each provider request."""
    now = datetime.now().astimezone()
    tz_name = now.tzname() or "local"
    offset = now.strftime("%z")
    if len(offset) == 5:
        offset = f"{offset[:3]}:{offset[3:]}"
    return (
        "[Current Time]\n"
        f"当前系统时间：{now.strftime('%Y-%m-%d %H:%M:%S')} {tz_name} ({offset})。\n"
        "当用户询问今天、昨天、明天、日期、时间、日报、周报或日程时，以此为准；"
        "如需刷新精确时间，可调用 current_time 工具。"
    )


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    """Return the most recent user message content (or empty)."""
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content", "")
            return content if isinstance(content, str) else ""
    return ""


async def _process_single_tool_call(
    tc: Any,
    run: AgentRun,
    registry: ToolRegistry,
    permission_gate: Any,
    result_processor: Any,
    tool_ctx: ToolContext,
    ctx: AgentContext,
) -> dict[str, Any]:
    """Process a single tool call and return the result message."""
    sig = _call_signature(tc.name, tc.arguments)

    # Duplicate detection
    if sig in run.seen_calls:
        return {
            "role": "tool",
            "tool_call_id": tc.id,
            "content": "[duplicate call skipped]",
        }
    run.seen_calls.add(sig)

    # Permission check
    tool = registry.get(tc.name)
    if tool is None:
        return {
            "role": "tool",
            "tool_call_id": tc.id,
            "content": f"[error] unknown tool: {tc.name}",
        }

    decision = permission_gate.evaluate(
        tool=tc.name, args=tc.arguments, ctx=_ctx_to_dict(ctx)
    )

    # Handle audit event if present
    if decision.audit_event:
        debug_id = ctx.audit_logger.log_security_event(
            event_type=decision.audit_event["event_type"],
            details=decision.audit_event,
            chat_id=ctx.chat_id,
            agent_id=ctx.agent_id,
        )
        decision = decision.__class__(
            action=decision.action,
            reason=decision.reason.replace("{debug_id}", debug_id),
            internal_reason=decision.internal_reason,
            audit_event=decision.audit_event,
        )

    if decision.action == "deny":
        return {
            "role": "tool",
            "tool_call_id": tc.id,
            "content": f"[denied] {decision.reason}",
        }

    if decision.action == "need_approval":
        # Cannot suspend during parallel processing, treat as deny
        return {
            "role": "tool",
            "tool_call_id": tc.id,
            "content": "[error] approval required but not supported in parallel mode",
        }

    # decision.action == "allow"
    # Chain attack detection (pre-tool)
    if hasattr(ctx, "chain_detector") and ctx.chain_detector:
        blocked = ctx.chain_detector.evaluate_before_tool(tc, run, ctx)
        if blocked:
            chain_action = blocked.get("action", "deny")
            audit_event = blocked.get("audit_event") or {}
            event_type = audit_event.get("event_type", "chain_attack_blocked")
            debug_id = ""
            if ctx.audit_logger:
                debug_id = ctx.audit_logger.log_security_event(
                    event_type=event_type,
                    details=audit_event,
                    chat_id=ctx.chat_id,
                    agent_id=ctx.agent_id,
                )
            # Phase 9 横切: warn = audit only, do NOT block tool execution
            if chain_action == "warn":
                # Fall through to normal execution; auditing has occurred.
                pass
            else:
                return {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": f"[denied] Chain attack detected. debug_id={debug_id}",
                }

    # Phase B.4: Record tool call duration
    start_time = time.monotonic()
    tool_status = "ok"
    try:
        result = await tool.handler(**tc.arguments, ctx=tool_ctx)
    except TypeError as exc:
        result = f"[error] tool {tc.name} rejected arguments: {exc}"
        tool_status = "error"
    except Exception as exc:
        tool_status = "error"
        if result_processor:
            result = result_processor.process_error(exc)
        else:
            result = f"[error] {type(exc).__name__}: {exc}"
    else:
        if result_processor:
            result = result_processor.process(result, tc.name)
    duration_ms = int((time.monotonic() - start_time) * 1000)

    # Persist tool_call record (Phase B.4)
    _persist_tool_call(ctx, run.id, tc, result, tool_status, duration_ms)

    # Chain attack detection (post-tool observation)
    if hasattr(ctx, "chain_detector") and ctx.chain_detector:
        ctx.chain_detector.observe_after_tool(tc, run, result, success=True, ctx=ctx)

    return {
        "role": "tool",
        "tool_call_id": tc.id,
        "content": result,
    }


def _persist_tool_call(
    ctx: AgentContext, run_id: str, tc: Any, result: str, status: str, duration_ms: int
) -> None:
    """Persist a tool_call record with duration_ms (Phase B.4)."""
    storage = getattr(ctx, "storage", None)
    if storage is None:
        return
    try:
        now = int(time.time())
        tc_id = getattr(tc, "id", None) or str(uuid.uuid4())
        storage.execute(
            "INSERT OR REPLACE INTO tool_calls "
            "(id, run_id, tool_name, arguments, result, status, "
            " created_at, finished_at, duration_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tc_id,
                run_id,
                tc.name,
                json.dumps(tc.arguments) if tc.arguments else None,
                result[:5000] if isinstance(result, str) else str(result)[:5000],
                status,
                now,
                now,
                duration_ms,
            ),
        )
    except Exception:
        # Stats persistence is best-effort; failure should not break the run
        pass


# ---------------------------------------------------------------------------
# Phase 10 M10.1: ReActStep persistence + ReActUserUpdate dispatch
# ---------------------------------------------------------------------------


def _new_step_id(run_id: str, iteration: int) -> str:
    return f"rs-{run_id[:8]}-{iteration:03d}-{uuid.uuid4().hex[:6]}"


def _truncate_for_reflection(text: str, max_chars: int) -> str:
    """Phase 10 §6: cap reflection-prompt input length per ``max_reflection_chars``."""
    if not text:
        return ""
    if max_chars and max_chars > 0 and len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text


async def _run_finalizer(
    *,
    policy: Any,
    decision: Any,
    observation: Any,
    reflection: Any,
    raw_final_text: str | None,
    fallback_text: str = "",
) -> str:
    """Phase 10 §6 Finalizer wiring.

    - ``finalizer_enabled=False`` short-circuits with the raw final text
      (or the fallback) so the user gets *something* without invoking
      the deterministic Finalizer composition layer.
    - ``finalizer_timeout_sec`` bounds the (currently synchronous, but
      future-proof) finalize call via ``asyncio.wait_for``. If the
      Finalizer ever becomes async / LLM-driven the timeout already
      applies.
    """
    if policy is not None and not getattr(policy, "finalizer_enabled", True):
        return (raw_final_text or fallback_text or decision.reason or "").strip()
    from mini_claw.agent.finalizer import finalize_response

    timeout = (
        getattr(policy, "finalizer_timeout_sec", 20) if policy is not None else 20
    )

    async def _finalize_async() -> str:
        return finalize_response(
            decision=decision,
            observation=observation,
            reflection=reflection,
            raw_final_text=raw_final_text,
        )

    try:
        return await asyncio.wait_for(_finalize_async(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("finalizer timed out after %ss", timeout)
        return (raw_final_text or fallback_text or decision.reason or "").strip()
    except Exception:
        logger.warning("finalizer call failed", exc_info=True)
        return (raw_final_text or fallback_text or decision.reason or "").strip()


def _persist_react_step(ctx: AgentContext, step: Any) -> None:
    """Best-effort upsert of a ReActStep into ``react_steps``."""
    storage = getattr(ctx, "storage", None)
    if storage is None:
        return
    try:
        now = int(time.time())
        is_new = not step.created_at
        if is_new:
            step.created_at = now
        step.updated_at = now
        storage.execute(
            "INSERT OR REPLACE INTO react_steps "
            "(step_id, run_id, chat_id, agent_id, iteration, action_phase, "
            " assistant_content_hash, tool_calls_json, tool_call_refs_json, "
            " permission_decisions_json, observation_json, reflection_json, "
            " reflection_triggered, reflection_reasons_json, user_updates_json, "
            " decision, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                step.step_id,
                step.run_id,
                step.chat_id,
                step.agent_id,
                step.iteration,
                step.action_phase,
                step.assistant_content_hash,
                json.dumps(step.tool_calls, ensure_ascii=False) if step.tool_calls else None,
                json.dumps(step.tool_call_refs, ensure_ascii=False) if step.tool_call_refs else None,
                json.dumps(step.permission_decisions, ensure_ascii=False) if step.permission_decisions else None,
                json.dumps(step.observation, ensure_ascii=False) if step.observation else None,
                json.dumps(step.reflection, ensure_ascii=False) if step.reflection else None,
                1 if step.reflection_triggered else 0,
                json.dumps(step.reflection_reasons, ensure_ascii=False) if step.reflection_reasons else None,
                json.dumps(step.user_updates, ensure_ascii=False) if step.user_updates else None,
                step.decision,
                step.status,
                step.created_at,
                step.updated_at,
            ),
        )
        if is_new and ctx.audit_logger:
            try:
                ctx.audit_logger.log_security_event(
                    event_type="react_step_created",
                    details={
                        "run_id": step.run_id,
                        "step_id": step.step_id,
                        "iteration": step.iteration,
                        "action_phase": step.action_phase,
                    },
                    chat_id=step.chat_id,
                    agent_id=step.agent_id,
                )
            except Exception:
                pass
    except Exception:
        logger.debug("react_steps persist failed", exc_info=True)


def _open_step(run: AgentRun, action_phase: str) -> Any:
    """Allocate a new ReActStep skeleton. Caller updates fields then persists."""
    from mini_claw.agent.react_models import ReActStep

    run.step_counter += 1
    step = ReActStep(
        step_id=_new_step_id(run.id, run.step_counter),
        run_id=run.id,
        chat_id=run.chat_id,
        agent_id=run.agent_id,
        iteration=run.step_counter,
        action_phase=action_phase,  # type: ignore[arg-type]
        status="running",
    )
    run.react_steps.append(step)
    return step


async def _emit_react_update(
    ctx: AgentContext,
    run: AgentRun,
    step: Any,
    *,
    event_type: str,
    candidate_text: str,
    visible_level: str = "normal",
    is_important: bool = False,
) -> bool:
    """Build, sanitize, send and persist a single ReActUserUpdate.

    Returns True iff the update was sent to the channel. Sending failures
    are non-blocking — they never raise and never abort the loop.

    Persistence contract (Phase 10 M10.1):
    - The update row is **always** stored in ``react_user_updates`` (even
      when there is no channel callback or sending fails) so the trace
      layer remains complete.
    - When a channel callback is registered, it owns channel-send + the
      ``messages.message_kind='react_update'`` mirror; this function only
      hands the prepared update over and records send status.

    Phase 10 §6 knobs consumed here:
    - ``react_user_updates_sanitize_completion_claims``: when False the
      sanitizer accepts ``action_planned`` text containing completion
      claims (otherwise the legacy guard rejects it).
    - ``react_user_updates_store_redacted_text``: forwarded to
      :func:`store_react_update`; when False ``redacted_text`` is NULL.
    - ``react_user_updates_send_failure_non_blocking``: when False, a
      send-side exception re-raises so the surrounding loop turns into a
      hard error. Default True preserves the historical fail-soft path.
    """
    if not candidate_text:
        return False
    if not getattr(ctx, "react_user_updates_enabled", True):
        return False

    from mini_claw.agent.react_update import (
        make_update,
        prepare_react_update_text,
        should_send_update,
        store_react_update,
    )

    # When the operator has flipped sanitize_completion_claims off, treat
    # the candidate as ``observation_summary`` for sanitize purposes —
    # that branch already skips the completion-claim guard.
    sanitize_event = event_type
    if (
        event_type == "action_planned"
        and not getattr(ctx, "react_user_updates_sanitize_completion_claims", True)
    ):
        sanitize_event = "observation_summary"

    prepared = prepare_react_update_text(
        candidate_text,
        max_chars=getattr(ctx, "react_user_update_max_chars", 160),
        event_type=sanitize_event,  # type: ignore[arg-type]
    )
    if prepared is None:
        return False
    final_text, text_hash = prepared

    update = make_update(
        step_id=step.step_id,
        run_id=run.id,
        chat_id=run.chat_id,
        agent_id=run.agent_id,
        event_type=event_type,  # type: ignore[arg-type]
        final_text=final_text,
        text_hash=text_hash,
        visible_level=visible_level,  # type: ignore[arg-type]
        is_important=is_important,
    )

    mode = getattr(ctx, "react_user_update_mode", "normal")
    on_react_update = getattr(ctx, "on_react_update", None)
    store_redacted = getattr(ctx, "react_user_updates_store_redacted_text", True)

    if not should_send_update(update, mode) or on_react_update is None:
        update.send_status = "skipped"
        # Persist skipped updates so the trace shows what was suppressed.
        store_react_update(
            getattr(ctx, "storage", None), update, store_redacted_text=store_redacted
        )
        step.user_updates.append({
            "update_id": update.update_id,
            "event_type": update.event_type,
            "send_status": update.send_status,
            "is_important": update.is_important,
            "text_hash": update.text_hash,
        })
        return False

    non_blocking = getattr(ctx, "react_user_updates_send_failure_non_blocking", True)
    try:
        sent = await on_react_update(update)
    except Exception:
        logger.warning("react update callback raised", exc_info=True)
        update.send_status = "failed"
        sent = False
        if not non_blocking:
            store_react_update(
                getattr(ctx, "storage", None),
                update,
                store_redacted_text=store_redacted,
            )
            raise

    if update.send_status == "pending":
        update.send_status = "sent" if sent else "failed"
    if sent and update.sent_at is None:
        update.sent_at = int(time.time())

    # Best-effort double-write: the router's _send_react_user_update also
    # persists, but if that path was bypassed (eg. tests using a bare
    # callback), this guarantees the row exists.
    store_react_update(
        getattr(ctx, "storage", None), update, store_redacted_text=store_redacted
    )

    step.user_updates.append({
        "update_id": update.update_id,
        "event_type": update.event_type,
        "send_status": update.send_status,
        "is_important": update.is_important,
        "text_hash": update.text_hash,
    })
    if update.event_type == "action_planned" and update.send_status in ("sent", "failed"):
        run.react_action_planned_sent_steps.add(step.step_id)

    if ctx.audit_logger:
        try:
            ev = "react_user_update_sent" if sent else (
                "react_user_update_skipped"
                if update.send_status == "skipped"
                else "react_user_update_failed"
            )
            ctx.audit_logger.log_security_event(
                event_type=ev,
                details={
                    "run_id": run.id,
                    "step_id": step.step_id,
                    "event_type": update.event_type,
                    "text_hash": update.text_hash,
                    "is_important": update.is_important,
                },
                chat_id=run.chat_id,
                agent_id=run.agent_id,
            )
        except Exception:
            pass

    return bool(sent)


async def _process_tool_calls_parallel(
    calls: list[tuple[int, Any, Any]],  # (index, tool_call, pre-evaluated decision)
    run: AgentRun,
    registry: ToolRegistry,
    permission_gate: Any,
    result_processor: Any,
    tool_ctx: ToolContext,
    ctx: AgentContext,
) -> list[tuple[int, Any, dict[str, Any]]]:
    """Process multiple tool calls in parallel and return results.

    Phase 0.7: ``calls`` now carries pre-evaluated decisions from the
    permission gate. The parallel batch should only receive ``allow``
    decisions, but we defensively check and downgrade any stragglers.
    """

    async def _safe_process(tc, decision):
        """Wrapper that short-circuits non-allow decisions."""
        if decision.action != "allow":
            if ctx.audit_logger:
                ctx.audit_logger.log_security_event(
                    event_type="parallel_precheck_violation",
                    details={"tool": tc.name, "args": tc.arguments, "action": decision.action},
                    chat_id=ctx.chat_id,
                    agent_id=ctx.agent_id,
                )
            return {
                "role": "tool",
                "tool_call_id": tc.id,
                "content": "[ERROR] Internal: non-allow in parallel batch",
            }
        # Already evaluated; mark decision on tc so _process_single_tool_call can skip re-eval.
        tc._precheck_decision = decision
        return await _process_single_tool_call(tc, run, registry, permission_gate, result_processor, tool_ctx, ctx)

    tasks = [_safe_process(tc, decision) for idx, tc, decision in calls]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    output = []
    for (idx, tc, decision), result in zip(calls, results):
        if isinstance(result, Exception):
            result_msg = {
                "role": "tool",
                "tool_call_id": tc.id,
                "content": f"[error] {type(result).__name__}: {result}",
            }
        else:
            result_msg = result
        output.append((idx, tc, result_msg))
    return output


async def _finalize_direct_answer(
    *,
    run: AgentRun,
    ctx: AgentContext,
    provider: Provider,
    response_text: str | None,
    hallucination_triggered: bool,
) -> None:
    """Phase 10 M10.2: finish a direct-answer iteration via Observation+Decision.

    Always sets ``run.status``. When a Reflection flips the decision to
    ``block`` or ``fail``, ``run.status`` is set to ``ABORTED`` and
    ``run.final_answer`` is replaced with the Finalizer's text.

    Reflection only fires when ``ctx.react_policy`` is set; otherwise the
    legacy direct-answer path is preserved (status = DONE).
    """
    from mini_claw.agent.observation import build_direct_answer_observation
    from mini_claw.agent.react_decision import decide_from_reflection
    from mini_claw.agent.reflection import fallback_reflection, run_reflection
    from mini_claw.agent.reflection_trigger import should_reflect

    step = _open_step(run, action_phase="direct_answer")
    step.assistant_content_hash = (
        hashlib.sha256((response_text or "").encode("utf-8")).hexdigest()[:16]
        if response_text
        else None
    )
    policy = getattr(ctx, "react_policy", None)
    obs_max_chars = getattr(policy, "max_observation_chars", None) if policy else None
    observation = build_direct_answer_observation(
        response_text or "", max_chars=obs_max_chars
    )
    step.observation = {
        "observation_type": observation.observation_type,
        "summary": observation.summary,
    }

    if policy is None:
        # Legacy path — no Phase 10 reflection.
        step.decision = "finalize"
        step.status = "completed"
        _persist_react_step(ctx, step)
        run.status = RunOutcome.DONE
        return

    trigger = should_reflect(
        observation,
        iteration=run.iterations,
        max_iterations=MAX_ITERATIONS,
        policy=policy,
        repeated_tool_call_detected=run.repeated_tool_call_detected,
        hallucination_guard_triggered=hallucination_triggered,
    )
    step.reflection_triggered = trigger.should_reflect
    step.reflection_reasons = list(trigger.reasons)

    if trigger.should_reflect:
        # Phase 10 §6 ``reflect_before_finalize_mode``:
        # - "deterministic_first" (default) — skip the LLM unless we
        #   have a terminal or heavy trigger that genuinely needs a
        #   structured re-evaluation.
        # - "always" — always call the Reflection LLM regardless of
        #   trigger.terminal/heavy heuristics.
        before_finalize_mode = getattr(
            policy, "reflect_before_finalize_mode", "deterministic_first"
        )
        if before_finalize_mode == "always":
            use_llm = True
        else:
            use_llm = trigger.terminal or any(
                r in {"tool_error", "hallucination_guard", "repeated_tool_call"}
                for r in trigger.reasons
            )
        if use_llm:
            reflection = await run_reflection(
                provider=getattr(ctx, "reflection_provider", None) or provider,
                observation=observation,
                original_goal_summary=_truncate_for_reflection(
                    run.original_goal_summary or run.original_goal_raw or "",
                    getattr(policy, "max_reflection_chars", 4000),
                ),
                iteration=run.iterations,
                max_iterations=MAX_ITERATIONS,
                trigger_reasons=trigger.reasons,
                timeout_sec=policy.reflection_timeout_sec,
                max_reflection_chars=getattr(policy, "max_reflection_chars", 4000),
            )
        else:
            reflection = fallback_reflection(observation)
    else:
        reflection = fallback_reflection(observation)

    # Phase 10 §6 ``store_reflection``: when False, we still need a
    # decision but skip writing the reflection blob to the step.
    if getattr(policy, "store_reflection", True):
        step.reflection = {
            "decision": reflection.decision,
            "goal_status": reflection.goal_status,
            "safety_assessment": reflection.safety_assessment,
            "confidence": reflection.confidence,
            "fallback_used": reflection.fallback_used,
            "parse_failed": reflection.parse_failed,
        }
    else:
        step.reflection = {"decision": reflection.decision, "stored": False}
    decision = decide_from_reflection(observation, reflection)

    if ctx.audit_logger:
        try:
            ctx.audit_logger.log_security_event(
                event_type="react_reflection_completed",
                details={
                    "run_id": run.id,
                    "step_id": step.step_id,
                    "decision": reflection.decision,
                    "fallback_used": reflection.fallback_used,
                    "parse_failed": reflection.parse_failed,
                },
                chat_id=run.chat_id,
                agent_id=run.agent_id,
            )
            ctx.audit_logger.log_security_event(
                event_type="react_decision_made",
                details={
                    "run_id": run.id,
                    "step_id": step.step_id,
                    "action": decision.action,
                    "reason": decision.reason,
                },
                chat_id=run.chat_id,
                agent_id=run.agent_id,
            )
        except Exception:
            pass

    if decision.action == "finalize":
        step.decision = "finalize"
        step.status = "completed"
        run.status = RunOutcome.DONE
        if ctx.audit_logger:
            try:
                ctx.audit_logger.log_security_event(
                    event_type="react_finalized",
                    details={
                        "run_id": run.id,
                        "step_id": step.step_id,
                        "via": "direct_answer",
                    },
                    chat_id=run.chat_id,
                    agent_id=run.agent_id,
                )
            except Exception:
                pass
    elif decision.action == "block":
        step.decision = "blocked"
        step.status = "completed"
        run.status = RunOutcome.ABORTED
        run.final_answer = await _run_finalizer(
            policy=policy,
            decision=decision,
            observation=observation,
            reflection=reflection,
            raw_final_text=response_text,
        )
    elif decision.action == "fail":
        step.decision = "failed"
        step.status = "failed"
        run.status = RunOutcome.ABORTED
        run.final_answer = await _run_finalizer(
            policy=policy,
            decision=decision,
            observation=observation,
            reflection=reflection,
            raw_final_text=response_text,
        )
    elif decision.action == "suspend":
        step.decision = "suspended"
        step.status = "suspended"
        run.status = RunOutcome.SUSPENDED
    else:  # continue
        step.decision = "continue"
        step.status = "completed"
        run.status = RunOutcome.DONE

    run.final_reflection_json = json.dumps(step.reflection, ensure_ascii=False)

    # Optional decision_summary update for important outcomes.
    if decision.action in ("block", "fail", "suspend"):
        await _emit_react_update(
            ctx,
            run,
            step,
            event_type="decision_summary",
            candidate_text=decision.final_response_hint or decision.reason,
            visible_level="normal",
            is_important=True,
        )

    _persist_react_step(ctx, step)


async def run_agent_step(
    run: AgentRun,
    provider: Provider,
    registry: ToolRegistry,
    permission_gate: Any,
    result_processor: Any,
    ctx: AgentContext,
) -> AgentRun:
    """Execute one iteration cycle of the agent loop.

    Calls the LLM, processes tool calls (with permission checks and
    duplicate detection), and loops until done, suspended, or aborted.
    """
    tool_schemas = registry.schemas_for(run.allowed_tools)
    tool_ctx = _build_tool_context(ctx)

    # Build streaming callback if channel supports it
    stream_callback = None
    if hasattr(ctx.channel, 'send_stream_chunk'):
        def _on_chunk(delta: str) -> None:
            # Fire-and-forget: schedule on event loop
            import asyncio
            try:
                asyncio.create_task(ctx.channel.send_stream_chunk(ctx.chat_id, delta))
            except Exception:
                pass
        stream_callback = _on_chunk

    while run.iterations < MAX_ITERATIONS:
        run.iterations += 1

        # Phase 9.8 M1: Send progress update to user
        if PROGRESS_NOTIFY_INTERVAL > 0 and run.iterations % PROGRESS_NOTIFY_INTERVAL == 0:
            last_tool = run.tool_call_history[-1][0] if run.tool_call_history else None
            await _send_progress_update(run, ctx, run.iterations, last_tool)

        # Phase 9.8 M2: Detect tool call loops
        is_looping, loop_tool = _detect_tool_call_loop(run)
        if is_looping:
            # Inject a system message telling LLM to change strategy
            loop_warning = (
                f"⚠️ 系统提示：你已经连续多次调用 `{loop_tool}` 工具但未成功。"
                f"请换一个不同的方法或工具来解决问题，不要再重复调用 `{loop_tool}`。"
            )
            run.messages.append({
                "role": "system",
                "content": loop_warning,
            })
            logger.warning(
                "Tool call loop detected: %s called %d times in recent history",
                loop_tool,
                sum(1 for name, _ in run.tool_call_history[-5:] if name == loop_tool)
            )

        # Tool calls are materially more reliable in non-streaming mode for
        # OpenAI-compatible providers such as DeepSeek: streamed tool calls can
        # arrive as partial name/arguments deltas and older parsers may lose or
        # truncate arguments. Prefer correctness over token-by-token UI updates
        # whenever tools are available for this turn.
        use_stream = stream_callback is not None and not tool_schemas

        response = await provider.chat(
            messages=_messages_for_provider(run, ctx),
            tools=tool_schemas if tool_schemas else None,
            stream=use_stream,
            stream_callback=stream_callback if use_stream else None,
        )

        # Phase 10 M10.1: surface intermediate LLM text via on_progress only
        # when there is no ReActUserUpdate channel — otherwise the
        # action_planned update has already shown the user what's coming
        # and we'd duplicate the message.
        first_action_planned_emitted = bool(run.react_action_planned_sent_steps)
        should_send_immediately = (
            response.text
            and ctx.on_progress
            and response.tool_calls
            and ctx.on_react_update is None
            and first_action_planned_emitted is False
        )
        if should_send_immediately and ctx.on_progress is not None and response.text is not None:
            try:
                await ctx.on_progress(response.text)
            except Exception:
                logger.warning("Failed to send intermediate LLM response", exc_info=True)

        # No tool calls -> check for hallucination before marking complete
        if not response.tool_calls or response.finish_reason != "tool_calls":
            # Hallucination detection: model claims action completed without calling tools
            # Only trigger if the response contains BOTH action verbs AND completion indicators
            text_lower = (response.text or "").lower()

            # Action verbs that indicate an operation was performed
            action_verbs = [
                "创建", "写入", "删除", "执行", "运行", "索引",
                "create", "write", "delete", "execute", "run", "index"
            ]

            # Completion indicators that claim the action is done
            completion_indicators = [
                "已", "完成", "成功", "好的",  # Chinese
                "done", "success", "complet", "has been", "have been"  # English
            ]

            # Check if BOTH patterns exist (reduces false positives)
            has_action = any(verb in text_lower for verb in action_verbs)
            has_completion = any(indicator in text_lower for indicator in completion_indicators)

            # Additional check: does the message look like it's claiming to have acted?
            likely_hallucination = (
                has_action and has_completion and
                len(response.text or "") < 200  # Short messages are more likely to be claims vs explanations
            )

            if likely_hallucination:
                # Determine which tool should have been called based on action verb
                suggested_tool = None
                if any(w in text_lower for w in ["创建", "写入", "create", "write"]):
                    suggested_tool = "write_file"
                elif any(w in text_lower for w in ["删除", "delete", "remove"]):
                    suggested_tool = "delete_file"
                elif any(w in text_lower for w in ["执行", "运行", "execute", "run"]):
                    suggested_tool = "run_shell"
                elif any(w in text_lower for w in ["索引", "index"]):
                    suggested_tool = "index_context"

                tool_hint = f"例如 {suggested_tool}" if suggested_tool else ""

                # Insert correction message and continue loop to force tool use
                run.messages.append({"role": "assistant", "content": response.text})
                run.messages.append({
                    "role": "user",
                    "content": (
                        f"[SYSTEM] 你刚才声称完成了操作，但没有实际调用任何工具。\n"
                        f"请使用相应的工具来完成用户的请求{tool_hint}。\n"
                        f"工具调用是强制性的，不是可选的。用户需要看到实际的工具执行记录。"
                    )
                })
                continue  # Force another iteration

            # Normal completion: no hallucination detected
            # Phase 10 M10.2: open a direct_answer step + optional Reflection.
            await _finalize_direct_answer(
                run=run,
                ctx=ctx,
                provider=provider,
                response_text=response.text,
                hallucination_triggered=likely_hallucination,
            )
            if run.status != RunOutcome.DONE:
                # Reflection / Decision flipped the outcome (block/fail).
                return run
            run.final_answer = response.text if not run.final_answer else run.final_answer
            if response.text:
                run.messages.append({"role": "assistant", "content": response.text})
            return run

        # Append assistant message with tool_calls
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": response.text or None,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in response.tool_calls
            ],
        }
        run.messages.append(assistant_msg)

        # Phase 10 M10.1: open a ReActStep for this tool_call iteration and
        # emit an ``action_planned`` ReActUserUpdate. The new pipeline runs
        # alongside the legacy prelude path; if ``on_react_update`` is unset
        # the step is still recorded for trace continuity but no message is
        # sent.
        tool_step = _open_step(run, action_phase="tool_call")
        tool_step.assistant_content_hash = (
            hashlib.sha256((response.text or "").encode("utf-8")).hexdigest()[:16]
            if response.text
            else None
        )
        tool_step.tool_calls = [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
            for tc in response.tool_calls
        ]
        tool_step.tool_call_refs = [
            {"id": tc.id, "name": tc.name} for tc in response.tool_calls
        ]
        # Phase 10 M10.2: per-iteration outcome tracker. Higher severity
        # entries override lower so the iteration's Observation reflects
        # the worst event we saw. Severity ranking: permission_denied >
        # chain_blocked > tool_error > tool_success.
        iter_outcome: dict[str, Any] = {
            "type": None,
            "tool_name": None,
            "summary": "",
            "reason": "",
        }

        def _bump_outcome(kind: str, *, tool_name: str | None = None,
                          summary: str = "", reason: str = "") -> None:
            severity = {
                "permission_denied": 4,
                "chain_blocked": 3,
                "tool_error": 2,
                "tool_success": 1,
                "approval_required": 5,  # never observed inline (suspends), but kept for completeness
            }
            cur = iter_outcome["type"]
            if cur is None or severity.get(kind, 0) > severity.get(cur, 0):
                iter_outcome["type"] = kind
                iter_outcome["tool_name"] = tool_name
                iter_outcome["summary"] = summary
                iter_outcome["reason"] = reason
        if getattr(ctx, "on_react_update", None) is not None:
            from mini_claw.agent.react_update import (
                generate_action_planned_from_tools,
            )

            candidate = response.text or generate_action_planned_from_tools(
                [tc.name for tc in response.tool_calls]
            )
            await _emit_react_update(
                ctx,
                run,
                tool_step,
                event_type="action_planned",
                candidate_text=candidate,
                visible_level="normal",
            )

        # Phase 10 M10.1: legacy on_prelude branch removed — the new flow
        # only ever emits ReActUserUpdate('action_planned') above. Tests
        # that still drive on_prelude directly (no run_agent_step) keep
        # working because the AgentContext field is still present.

        # Group tool calls: parallel-safe (L0 + allow) vs sequential.
        # Phase 0.7: Pre-check permissions for every call BEFORE splitting
        # into batches. An L0 tool whose args point to a sensitive path
        # (e.g. list_directory(".ssh")) will be denied by evaluate even
        # though the tool metadata says L0; those go to the sequential path
        # for proper error obfuscation and audit.
        parallel_calls: list[tuple[int, Any, Any]] = []  # (index, tc, decision)
        sequential_calls: list[tuple[int, Any, Any]] = []  # (index, tc, decision)

        for idx, tc in enumerate(response.tool_calls):
            tool = registry.get(tc.name)
            if tool is None:
                # Unknown tool -> sequential (will error there)
                sequential_calls.append((idx, tc, None))
                continue

            # Pre-evaluate every call to classify by actual permission outcome.
            decision = permission_gate.evaluate(
                tool=tc.name, args=tc.arguments, ctx=_ctx_to_dict(ctx)
            )

            # Only calls that are BOTH allowed AND L0-rated go parallel.
            if decision.action == "allow" and tool.permission_level == "L0":
                parallel_calls.append((idx, tc, decision))
            else:
                # deny / need_approval / non-L0 → sequential
                sequential_calls.append((idx, tc, decision))

        # Process parallel calls first
        if parallel_calls:
            results = await _process_tool_calls_parallel(
                parallel_calls, run, registry, permission_gate, result_processor, tool_ctx, ctx
            )
            for idx, tc, result_msg in results:
                run.messages.append(result_msg)
                # Phase 10: feed parallel-batch outcomes into the per-iteration
                # tracker so should_reflect / DecisionController see them.
                content = result_msg.get("content", "") if isinstance(result_msg, dict) else ""
                if isinstance(content, str) and content.startswith("[denied]"):
                    _bump_outcome(
                        "permission_denied",
                        tool_name=tc.name,
                        summary=content,
                        reason=content,
                    )
                elif isinstance(content, str) and content.startswith("[error]"):
                    _bump_outcome(
                        "tool_error",
                        tool_name=tc.name,
                        summary=content,
                        reason=content,
                    )
                else:
                    _bump_outcome(
                        "tool_success",
                        tool_name=tc.name,
                        summary=str(content)[:300],
                    )

        # Process sequential calls
        for idx, tc, precheck_decision in sequential_calls:
            sig = _call_signature(tc.name, tc.arguments)

            # Duplicate detection
            if sig in run.seen_calls:
                run.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": "[duplicate call skipped]",
                })
                continue
            run.seen_calls.add(sig)

            # Permission check (Phase 0.7: reuse precheck if available)
            tool = registry.get(tc.name)
            if tool is None:
                run.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": f"[error] unknown tool: {tc.name}",
                })
                continue

            # If we have a precheck decision from the split phase, use it;
            # otherwise evaluate now (this path only triggers if the split
            # logic put a None-decision call here, e.g. unknown tool).
            if precheck_decision is not None:
                decision = precheck_decision
            else:
                decision = permission_gate.evaluate(
                    tool=tc.name, args=tc.arguments, ctx=_ctx_to_dict(ctx)
                )

            # Handle audit event if present
            if decision.audit_event:
                debug_id = ctx.audit_logger.log_security_event(
                    event_type=decision.audit_event["event_type"],
                    details=decision.audit_event,
                    chat_id=ctx.chat_id,
                    agent_id=ctx.agent_id,
                )
                decision = decision.__class__(
                    action=decision.action,
                    reason=decision.reason.replace("{debug_id}", debug_id),
                    internal_reason=decision.internal_reason,
                    audit_event=decision.audit_event,
                )

            if decision.action == "deny":
                run.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": f"[denied] {decision.reason}",
                })
                tool_step.permission_decisions.append({
                    "tool": tc.name,
                    "action": "deny",
                    "reason": decision.reason,
                })
                _bump_outcome(
                    "permission_denied",
                    tool_name=tc.name,
                    summary=f"PermissionGate denied {tc.name}",
                    reason=decision.reason,
                )
                continue

            if decision.action == "need_approval":
                approval_id = str(uuid.uuid4())
                run.pending_approval_id = approval_id
                run.pending_tool_call = json.dumps({
                    "id": tc.id,
                    "name": tc.name,
                    "arguments": tc.arguments,
                    "level": getattr(decision, "permission_level", tool.permission_level),
                })
                run.status = RunOutcome.SUSPENDED
                # Phase 10: persist the current tool_step as suspended.
                # When react_policy is set we also synthesize a Reflection
                # snapshot (needs_approval / decision=suspended) so the
                # trace shows a complete Observation→Reflection→Decision
                # cycle for this step.
                if run.react_steps:
                    suspend_step = run.react_steps[-1]
                    suspend_step.action_phase = "approval_required"
                    suspend_step.decision = "suspended"
                    suspend_step.status = "suspended"
                    suspend_step.permission_decisions.append({
                        "tool": tc.name,
                        "action": "need_approval",
                        "approval_id": approval_id,
                    })
                    suspend_step.observation = {
                        "observation_type": "approval_required",
                        "tool_name": tc.name,
                        "summary": f"approval required for {tc.name}",
                        "permission_action": "need_approval",
                    }
                    if getattr(ctx, "react_policy", None) is not None:
                        from mini_claw.agent.observation import (
                            build_approval_required_observation,
                        )
                        from mini_claw.agent.reflection import fallback_reflection
                        from mini_claw.agent.reflection_trigger import should_reflect

                        observation = build_approval_required_observation(
                            tc.name, decision.reason or "needs approval"
                        )
                        trigger = should_reflect(
                            observation,
                            iteration=run.iterations,
                            max_iterations=MAX_ITERATIONS,
                            policy=ctx.react_policy,
                        )
                        suspend_step.reflection_triggered = trigger.should_reflect
                        suspend_step.reflection_reasons = list(trigger.reasons)
                        reflection = fallback_reflection(observation)
                        suspend_step.reflection = {
                            "decision": reflection.decision,
                            "goal_status": reflection.goal_status,
                            "safety_assessment": reflection.safety_assessment,
                            "confidence": reflection.confidence,
                            "fallback_used": True,
                        }
                        if ctx.audit_logger:
                            try:
                                ctx.audit_logger.log_security_event(
                                    event_type="react_observation_built",
                                    details={
                                        "run_id": run.id,
                                        "step_id": suspend_step.step_id,
                                        "observation_type": "approval_required",
                                        "tool_name": tc.name,
                                    },
                                    chat_id=run.chat_id,
                                    agent_id=run.agent_id,
                                )
                                ctx.audit_logger.log_security_event(
                                    event_type="react_reflection_fallback_used",
                                    details={
                                        "run_id": run.id,
                                        "step_id": suspend_step.step_id,
                                        "decision": reflection.decision,
                                    },
                                    chat_id=run.chat_id,
                                    agent_id=run.agent_id,
                                )
                                ctx.audit_logger.log_security_event(
                                    event_type="react_decision_made",
                                    details={
                                        "run_id": run.id,
                                        "step_id": suspend_step.step_id,
                                        "action": "suspend",
                                        "reason": "approval required",
                                    },
                                    chat_id=run.chat_id,
                                    agent_id=run.agent_id,
                                )
                            except Exception:
                                pass
                    _persist_react_step(ctx, suspend_step)
                return run

            # decision.action == "allow"
            # Chain attack detection (pre-tool)
            if hasattr(ctx, "chain_detector") and ctx.chain_detector:
                blocked = ctx.chain_detector.evaluate_before_tool(tc, run, ctx)
                if blocked:
                    chain_action = blocked.get("action", "deny")
                    audit_event = blocked.get("audit_event") or {}
                    event_type = audit_event.get("event_type", "chain_attack_blocked")
                    debug_id = ""
                    if ctx.audit_logger:
                        debug_id = ctx.audit_logger.log_security_event(
                            event_type=event_type,
                            details=audit_event,
                            chat_id=ctx.chat_id,
                            agent_id=ctx.agent_id,
                        )
                    # Phase 9 横切: warn = audit only, continue execution
                    if chain_action != "warn":
                        run.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": f"[denied] Chain attack detected. debug_id={debug_id}",
                        })
                        tool_step.permission_decisions.append({
                            "tool": tc.name,
                            "action": "chain_block",
                            "reason": audit_event.get("event_type", "chain_attack_blocked"),
                            "debug_id": debug_id,
                        })
                        _bump_outcome(
                            "chain_blocked",
                            tool_name=tc.name,
                            summary=f"ChainDetector blocked {tc.name}",
                            reason=str(audit_event.get("event_type", "chain_attack_blocked")),
                        )
                        continue

            try:
                result = await tool.handler(**tc.arguments, ctx=tool_ctx)
                # Phase 9.8 M2: Record successful tool call
                run.tool_call_history.append((tc.name, True))
                _bump_outcome(
                    "tool_success",
                    tool_name=tc.name,
                    summary=str(result)[:300] if result is not None else "",
                )
            except TypeError as exc:
                result = f"[error] tool {tc.name} rejected arguments: {exc}"
                run.tool_call_history.append((tc.name, False))
                _bump_outcome(
                    "tool_error",
                    tool_name=tc.name,
                    summary=result,
                    reason=str(exc),
                )
            except Exception as exc:
                run.tool_call_history.append((tc.name, False))
                if result_processor:
                    result = result_processor.process_error(exc)
                else:
                    result = f"[error] {type(exc).__name__}: {exc}"
                _bump_outcome(
                    "tool_error",
                    tool_name=tc.name,
                    summary=result,
                    reason=f"{type(exc).__name__}: {exc}",
                )
            else:
                if result_processor:
                    result = result_processor.process(result, tc.name)

            # Chain attack detection (post-tool observation)
            if hasattr(ctx, "chain_detector") and ctx.chain_detector:
                ctx.chain_detector.observe_after_tool(tc, run, result, success=True, ctx=ctx)

            run.messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

        # Phase 10: at the end of each iteration's tool processing, persist
        # the tool_step (created right after the assistant_msg append). The
        # step is finalized as ``status=observed`` since reflection over
        # tool results is deferred to a future milestone.
        if run.react_steps and run.react_steps[-1].action_phase == "tool_call":
            iter_step = run.react_steps[-1]
            if iter_step.status == "running":
                iter_step.status = "observed"
                iter_step.decision = "continue"

            # Phase 10 M10.2/M10.3: per-iteration Observation + Reflection.
            # Always build the Observation from the worst event we saw
            # this iteration (permission_denied > chain_blocked >
            # tool_error > tool_success). Then ``should_reflect()``
            # decides whether to actually run the LLM Reflection — it
            # covers tool_error / permission_denied / chain_blocked /
            # approval_rejected / hallucination_guard / repeated_tool_call /
            # iteration_threshold for controlled mode, and every-iteration
            # for strict mode.
            policy = getattr(ctx, "react_policy", None)
            if policy is not None:
                from mini_claw.agent.observation import (
                    build_chain_blocked_observation,
                    build_permission_denied_observation,
                    build_tool_error_observation,
                    build_tool_success_observation,
                )
                from mini_claw.agent.react_decision import decide_from_reflection
                from mini_claw.agent.reflection import (
                    fallback_reflection,
                    run_reflection,
                )
                from mini_claw.agent.reflection_trigger import should_reflect

                outcome_kind = iter_outcome["type"] or "tool_success"
                outcome_tool = iter_outcome["tool_name"] or (
                    run.tool_call_history[-1][0] if run.tool_call_history else "?"
                )
                outcome_summary = iter_outcome["summary"]
                outcome_reason = iter_outcome["reason"] or outcome_summary

                obs_max_chars = getattr(policy, "max_observation_chars", None)

                if outcome_kind == "permission_denied":
                    observation = build_permission_denied_observation(
                        outcome_tool, outcome_reason, max_chars=obs_max_chars
                    )
                    iter_step.action_phase = "permission_denied"
                elif outcome_kind == "chain_blocked":
                    observation = build_chain_blocked_observation(
                        outcome_tool, outcome_reason, max_chars=obs_max_chars
                    )
                    iter_step.action_phase = "chain_blocked"
                elif outcome_kind == "tool_error":
                    observation = build_tool_error_observation(
                        outcome_tool, outcome_summary or outcome_reason,
                        max_chars=obs_max_chars,
                    )
                else:
                    observation = build_tool_success_observation(
                        outcome_tool, outcome_summary, max_chars=obs_max_chars
                    )

                iter_step.observation = {
                    "observation_type": observation.observation_type,
                    "tool_name": observation.tool_name,
                    "summary": observation.summary,
                    "permission_action": observation.permission_action,
                    "permission_reason": observation.permission_reason,
                }
                if ctx.audit_logger:
                    try:
                        ctx.audit_logger.log_security_event(
                            event_type="react_observation_built",
                            details={
                                "run_id": run.id,
                                "step_id": iter_step.step_id,
                                "observation_type": observation.observation_type,
                                "tool_name": observation.tool_name,
                            },
                            chat_id=run.chat_id,
                            agent_id=run.agent_id,
                        )
                    except Exception:
                        pass

                # Phase 10 §6 mode policy: surface observation_summary in
                # verbose/debug — gate via _emit_react_update which already
                # consults react_user_update_mode.
                if observation.summary:
                    await _emit_react_update(
                        ctx,
                        run,
                        iter_step,
                        event_type="observation_summary",
                        candidate_text=observation.summary,
                        visible_level="verbose",
                    )

                trigger = should_reflect(
                    observation,
                    iteration=run.iterations,
                    max_iterations=MAX_ITERATIONS,
                    policy=policy,
                    repeated_tool_call_detected=run.repeated_tool_call_detected,
                    hallucination_guard_triggered=run.hallucination_guard_triggered,
                )
                iter_step.reflection_triggered = trigger.should_reflect
                iter_step.reflection_reasons = list(trigger.reasons)
                if trigger.should_reflect:
                    if ctx.audit_logger:
                        try:
                            ctx.audit_logger.log_security_event(
                                event_type="react_reflection_triggered",
                                details={
                                    "run_id": run.id,
                                    "step_id": iter_step.step_id,
                                    "reasons": trigger.reasons,
                                    "terminal": trigger.terminal,
                                },
                                chat_id=run.chat_id,
                                agent_id=run.agent_id,
                            )
                        except Exception:
                            pass
                    # Phase 10 §6 ``reflect_before_finalize_mode``: same
                    # branching as the direct-answer path (see
                    # ``_finalize_direct_answer``). "always" forces an
                    # LLM call regardless of the heuristic; the default
                    # "deterministic_first" sticks with terminal/heavy-trigger
                    # gating.
                    before_finalize_mode = getattr(
                        policy, "reflect_before_finalize_mode", "deterministic_first"
                    )
                    if before_finalize_mode == "always":
                        use_llm = True
                    else:
                        use_llm = trigger.terminal or any(
                            r in {"tool_error", "hallucination_guard", "repeated_tool_call"}
                            for r in trigger.reasons
                        )
                    if use_llm:
                        reflection = await run_reflection(
                            provider=getattr(ctx, "reflection_provider", None) or provider,
                            observation=observation,
                            original_goal_summary=_truncate_for_reflection(
                                run.original_goal_summary
                                or run.original_goal_raw
                                or "",
                                getattr(policy, "max_reflection_chars", 4000),
                            ),
                            iteration=run.iterations,
                            max_iterations=MAX_ITERATIONS,
                            trigger_reasons=trigger.reasons,
                            timeout_sec=policy.reflection_timeout_sec,
                            max_reflection_chars=getattr(
                                policy, "max_reflection_chars", 4000
                            ),
                        )
                    else:
                        reflection = fallback_reflection(observation)
                    if getattr(policy, "store_reflection", True):
                        iter_step.reflection = {
                            "decision": reflection.decision,
                            "goal_status": reflection.goal_status,
                            "safety_assessment": reflection.safety_assessment,
                            "confidence": reflection.confidence,
                            "fallback_used": reflection.fallback_used,
                            "parse_failed": reflection.parse_failed,
                            "timed_out": getattr(reflection, "timed_out", False),
                        }
                    else:
                        iter_step.reflection = {
                            "decision": reflection.decision,
                            "stored": False,
                        }
                    # Phase 10 §6 mode policy: emit a structured
                    # ``reflection_summary`` for debug mode. Per P10/P11
                    # the user-facing text never exposes the raw chain of
                    # thought — only goal_status + decision + a single
                    # safe_next_action sentence.
                    short_reflection = (
                        f"goal={reflection.goal_status}, decision={reflection.decision}"
                        + (
                            f", next={reflection.safe_next_action}"
                            if reflection.safe_next_action
                            else ""
                        )
                    )
                    await _emit_react_update(
                        ctx,
                        run,
                        iter_step,
                        event_type="reflection_summary",
                        candidate_text=short_reflection,
                        visible_level="debug",
                    )
                    if ctx.audit_logger:
                        try:
                            if getattr(reflection, "timed_out", False):
                                ctx.audit_logger.log_security_event(
                                    event_type="react_reflection_timeout",
                                    details={
                                        "run_id": run.id,
                                        "step_id": iter_step.step_id,
                                        "timeout_sec": policy.reflection_timeout_sec,
                                    },
                                    chat_id=run.chat_id,
                                    agent_id=run.agent_id,
                                )
                            evt = (
                                "react_reflection_fallback_used"
                                if reflection.fallback_used
                                else "react_reflection_completed"
                            )
                            ctx.audit_logger.log_security_event(
                                event_type=evt,
                                details={
                                    "run_id": run.id,
                                    "step_id": iter_step.step_id,
                                    "decision": reflection.decision,
                                    "parse_failed": reflection.parse_failed,
                                },
                                chat_id=run.chat_id,
                                agent_id=run.agent_id,
                            )
                            if reflection.parse_failed and not getattr(
                                reflection, "timed_out", False
                            ):
                                ctx.audit_logger.log_security_event(
                                    event_type="react_reflection_parse_failed",
                                    details={"run_id": run.id, "step_id": iter_step.step_id},
                                    chat_id=run.chat_id,
                                    agent_id=run.agent_id,
                                )
                        except Exception:
                            pass

                    decision = decide_from_reflection(observation, reflection)
                    iter_step.decision = (
                        "blocked"
                        if decision.action == "block"
                        else (
                            "failed"
                            if decision.action == "fail"
                            else (
                                "finalize"
                                if decision.action == "finalize"
                                else "continue"
                            )
                        )
                    )
                    if ctx.audit_logger:
                        try:
                            ctx.audit_logger.log_security_event(
                                event_type="react_decision_made",
                                details={
                                    "run_id": run.id,
                                    "step_id": iter_step.step_id,
                                    "action": decision.action,
                                    "reason": decision.reason,
                                },
                                chat_id=run.chat_id,
                                agent_id=run.agent_id,
                            )
                        except Exception:
                            pass
                    if decision.action in ("block", "fail"):
                        run.status = (
                            RunOutcome.ABORTED
                        )
                        run.final_answer = await _run_finalizer(
                            policy=policy,
                            decision=decision,
                            observation=observation,
                            reflection=reflection,
                            raw_final_text=None,
                        )
                        # Phase 10 §14: emit specific block-cause events.
                        if ctx.audit_logger:
                            try:
                                if observation.observation_type == "permission_denied":
                                    ctx.audit_logger.log_security_event(
                                        event_type="react_blocked_by_permission",
                                        details={
                                            "run_id": run.id,
                                            "step_id": iter_step.step_id,
                                            "tool_name": observation.tool_name,
                                            "reason": observation.permission_reason,
                                        },
                                        chat_id=run.chat_id,
                                        agent_id=run.agent_id,
                                    )
                                elif observation.observation_type == "chain_blocked":
                                    ctx.audit_logger.log_security_event(
                                        event_type="react_blocked_by_chain_detector",
                                        details={
                                            "run_id": run.id,
                                            "step_id": iter_step.step_id,
                                            "tool_name": observation.tool_name,
                                        },
                                        chat_id=run.chat_id,
                                        agent_id=run.agent_id,
                                    )
                            except Exception:
                                pass
                        await _emit_react_update(
                            ctx,
                            run,
                            iter_step,
                            event_type="decision_summary",
                            candidate_text=decision.final_response_hint
                            or decision.reason,
                            visible_level="normal",
                            is_important=True,
                        )
                        iter_step.status = "completed"
                        _persist_react_step(ctx, iter_step)
                        return run
                    if decision.action == "finalize":
                        run.status = RunOutcome.DONE
                        run.final_answer = decision.final_response_hint or ""
                        iter_step.status = "completed"
                        if ctx.audit_logger:
                            try:
                                ctx.audit_logger.log_security_event(
                                    event_type="react_finalized",
                                    details={
                                        "run_id": run.id,
                                        "step_id": iter_step.step_id,
                                        "via": "tool_call_iteration",
                                    },
                                    chat_id=run.chat_id,
                                    agent_id=run.agent_id,
                                )
                            except Exception:
                                pass
                        _persist_react_step(ctx, iter_step)
                        return run
            _persist_react_step(ctx, iter_step)

    # Max iterations reached
    run.status = RunOutcome.ABORTED
    run.final_answer = (
        f"抱歉，我在 {MAX_ITERATIONS} 轮对话后仍未能完成任务。"
        "这可能是因为任务过于复杂，或者遇到了重复的工具调用问题。"
        "请尝试简化您的请求，或将任务拆分成更小的步骤。"
    )
    # Phase 10: record terminal max_iteration step.
    abort_step = _open_step(run, action_phase="max_iteration")
    abort_step.observation = {
        "observation_type": "max_iteration",
        "summary": "Reached MAX_ITERATIONS without converging.",
    }
    abort_step.decision = "failed"
    abort_step.status = "failed"
    _persist_react_step(ctx, abort_step)
    return run


async def resume_after_approval(
    run: AgentRun,
    approval: str,
    provider: Provider,
    registry: ToolRegistry,
    permission_gate: Any,
    result_processor: Any,
    ctx: AgentContext,
) -> AgentRun:
    """Resume a suspended run after an approval decision.

    Phase 10 M10.3: opens a *new* ReActStep for the resume action so the
    trace shows two consecutive steps — Step N (suspended approval) →
    Step N+1 (tool_call after approve, OR approval_rejected on deny).
    The iteration counter is monotonic so timelines stay continuous.

    Args:
        approval: One of "approved", "rejected", "expired".
    """
    if not run.pending_tool_call:
        run.status = RunOutcome.ABORTED
        return run

    call_data = json.loads(run.pending_tool_call)
    call_id = call_data["id"]
    call_name = call_data["name"]
    call_args = call_data["arguments"]

    if approval == "approved":
        # Phase 10: open a new tool_call step for the resumed execution.
        resume_step = _open_step(run, action_phase="tool_call")
        resume_step.tool_calls = [{"id": call_id, "name": call_name, "arguments": call_args}]
        resume_step.tool_call_refs = [{"id": call_id, "name": call_name}]
        resume_step.permission_decisions = [
            {"action": "allow_after_approval", "tool": call_name}
        ]

        # Grant session-level permission and execute
        permission_gate.grant_session(_ctx_to_dict(ctx), call_name)
        tool = registry.get(call_name)
        if tool is None:
            result = f"[error] unknown tool: {call_name}"
            run.messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": result,
            })
            resume_step.observation = {
                "observation_type": "tool_error",
                "tool_name": call_name,
                "summary": result,
            }
            resume_step.decision = "failed"
            resume_step.status = "failed"
            run.tool_call_history.append((call_name, False))
        else:
            tool_ctx = _build_tool_context(ctx)
            success = True
            try:
                result = await tool.handler(**call_args, ctx=tool_ctx)
            except TypeError as exc:
                success = False
                result = f"[error] tool {call_name} rejected arguments: {exc}"
            except Exception as exc:
                success = False
                if result_processor:
                    result = result_processor.process_error(exc)
                else:
                    result = f"[error] {type(exc).__name__}: {exc}"
            else:
                if result_processor:
                    result = result_processor.process(result, call_name)
            run.tool_call_history.append((call_name, success))
            run.messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": result,
            })
            obs_type = "tool_success" if success else "tool_error"
            resume_step.observation = {
                "observation_type": obs_type,
                "tool_name": call_name,
                "summary": (result or "")[:400],
            }
            resume_step.decision = "continue" if success else "failed"
            resume_step.status = "observed" if success else "failed"
        _persist_react_step(ctx, resume_step)
    else:
        # Rejected or expired — open an approval_rejected step.
        reject_step = _open_step(run, action_phase="approval_rejected")
        reject_step.tool_calls = [{"id": call_id, "name": call_name, "arguments": call_args}]
        reject_step.observation = {
            "observation_type": "approval_rejected",
            "tool_name": call_name,
            "summary": f"User rejected approval for {call_name}",
            "permission_action": "rejected",
        }
        reject_step.reflection = {
            "decision": "blocked",
            "safety_assessment": "blocked_by_user_rejection",
            "fallback_used": True,
        }
        reject_step.decision = "blocked"
        reject_step.status = "completed"
        run.messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": f"[denied] approval {approval}",
        })
        _persist_react_step(ctx, reject_step)

        if ctx.audit_logger:
            try:
                ctx.audit_logger.log_security_event(
                    event_type="react_blocked_by_approval_reject",
                    details={
                        "run_id": run.id,
                        "step_id": reject_step.step_id,
                        "tool": call_name,
                        "approval": approval,
                    },
                    chat_id=run.chat_id,
                    agent_id=run.agent_id,
                )
            except Exception:
                pass

    # Clear pending state
    run.pending_approval_id = None
    run.pending_tool_call = None

    # Continue the loop
    return await run_agent_step(
        run, provider, registry, permission_gate, result_processor, ctx
    )
