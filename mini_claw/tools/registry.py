"""Tool registry: dataclasses and registry for managing agent tools."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine


@dataclass(frozen=True, slots=True)
class Tool:
    """Descriptor for a single tool available to the agent."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Coroutine[Any, Any, str]]
    permission_level: str = "L0"  # L0 (safe) -> L4 (dangerous)

    def __post_init__(self) -> None:
        if self.permission_level not in ("L0", "L1", "L2", "L3", "L4"):
            raise ValueError(
                f"Invalid permission_level '{self.permission_level}', "
                "must be one of L0-L4"
            )


@dataclass(slots=True)
class ToolContext:
    """Runtime context passed to tool handlers."""

    workspace_dir: Path
    chat_id: str = ""
    agent_id: str = ""
    timeout: int = 30


class ToolRegistry:
    """Central registry that holds all available tools."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool. Raises ValueError on duplicate names."""
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        """Retrieve a tool by name, or None if not found."""
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        """Return sorted list of all registered tool names."""
        return sorted(self._tools.keys())

    def schemas_for(self, allowed: list[str]) -> list[dict[str, Any]]:
        """Return JSON-Schema-style tool definitions for the allowed tools.

        Each entry follows the OpenAI function-calling format:
        {"type": "function", "function": {"name", "description", "parameters"}}
        """
        results: list[dict[str, Any]] = []
        for name in allowed:
            tool = self._tools.get(name)
            if tool is None:
                continue
            results.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
            )
        return results
