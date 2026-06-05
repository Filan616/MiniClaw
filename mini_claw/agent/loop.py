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


MAX_ITERATIONS = 10
logger = logging.getLogger(__name__)


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

    # Compose system prompt: agent system prompt + skills first, then RAG blocks.
    parts = list(prompt_parts) + rag_blocks
    system_message = {"role": "system", "content": "\n\n".join(parts)}
    if base_messages and base_messages[0].get("role") == "system":
        return [system_message, *base_messages[1:]]
    return [system_message, *base_messages]


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
            run.status = RunOutcome.DONE
            run.final_answer = response.text
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

        # Phase 9.7: Send prelude if this is the first tool call with content
        if response.tool_calls and response.text and not run.prelude_sent and ctx.on_prelude:
            # Create audit callback
            def audit_rejected(event_type: str, details: dict) -> None:
                if ctx.audit_logger:
                    ctx.audit_logger.log_security_event(
                        event_type=event_type,
                        details=details,
                        chat_id=ctx.chat_id,
                        agent_id=ctx.agent_id,
                    )

            # Sanitize with audit
            sanitized = _sanitize_prelude(
                response.text,
                max_length=ctx.prelude_max_length,
                audit_callback=audit_rejected,
            )

            if sanitized:
                try:
                    await ctx.on_prelude(sanitized)
                    run.prelude_sent = True
                except Exception:
                    logger.warning(
                        "Failed to send prelude, continuing with tool execution",
                        exc_info=True,
                        extra={"chat_id": ctx.chat_id, "run_id": run.id},
                    )

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
                continue

            if decision.action == "need_approval":
                approval_id = str(uuid.uuid4())
                run.pending_approval_id = approval_id
                run.pending_tool_call = json.dumps({
                    "id": tc.id,
                    "name": tc.name,
                    "arguments": tc.arguments,
                    "level": decision.permission_level,  # Phase 0.3: for approval card
                })
                run.status = RunOutcome.SUSPENDED
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
                        continue

            try:
                result = await tool.handler(**tc.arguments, ctx=tool_ctx)
            except TypeError as exc:
                result = f"[error] tool {tc.name} rejected arguments: {exc}"
            except Exception as exc:
                if result_processor:
                    result = result_processor.process_error(exc)
                else:
                    result = f"[error] {type(exc).__name__}: {exc}"
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

    # Max iterations reached
    run.status = RunOutcome.ABORTED
    run.final_answer = (
        f"抱歉，我在 {MAX_ITERATIONS} 轮对话后仍未能完成任务。"
        "这可能是因为任务过于复杂，或者遇到了重复的工具调用问题。"
        "请尝试简化您的请求，或将任务拆分成更小的步骤。"
    )
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
        # Grant session-level permission and execute
        permission_gate.grant_session(_ctx_to_dict(ctx), call_name)
        tool = registry.get(call_name)
        if tool is None:
            run.messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": f"[error] unknown tool: {call_name}",
            })
        else:
            tool_ctx = _build_tool_context(ctx)
            try:
                result = await tool.handler(**call_args, ctx=tool_ctx)
            except TypeError as exc:
                result = f"[error] tool {call_name} rejected arguments: {exc}"
            except Exception as exc:
                if result_processor:
                    result = result_processor.process_error(exc)
                else:
                    result = f"[error] {type(exc).__name__}: {exc}"
            else:
                if result_processor:
                    result = result_processor.process(result, call_name)
            run.messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": result,
            })
    else:
        # Rejected or expired
        run.messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": f"[denied] approval {approval}",
        })

    # Clear pending state
    run.pending_approval_id = None
    run.pending_tool_call = None

    # Continue the loop
    return await run_agent_step(
        run, provider, registry, permission_gate, result_processor, ctx
    )
