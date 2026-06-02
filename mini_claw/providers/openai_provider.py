"""OpenAI LLM provider."""

from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from .base import LLMResponse, Provider, ToolCall


class OpenAIProvider(Provider):
    """OpenAI API provider."""

    def __init__(self, api_key: str, model: str = "gpt-4o", base_url: str | None = None):
        self.model = model
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
        )

    def format_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Wrap tool defs in OpenAI function-calling format."""
        return [{"type": "function", "function": t} for t in tools]

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        stream_callback: Any = None,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
        }
        if tools:
            kwargs["tools"] = self.format_tools(tools)

        if stream and stream_callback:
            # Streaming path
            resp = await self.client.chat.completions.create(**kwargs)
            collected_text = ""
            tool_calls: list[ToolCall] = []
            finish_reason = None

            async for chunk in resp:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta

                if delta.content:
                    collected_text += delta.content
                    stream_callback(delta.content)

                if choice.finish_reason:
                    finish_reason = choice.finish_reason

                # Tool calls in streaming (accumulate)
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        # Simplified: assume tool_calls come complete in one chunk
                        if tc_delta.function and tc_delta.function.name:
                            tool_calls.append(
                                ToolCall(
                                    id=tc_delta.id or "",
                                    name=tc_delta.function.name,
                                    arguments=json.loads(tc_delta.function.arguments or "{}"),
                                )
                            )

            return LLMResponse(
                text=collected_text or None,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                raw={},
            )
        else:
            # Non-streaming path
            resp = await self.client.chat.completions.create(**kwargs)
            choice = resp.choices[0]
            msg = choice.message

            tool_calls: list[ToolCall] = []
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_calls.append(
                        ToolCall(
                            id=tc.id,
                            name=tc.function.name,
                            arguments=json.loads(tc.function.arguments),
                        )
                    )

            return LLMResponse(
                text=msg.content,
                tool_calls=tool_calls,
                finish_reason=choice.finish_reason,
                raw=resp.model_dump(),
            )
