"""Tool registry: dataclasses and registry for managing agent tools."""

from __future__ import annotations

import threading
from dataclasses import dataclass
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
    """Runtime context passed to tool handlers.

    The audit_logger is injected by Gateway/router to allow tools to log
    security events without directly importing storage layer.
    """

    workspace_dir: Path
    chat_id: str = ""
    agent_id: str = ""
    timeout: int = 30
    sandbox_mode: str = "safe"  # "safe" or "bypass"
    audit_logger: Any = None  # SecurityAuditLogger instance (TYPE_CHECKING avoided for simplicity)
    chain_detector: Any = None  # ChainDetector instance


class ToolRegistry:
    """Central registry that holds all available tools.

    Concurrency / hot-removal semantics (Phase B.5):
    - register/unregister are protected by a threading.Lock for safety.
    - Each register/unregister increments _version (allows cache invalidation).
    - Run-in-progress safety: ``run_agent_step`` calls ``schemas_for`` ONCE at
      the start of the run and stores the result locally. The LLM sees a
      snapshot of tools as they existed when the run began. Tool handlers are
      looked up via ``registry.get(name)`` at call time; if a tool is
      unregistered mid-run, the next call to it returns "unknown tool" but any
      tool currently executing (already holding a handler reference) finishes
      normally. This is "partial snapshot mode" — schemas are snapshotted,
      handler resolution is live.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._lock = threading.Lock()
        self._version: int = 0

    def register(self, tool: Tool) -> None:
        """Register a tool. Raises ValueError on duplicate names."""
        with self._lock:
            if tool.name in self._tools:
                raise ValueError(f"Tool '{tool.name}' is already registered")
            self._tools[tool.name] = tool
            self._version += 1

    def unregister(self, name: str) -> bool:
        """Remove a tool from the registry. Returns True if removed.

        Hot-removal safety: see class docstring. New runs will not see the
        tool; runs in-flight that already have a handler reference complete
        their current call without disruption.
        """
        with self._lock:
            if name in self._tools:
                del self._tools[name]
                self._version += 1
                return True
            return False

    @property
    def version(self) -> int:
        """Increments on every register/unregister. Use to invalidate caches."""
        return self._version

    def get(self, name: str) -> Tool | None:
        """Retrieve a tool by name, or None if not found."""
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        """Return sorted list of all registered tool names."""
        return sorted(self._tools.keys())

    def schemas_for(self, allowed: list[str]) -> list[dict[str, Any]]:
        """Return raw tool specs for the allowed tools.

        Each entry is provider-agnostic:
            {"name": ..., "description": ..., "parameters": ...}

        It is the provider's job (``Provider.format_tools``) to wrap these
        into its wire format (e.g. OpenAI's
        ``{"type": "function", "function": {...}}``).
        """
        results: list[dict[str, Any]] = []
        for name in allowed:
            tool = self._tools.get(name)
            if tool is None:
                continue
            results.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                }
            )
        return results
