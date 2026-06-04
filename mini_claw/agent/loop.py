"""Agent loop: core execution engine driving LLM conversations with tool calling."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from mini_claw.agent.context import AgentContext
from mini_claw.providers.base import Provider
from mini_claw.tools.registry import ToolContext, ToolRegistry


MAX_ITERATIONS = 10


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


def _messages_for_provider(run: AgentRun, ctx: AgentContext) -> list[dict[str, Any]]:
    """Build provider messages with base and skill system prompt context."""
    prompt_parts: list[str] = []
    if ctx.system_prompt:
        prompt_parts.append(ctx.system_prompt.strip())

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

        response = await provider.chat(
            messages=_messages_for_provider(run, ctx),
            tools=tool_schemas if tool_schemas else None,
            stream=True,
            stream_callback=stream_callback,
        )

        # No tool calls -> conversation complete
        if not response.tool_calls or response.finish_reason != "tool_calls":
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
