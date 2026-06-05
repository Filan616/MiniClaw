"""Agent execution context passed through the agent loop."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
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
    rag_manager: Any = None  # Phase 8 M2: RagManager instance
    session_id: str | None = None  # Phase 9 P0.1: stable session id (sha1 of channel|chat|thread|agent)
    channel_name: str | None = None  # Phase 9 P0.1: channel of this run (feishu / cli / ...)
    chat_search_manager: Any = None  # Phase 9 M9.1: ChatSearchManager instance
    on_prelude: Callable[[str], Awaitable[None]] | None = None  # Phase 9.7: Progressive response callback
    prelude_max_length: int = 120  # Phase 9.7: Max prelude length before truncation
    on_progress: Callable[[str], Awaitable[None]] | None = None  # Phase 9.8: Progress update callback
