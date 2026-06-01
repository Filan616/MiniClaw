"""Ollama local LLM provider using OpenAI-compatible API."""

from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from .base import LLMResponse, Provider, ToolCall


class OllamaProvider(Provider):
    """Ollama local provider via OpenAI-compatible endpoint."""

    def __init__(
        self,
        model: str = "qwen2.5",
        base_url: str | None = None,
        api_key: str = "ollama",
    ):
        self.model = model
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or "http://localhost:11434/v1",
        )

    def format_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Wrap tool defs in OpenAI function-calling format."""
        return [{"type": "function", "function": t} for t in tools]

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = self.format_tools(tools)

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
