"""Agent execution context passed through the agent loop."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


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
    on_prelude: Callable[[str], Awaitable[None]] | None = None  # Phase 9.7 (legacy): kept as bridge for older callers
    prelude_max_length: int = 120  # Phase 9.7 (legacy)
    on_progress: Callable[[str], Awaitable[None]] | None = None  # Phase 9.8: Progress update callback
    # Phase 10 M10.1: ReActUserUpdate pipeline.
    on_react_update: Callable[[Any], Awaitable[bool]] | None = None
    react_user_updates_enabled: bool = True
    react_user_update_mode: Literal["silent", "normal", "verbose", "debug"] = "normal"
    react_user_update_max_chars: int = 160
    # Phase 10 §6 — additional react_user_updates knobs.
    react_user_updates_sanitize_completion_claims: bool = True
    react_user_updates_store_redacted_text: bool = True
    react_user_updates_send_failure_non_blocking: bool = True
    # Phase 10 M10.2: Reflection / Finalizer policy + provider for the
    # secondary "internal" reflection call. The provider is supplied
    # separately so the reflection request can be routed to a
    # cheaper/lighter model when desired; if None, the loop's main
    # provider is reused.
    react_policy: Any = None  # mini_claw.agent.reflection_trigger.ReActPolicy
    reflection_provider: Any = None
    # Phase 10 M10.0: Goal Anchor configuration.
    goal_anchor_enabled: bool = True
    goal_anchor_max_summary_chars: int = 800
    goal_anchor_mark_untrusted: bool = True
    goal_anchor_detect_policy: bool = True
    goal_anchor_inject_every_iteration: bool = True
    goal_anchor_summarization_mode: Literal["truncate"] = "truncate"
