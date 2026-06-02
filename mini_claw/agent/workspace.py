"""Workspace management: agent routing and configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from mini_claw.config import AgentConfig


@dataclass(slots=True)
class AgentWorkspace:
    """Configuration for a single agent workspace."""

    agent_id: str
    system_prompt: str
    allowed_tools: list[str] = field(default_factory=list)
    route_chat_ids: list[str] = field(default_factory=list)
    workspace_dir: Optional[Path] = None


class WorkspaceManager:
    """Manages agent workspaces and routes chats to the correct agent."""

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self._workspaces: dict[str, AgentWorkspace] = {}
        self._chat_routes: dict[str, str] = {}
        self._base_dir: Optional[Path] = base_dir

    def load_workspaces(
        self, agents_config: list[AgentConfig]
    ) -> dict[str, AgentWorkspace]:
        """Load workspaces from agent configuration list.

        Returns the dict of agent_id -> AgentWorkspace.
        """
        self._workspaces.clear()
        self._chat_routes.clear()

        for agent_cfg in agents_config:
            ws = AgentWorkspace(
                agent_id=agent_cfg.id,
                system_prompt=agent_cfg.system_prompt,
                allowed_tools=list(agent_cfg.tools),
                route_chat_ids=list(agent_cfg.route_chat_ids),
            )
            if self._base_dir is not None:
                ws.workspace_dir = self._base_dir / (agent_cfg.workspace or agent_cfg.id)
                ws.workspace_dir.mkdir(parents=True, exist_ok=True)
            self._workspaces[ws.agent_id] = ws

            for chat_id in ws.route_chat_ids:
                self._chat_routes[chat_id] = ws.agent_id

        return dict(self._workspaces)

    def get_workspace_for_chat(self, chat_id: str) -> AgentWorkspace:
        """Route a chat_id to its workspace.

        Falls back to the "default" workspace if no explicit route exists.
        """
        agent_id = self._chat_routes.get(chat_id, "default")
        ws = self._workspaces.get(agent_id)
        if ws is None:
            if self._workspaces:
                return next(iter(self._workspaces.values()))
            raise ValueError(
                f"No workspace found for chat_id={chat_id!r} "
                "and no default workspace configured"
            )
        return ws

    def get_workspace(self, chat_id: str, agent_id: str | None = None) -> Path:
        """Resolve the filesystem workspace directory for a chat.

        Gateway calls this to populate ``AgentContext.workspace_dir``. The
        ``agent_id`` argument is accepted for caller convenience; the actual
        routing is done by ``chat_id``.
        """
        if agent_id is not None:
            ws = self._workspaces.get(agent_id)
            if ws is None:
                raise ValueError(f"No workspace found for agent_id={agent_id!r}")
        else:
            ws = self.get_workspace_for_chat(chat_id)
        if ws.workspace_dir is None:
            raise RuntimeError(
                "WorkspaceManager has no base_dir configured; "
                "call WorkspaceManager(base_dir=...) before load_workspaces()."
            )
        return ws.workspace_dir
