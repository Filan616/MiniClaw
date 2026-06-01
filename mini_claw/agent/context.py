"""Agent execution context passed through the agent loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class AgentContext:
    """Runtime context for a single agent execution."""

    chat_id: str
    agent_id: str
    workspace_dir: Path
    channel: Any = None
    timeout: int = 30
