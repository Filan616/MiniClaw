"""Workspace management: agent routing and configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mini_claw.config import AgentConfig


@dataclass(slots=True)
class AgentWorkspace:
    """Configuration for a single agent workspace."""

    agent_id: str
    system_prompt: str
    allowed_tools: list[str] = field(default_factory=list)
    route_chat_ids: list[str] = field(default_factory=list)


class WorkspaceManager:
    """Manages agent workspaces and routes chats to the correct agent."""

    def __init__(self) -> None:
        self._workspaces: dict[str, AgentWorkspace] = {}
        self._chat_routes: dict[str, str] = {}

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
            self._workspaces[ws.agent_id] = ws

            # Build reverse lookup: chat_id -> agent_id
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
            # If "default" doesn't exist either, return first available
            if self._workspaces:
                return next(iter(self._workspaces.values()))
            raise ValueError(
                f"No workspace found for chat_id={chat_id!r} "
                "and no default workspace configured"
            )
        return ws
