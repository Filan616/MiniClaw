"""Simple internal event bus for pub/sub within the gateway."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

# Type alias for async event handlers
EventHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class EventBus:
    """Lightweight async event bus for internal component communication.

    Supports subscribing handlers to event types and emitting events
    that are dispatched to all registered handlers concurrently.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """Register a handler for a given event type.

        Args:
            event_type: String identifier for the event category.
            handler: Async callable that receives the event data dict.
        """
        self._handlers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        """Remove a previously registered handler."""
        handlers = self._handlers.get(event_type)
        if handlers and handler in handlers:
            handlers.remove(handler)

    async def emit(self, event_type: str, data: dict[str, Any]) -> None:
        """Emit an event, dispatching to all subscribed handlers.

        Handlers are invoked concurrently via asyncio.gather.
        Exceptions in individual handlers are logged but do not
        prevent other handlers from executing.
        """
        handlers = self._handlers.get(event_type, [])
        if not handlers:
            return

        tasks = [self._safe_call(h, event_type, data) for h in handlers]
        await asyncio.gather(*tasks)

    async def _safe_call(
        self,
        handler: EventHandler,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        """Call a handler with error isolation."""
        try:
            await handler(data)
        except Exception as exc:
            logger.exception(
                "Event handler %s failed for event '%s': %s",
                handler.__name__,
                event_type,
                exc,
            )
