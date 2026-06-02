"""Example local plugin that registers an L0 echo tool."""

from __future__ import annotations

from mini_claw.tools.registry import Tool


async def _echo_handler(ctx, text: str) -> str:
    return str(text)


def register_tools(registry, ctx) -> None:
    registry.register(
        Tool(
            name="echo",
            description="Echo text back to the caller.",
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to echo"}
                },
                "required": ["text"],
            },
            handler=_echo_handler,
            permission_level="L0",
        )
    )
