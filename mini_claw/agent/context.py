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
    sandbox_mode: str = "safe"
    audit_logger: Any = None  # SecurityAuditLogger instance
    chain_detector: Any = None  # ChainDetector instance
    system_prompt: str | None = None
    skill_manager: Any = None
    storage: Any = None  # Database for stats persistence (Phase B.4)
