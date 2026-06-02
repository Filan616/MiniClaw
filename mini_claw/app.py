"""Application factory for Mini-Claw."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from mini_claw.config import AppConfig, get_data_dir

logger = logging.getLogger(__name__)


def create_components(
    config: AppConfig, config_path: Path | None = None
) -> dict[str, Any]:
    """Create and wire all application components.

    Returns a dict with keys: provider, registry, permission_gate,
    storage, skills, config, workspace_manager, result_processor, gateway.
    """
    from mini_claw.agent.workspace import WorkspaceManager
    from mini_claw.gateway.router import Gateway
    from mini_claw.permissions.gate import PermissionGate
    from mini_claw.permissions.policy import PermissionPolicy
    from mini_claw.providers import get_provider
    from mini_claw.skills._loader import load_skills, register_skill_tools
    from mini_claw.storage import Database
    from mini_claw.tools.builtin import BUILTIN_TOOLS
    from mini_claw.tools.registry import ToolRegistry
    from mini_claw.tools.result_processor import ToolResultProcessor

    # Storage — co-located with config file
    data_dir = get_data_dir(config_path)
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "mini_claw.db"
    storage = Database(db_path)

    # Provider
    provider = get_provider(config.provider)

    # Tool registry
    registry = ToolRegistry()
    for tool in BUILTIN_TOOLS:
        registry.register(tool)

    # Skills
    skills_dir = Path.cwd() / "skills"
    skills = load_skills(skills_dir)
    register_skill_tools(registry, skills)

    # Permissions
    policy = PermissionPolicy(config.permissions)
    permission_gate = PermissionGate(policy, storage)

    # Workspaces — one filesystem dir per agent, co-located with the db
    workspace_manager = WorkspaceManager(base_dir=data_dir / "workspaces")
    workspace_manager.load_workspaces(config.agents)

    # Result processor for tool outputs
    result_processor = ToolResultProcessor()

    # Gateway — central message orchestrator
    gateway = Gateway(
        config=config,
        storage=storage,
        provider=provider,
        registry=registry,
        permission_gate=permission_gate,
        result_processor=result_processor,
        workspace_manager=workspace_manager,
    )

    return {
        "provider": provider,
        "registry": registry,
        "permission_gate": permission_gate,
        "storage": storage,
        "skills": skills,
        "config": config,
        "workspace_manager": workspace_manager,
        "result_processor": result_processor,
        "gateway": gateway,
    }


def create_app(config: AppConfig, config_path: Path | None = None) -> FastAPI:
    """Factory function: assemble the full FastAPI application.

    Wires up all components and starts the Feishu long-connection client
    in a background thread when enabled, with on_message / on_card_action
    routed to the Gateway.
    """
    from mini_claw.channels.feishu import FeishuChannel

    components = create_components(config, config_path=config_path)
    gateway = components["gateway"]
    registry = components["registry"]

    feishu: FeishuChannel | None = None
    if config.channels_feishu.enabled:
        feishu = FeishuChannel(
            app_id=config.channels_feishu.app_id,
            app_secret=config.channels_feishu.app_secret,
        )
        # Wire the channel into the gateway and the gateway into the channel.
        gateway.set_channel(feishu)
        feishu.on_message = gateway.handle_message
        feishu.on_card_action = gateway.handle_card_action
        components["feishu_channel"] = feishu

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if feishu is not None:
            await feishu.start()
        try:
            yield
        finally:
            if feishu is not None:
                await feishu.stop()

    app = FastAPI(
        title="Mini-Claw",
        description="本地优先的个人 AI Agent 助手",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Health check
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # Store components on app state for access in routes
    app.state.components = components

    logger.info(
        "Mini-Claw 应用已创建 (tools=%d, skills=%d, feishu=%s)",
        len(registry.list_tools()),
        len(components["skills"]),
        "on" if feishu is not None else "off",
    )
    return app
