"""Channel abstraction for MiniClaw messaging."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, ClassVar, Coroutine


@dataclass
class InboundMessage:
    """A message received from a channel."""

    chat_id: str
    text: str
    event_id: str
    channel_name: str = "feishu"
    sender_id: str | None = None
    thread_id: str | None = None
    timestamp: int = 0


class Channel(ABC):
    """Abstract base class for messaging channels."""

    channel_type: ClassVar[str] = "unknown"

    def __init__(self, name: str | None = None) -> None:
        self.name = name or self.channel_type
        self.on_message: Callable[
            [InboundMessage], Coroutine[Any, Any, None]
        ] | None = None
        self.on_card_action: Callable[
            [dict], Coroutine[Any, Any, None]
        ] | None = None

    async def start(self) -> None:
        """Start the channel."""
        pass

    async def stop(self) -> None:
        """Stop the channel."""
        pass

    async def _dispatch_message(self, msg: InboundMessage) -> None:
        if self.on_message is not None:
            await self.on_message(msg)

    async def _dispatch_card(self, payload: dict) -> None:
        if self.on_card_action is not None:
            await self.on_card_action(payload)

    @abstractmethod
    async def send(self, chat_id: str, text: str) -> None:
        """Send a text message to a chat."""
        ...

    @abstractmethod
    async def send_approval_card(
        self,
        chat_id: str,
        approval_id: str,
        tool_name: str,
        tool_args: dict,
        level: str,
    ) -> None:
        """Send an interactive approval card with approve/reject buttons."""
        ...

    async def send_stream_chunk(self, chat_id: str, delta: str) -> None:
        """Send a streaming text chunk (optional, default no-op)."""
        pass
