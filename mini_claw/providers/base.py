"""Abstract base classes for LLM providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable


@dataclass
class ToolCall:
    """Represents a single tool/function call from the LLM."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """Unified response from any LLM provider."""

    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class StreamChunk:
    """A single chunk from a streaming response."""

    delta: str  # Incremental text
    finish_reason: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


class Provider(ABC):
    """Abstract base for all LLM providers."""

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        stream_callback: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        """Send a chat completion request and return a unified response.

        Args:
            messages: Conversation history.
            tools: Tool schemas.
            stream: If True, enable streaming (provider-specific behavior).
            stream_callback: Called with each text delta when streaming.
        """

    @abstractmethod
    def format_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Format tool definitions into the provider's expected schema."""
