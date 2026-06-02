"""Agent loop: core execution engine driving LLM conversations with tool calling."""

from __future__ import annotations

import asyncio
import hashlib
import json
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


def _call_signature(call_name: str, call_args: dict[str, Any]) -> str:
    """Compute an MD5 signature for duplicate call detection."""
    raw = json.dumps({"name": call_name, "args": call_args}, sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()


def _build_tool_context(ctx: AgentContext) -> ToolContext:
    """Create a ToolContext from an AgentContext."""
    return ToolContext(
        workspace_dir=ctx.workspace_dir,
        chat_id=ctx.chat_id,
        agent_id=ctx.agent_id,
        timeout=ctx.timeout,
        sandbox_mode=ctx.sandbox_mode,
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

    if decision.action == "deny":
        return {
            "role": "tool",
            "tool_call_id": tc.id,
            "content": "[denied] permission denied for this action",
        }

    if decision.action == "need_approval":
        # Cannot suspend during parallel processing, treat as deny
        return {
            "role": "tool",
            "tool_call_id": tc.id,
            "content": "[error] approval required but not supported in parallel mode",
        }

    # decision.action == "allow"
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

    return {
        "role": "tool",
        "tool_call_id": tc.id,
        "content": result,
    }


async def _process_tool_calls_parallel(
    calls: list[tuple[int, Any]],
    run: AgentRun,
    registry: ToolRegistry,
    permission_gate: Any,
    result_processor: Any,
    tool_ctx: ToolContext,
    ctx: AgentContext,
) -> list[tuple[int, Any, dict[str, Any]]]:
    """Process multiple tool calls in parallel and return results."""
    tasks = [
        _process_single_tool_call(tc, run, registry, permission_gate, result_processor, tool_ctx, ctx)
        for idx, tc in calls
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    output = []
    for (idx, tc), result in zip(calls, results):
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
            messages=run.messages,
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

        # Group tool calls: parallel-safe (L0) vs sequential
        parallel_calls: list[tuple[int, Any]] = []  # (index, tool_call)
        sequential_calls: list[tuple[int, Any]] = []

        for idx, tc in enumerate(response.tool_calls):
            tool = registry.get(tc.name)
            # L0 tools are read-only and safe to parallelize
            if tool and tool.permission_level == "L0":
                parallel_calls.append((idx, tc))
            else:
                sequential_calls.append((idx, tc))

        # Process parallel calls first
        if parallel_calls:
            results = await _process_tool_calls_parallel(
                parallel_calls, run, registry, permission_gate, result_processor, tool_ctx, ctx
            )
            for idx, tc, result_msg in results:
                run.messages.append(result_msg)

        # Process sequential calls
        for idx, tc in sequential_calls:
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

            # Permission check
            tool = registry.get(tc.name)
            if tool is None:
                run.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": f"[error] unknown tool: {tc.name}",
                })
                continue

            decision = permission_gate.evaluate(
                tool=tc.name, args=tc.arguments, ctx=_ctx_to_dict(ctx)
            )

            if decision.action == "deny":
                run.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": "[denied] permission denied for this action",
                })
                continue

            if decision.action == "need_approval":
                approval_id = str(uuid.uuid4())
                run.pending_approval_id = approval_id
                run.pending_tool_call = json.dumps({
                    "id": tc.id,
                    "name": tc.name,
                    "arguments": tc.arguments,
                })
                run.status = RunOutcome.SUSPENDED
                return run

            # decision.action == "allow"
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
