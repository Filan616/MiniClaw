"""Channel abstraction for MiniClaw messaging."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class InboundMessage:
    """A message received from a channel."""

    chat_id: str
    sender_id: str
    text: str
    event_id: str
    timestamp: int


class Channel(ABC):
    """Abstract base class for messaging channels."""

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
