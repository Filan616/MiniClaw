"""Tools module: registry, built-in tools, and result processing."""

from .registry import Tool, ToolContext, ToolRegistry
from .result_processor import ToolResultProcessor


def create_default_registry() -> ToolRegistry:
    """Create a ToolRegistry pre-loaded with all built-in and web tools."""
    from .builtin import BUILTIN_TOOLS
    from .web import WEB_TOOLS

    registry = ToolRegistry()
    for tool in BUILTIN_TOOLS:
        registry.register(tool)
    for tool in WEB_TOOLS:
        registry.register(tool)
    return registry


__all__ = [
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "ToolResultProcessor",
    "create_default_registry",
]
