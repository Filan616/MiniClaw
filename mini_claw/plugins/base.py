"""Plugin protocol for MiniClaw.

Phase 4 only provides the skeleton. Plugins are local-only, disabled by
default, and must pass conservative static checks before loading.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class PluginContext:
    manifest: dict[str, Any]
    declared_permissions: list[str]
    workspace_dir: Path
    storage: Any = None  # read-only by convention in Phase 4


@runtime_checkable
class PluginProtocol(Protocol):
    def register_tools(self, registry: Any, ctx: PluginContext) -> None:
        ...

    def register_channels(self, channel_manager: Any, ctx: PluginContext) -> None:
        ...

    def register_providers(self, provider_manager: Any, ctx: PluginContext) -> None:
        ...

    def register_hooks(self, hook_manager: Any, ctx: PluginContext) -> None:
        ...
