"""Application factory for Mini-Claw."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from mini_claw.config import AppConfig, DEFAULT_CONFIG_DIR

logger = logging.getLogger(__name__)


def create_components(config: AppConfig) -> dict[str, Any]:
    """Create and wire all application components.

    Returns a dict with keys: provider, registry, permission_gate,
    storage, gateway, skills, feishu_channel.
    """
    from mini_claw.permissions.gate import PermissionGate
    from mini_claw.permissions.policy import PermissionPolicy
    from mini_claw.providers import get_provider
    from mini_claw.skills._loader import load_skills, register_skill_tools
    from mini_claw.storage import Database
    from mini_claw.tools.builtin import BUILTIN_TOOLS
    from mini_claw.tools.registry import ToolRegistry

    # Storage
    db_path = DEFAULT_CONFIG_DIR / "mini_claw.db"
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

    return {
        "provider": provider,
        "registry": registry,
        "permission_gate": permission_gate,
        "storage": storage,
        "skills": skills,
        "config": config,
    }


def create_app(config: AppConfig) -> FastAPI:
    """Factory function: assemble the full FastAPI application.

    This wires up all components — provider, tools, permissions, skills,
    gateway, scheduler, and Feishu webhook — and returns a ready-to-run
    FastAPI instance.
    """
    from mini_claw.channels.feishu import FeishuChannel

    components = create_components(config)
    storage = components["storage"]
    provider = components["provider"]
    registry = components["registry"]
    permission_gate = components["permission_gate"]

    app = FastAPI(
        title="Mini-Claw",
        description="本地优先的个人 AI Agent 助手",
        version="0.1.0",
    )

    # Health check
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # Feishu webhook
    if config.channels_feishu.enabled:
        feishu = FeishuChannel(
            app_id=config.channels_feishu.app_id,
            app_secret=config.channels_feishu.app_secret,
            verification_token=config.channels_feishu.verification_token,
            encrypt_key=config.channels_feishu.encrypt_key,
        )
        webhook_router = feishu.create_webhook_router()
        app.include_router(webhook_router)
        logger.info("Feishu webhook 已挂载")

    # Store components on app state for access in routes
    app.state.components = components

    logger.info(
        "Mini-Claw 应用已创建 (tools=%d, skills=%d)",
        len(registry.list_tools()),
        len(components["skills"]),
    )
    return app
