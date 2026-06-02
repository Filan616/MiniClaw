from pathlib import Path

import pytest

from mini_claw.agent.manager import AgentManager
from mini_claw.agent.workspace import WorkspaceManager
from mini_claw.config import AgentConfig, AppConfig
from mini_claw.storage.db import Database
from mini_claw.config import load_config


def _manager(tmp_path: Path, config: AppConfig) -> AgentManager:
    db = Database(tmp_path / "agents.db")
    workspace_manager = WorkspaceManager(tmp_path / "workspaces")
    workspace_manager.load_workspaces(config.agents)
    return AgentManager(db, config, workspace_manager)


def test_config_agents_are_persisted(tmp_path: Path):
    cfg = AppConfig(agents=[AgentConfig(id="default"), AgentConfig(id="ops")])
    manager = _manager(tmp_path, cfg)

    assert [agent.id for agent in manager.list_agents()] == ["default", "ops"]


def test_runtime_agent_conflicts_with_config_agent(tmp_path: Path):
    db = Database(tmp_path / "agents.db")
    db.execute(
        "INSERT INTO agents (id, name, config_json, source, enabled, created_at, updated_at) "
        "VALUES (?, ?, ?, 'runtime', 1, 1, 1)",
        ("ops", "ops", AgentConfig(id="ops").model_dump_json()),
    )
    workspace_manager = WorkspaceManager(tmp_path / "workspaces")
    cfg = AppConfig(agents=[AgentConfig(id="ops")])

    with pytest.raises(RuntimeError, match="runtime agent"):
        AgentManager(db, cfg, workspace_manager)


def test_channel_binding_wins_over_route_chat_ids(tmp_path: Path):
    cfg = AppConfig(
        agents=[
            AgentConfig(id="default", route_chat_ids=["chat1"]),
            AgentConfig(id="ops"),
        ]
    )
    manager = _manager(tmp_path, cfg)
    assert manager.resolve_for_chat("feishu", "chat1").id == "default"

    manager.bind_chat("feishu", "chat1", "ops")
    assert manager.resolve_for_chat("feishu", "chat1").id == "ops"


def test_remove_config_agent_is_rejected(tmp_path: Path):
    manager = _manager(tmp_path, AppConfig(agents=[AgentConfig(id="default")]))

    with pytest.raises(ValueError, match="Config-backed"):
        manager.remove_agent("default")


def test_agents_defaults_merge_into_agents(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
agents_defaults:
  tools: [read_file]
  model: base-model
agents:
  - id: default
  - id: ops
    model: ops-model
""",
        encoding="utf-8",
    )

    cfg = load_config(config_path)
    assert cfg.agents[0].tools == ["read_file"]
    assert cfg.agents[0].model == "base-model"
    assert cfg.agents[1].model == "ops-model"
