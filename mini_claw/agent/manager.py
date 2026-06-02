"""Agent persistence, routing, and chat binding management."""

from __future__ import annotations

import time
from typing import Any

from mini_claw.config import AgentConfig, AppConfig


class AgentManager:
    """Owns configured/runtime agents and channel chat bindings."""

    def __init__(self, storage: Any, config: AppConfig, workspace_manager: Any) -> None:
        self._storage = storage
        self._config = config
        self._workspace_manager = workspace_manager
        self._sync_config_agents()
        self._workspace_manager.load_workspaces(self.list_agents())

    def _sync_config_agents(self) -> None:
        now = int(time.time())
        for agent_cfg in self._config.agents:
            existing = self._storage.fetchone(
                "SELECT id, source FROM agents WHERE id = ?", (agent_cfg.id,)
            )
            payload = agent_cfg.model_dump_json()
            name = agent_cfg.name or agent_cfg.id
            enabled = 1 if agent_cfg.enabled else 0

            if existing and existing["source"] == "runtime":
                raise RuntimeError(
                    f"Agent id {agent_cfg.id!r} exists as a runtime agent. "
                    f"Remove it first with `mini-claw agents remove {agent_cfg.id}` "
                    "or change the id in config."
                )
            if existing:
                self._storage.execute(
                    "UPDATE agents SET name=?, config_json=?, source='config', "
                    "enabled=?, updated_at=? WHERE id=?",
                    (name, payload, enabled, now, agent_cfg.id),
                )
            else:
                self._storage.execute(
                    "INSERT INTO agents "
                    "(id, name, config_json, source, enabled, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'config', ?, ?, ?)",
                    (agent_cfg.id, name, payload, enabled, now, now),
                )

    def list_agents(self) -> list[AgentConfig]:
        rows = self._storage.fetchall(
            "SELECT config_json FROM agents WHERE enabled=1 ORDER BY created_at, id"
        )
        return [AgentConfig.model_validate_json(row["config_json"]) for row in rows]

    def get_agent(self, agent_id: str) -> AgentConfig:
        row = self._storage.fetchone(
            "SELECT config_json FROM agents WHERE id=? AND enabled=1", (agent_id,)
        )
        if row is None:
            raise KeyError(f"Unknown or disabled agent: {agent_id}")
        return AgentConfig.model_validate_json(row["config_json"])

    def add_agent(self, cfg: AgentConfig) -> AgentConfig:
        existing = self._storage.fetchone("SELECT id FROM agents WHERE id=?", (cfg.id,))
        if existing:
            raise ValueError(f"Agent already exists: {cfg.id}")
        now = int(time.time())
        self._storage.execute(
            "INSERT INTO agents "
            "(id, name, config_json, source, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, 'runtime', ?, ?, ?)",
            (
                cfg.id,
                cfg.name or cfg.id,
                cfg.model_dump_json(),
                1 if cfg.enabled else 0,
                now,
                now,
            ),
        )
        self._workspace_manager.load_workspaces(self.list_agents())
        return cfg

    def remove_agent(self, agent_id: str) -> bool:
        row = self._storage.fetchone(
            "SELECT source FROM agents WHERE id=?", (agent_id,)
        )
        if row is None:
            return False
        if row["source"] == "config":
            raise ValueError("Config-backed agents cannot be removed at runtime")
        self._storage.execute("DELETE FROM channel_bindings WHERE agent_id=?", (agent_id,))
        cur = self._storage.execute("DELETE FROM agents WHERE id=?", (agent_id,))
        self._workspace_manager.load_workspaces(self.list_agents())
        return cur.rowcount > 0

    def bind_chat(self, channel_name: str, chat_id: str, agent_id: str) -> None:
        self.get_agent(agent_id)
        now = int(time.time())
        self._storage.execute(
            "INSERT OR REPLACE INTO channel_bindings "
            "(channel_name, chat_id, agent_id, created_at) VALUES (?, ?, ?, ?)",
            (channel_name, chat_id, agent_id, now),
        )

    def resolve_for_chat(self, channel_name: str, chat_id: str) -> AgentConfig:
        row = self._storage.fetchone(
            "SELECT agent_id FROM channel_bindings "
            "WHERE channel_name=? AND chat_id=?",
            (channel_name, chat_id),
        )
        if row:
            return self.get_agent(row["agent_id"])

        for agent_cfg in self._config.agents:
            if agent_cfg.enabled and chat_id in agent_cfg.route_chat_ids:
                return self.get_agent(agent_cfg.id)

        agents = self.list_agents()
        if agents:
            return agents[0]
        raise RuntimeError("No enabled agents configured")

    def bindings_for(self, agent_id: str) -> list[dict[str, Any]]:
        return self._storage.fetchall(
            "SELECT channel_name, chat_id, created_at FROM channel_bindings "
            "WHERE agent_id=? ORDER BY channel_name, chat_id",
            (agent_id,),
        )
