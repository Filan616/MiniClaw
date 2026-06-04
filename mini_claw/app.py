"""Application factory for Mini-Claw."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from mini_claw.config import AppConfig, get_data_dir

logger = logging.getLogger(__name__)


def _recover_stale_events(storage: Any) -> None:
    """Recover stale processing events on service startup.

    Events stuck in 'processing' with heartbeat older than 5 minutes are
    marked as 'failed'. This only runs once at startup, not during normal
    handle_message flow to avoid preempting long-running tasks.
    """
    stale_threshold = int(time.time()) - 300  # 5 minutes
    stale = storage.fetchall(
        "SELECT event_id FROM processed_events "
        "WHERE status='processing' AND heartbeat_at < ?",
        (stale_threshold,),
    )
    for row in stale:
        storage.execute(
            "UPDATE processed_events "
            "SET status='failed', finished_at=?, error='service restarted, marked as failed' "
            "WHERE event_id=?",
            (int(time.time()), row["event_id"]),
        )
    if stale:
        logger.info(f"Recovered {len(stale)} stale processing events on startup")


def create_components(
    config: AppConfig, config_path: Path | None = None
) -> dict[str, Any]:
    """Create and wire all application components.

    Returns a dict with keys: provider, registry, permission_gate,
    storage, skills, config, workspace_manager, result_processor, gateway.
    """
    from mini_claw.agent.manager import AgentManager
    from mini_claw.agent.workspace import WorkspaceManager
    from mini_claw.channels.manager import ChannelManager
    from mini_claw.gateway.router import Gateway
    from mini_claw.permissions.approval_store import ApprovalStore
    from mini_claw.permissions.gate import PermissionGate
    from mini_claw.permissions.policy import PermissionPolicy
    from mini_claw.plugins.manager import PluginManager
    from mini_claw.providers.manager import ProviderManager
    from mini_claw.skills.manager import SkillManager
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

    # Provider manager (Phase 1.3)
    provider_manager = ProviderManager(config, storage=storage)

    # Tool registry
    registry = ToolRegistry()
    for tool in BUILTIN_TOOLS:
        registry.register(tool)

    # Skills
    skills_dir = Path.cwd() / "skills"
    skills = load_skills(skills_dir)
    register_skill_tools(registry, skills)
    skill_manager = SkillManager(storage, skills_dir, registry)

    # Permissions (Phase 0.2: ApprovalStore for persistent approvals/grants)
    approval_store = ApprovalStore(storage)
    policy = PermissionPolicy(config.permissions)
    permission_gate = PermissionGate(policy, approval_store)
    # Expire old pending approvals on startup
    expired = approval_store.expire_pending(86400)
    if expired:
        logger.info(f"Expired {expired} old pending approvals on startup")

    # Phase 8 M2: RAG tools (conditional registration based on config)
    rag_manager = None
    if config.rag.enabled and config.rag.namespaces.context_enabled:
        from mini_claw.rag.manager import RagManager
        from mini_claw.tools.rag_tools import (
            TOOL_ARCHIVE_CONTEXT,
            TOOL_CLEAR_CONTEXT,
            TOOL_DELETE_CONTEXT,
            TOOL_DIFF_CONTEXT,
            TOOL_INDEX_CONTEXT,
            TOOL_INSPECT_CONTEXT,
            TOOL_LIST_CONTEXTS,
            TOOL_RAG_STATUS,
            TOOL_READ_SENSITIVE_CONTEXT,
            TOOL_REBIND_CONTEXT,
            TOOL_REEMBED_CONTEXT,
            TOOL_REINDEX_CONTEXT,
            TOOL_SEARCH_CONTEXT,
        )
        rag_manager = RagManager(storage, config.rag, policy)
        for tool in [
            TOOL_INDEX_CONTEXT,
            TOOL_SEARCH_CONTEXT,
            TOOL_LIST_CONTEXTS,
            TOOL_INSPECT_CONTEXT,
            TOOL_CLEAR_CONTEXT,
            TOOL_ARCHIVE_CONTEXT,
            TOOL_DELETE_CONTEXT,
            TOOL_READ_SENSITIVE_CONTEXT,
            TOOL_REINDEX_CONTEXT,  # Phase 8 M3
            TOOL_DIFF_CONTEXT,     # Phase 8.3.5
            TOOL_REEMBED_CONTEXT,  # Phase 8.3.5
            TOOL_REBIND_CONTEXT,   # Phase 8 M3
            TOOL_RAG_STATUS,       # Phase 8 M4.5
        ]:
            registry.register(tool)
        logger.info("RAG context tools registered (rag.enabled=%s)", config.rag.enabled)

    # Phase 8 M5: memory tools (independent gate — memory namespace can be on
    # without context namespace, e.g. for explicit /memory remember only)
    if config.rag.enabled and config.rag.namespaces.memory_enabled:
        from mini_claw.tools.rag_tools import (
            TOOL_MEMORY_COMPACT_TO_RAG,
            TOOL_MEMORY_DELETE,
            TOOL_MEMORY_INSPECT,
            TOOL_MEMORY_LIST,
            TOOL_MEMORY_PIN,
            TOOL_MEMORY_REMEMBER,
            TOOL_MEMORY_SEARCH,
            TOOL_MEMORY_UNPIN,
        )
        if rag_manager is None:
            # Memory-only mode: still need a RagManager
            from mini_claw.rag.manager import RagManager
            rag_manager = RagManager(storage, config.rag, policy)
        for tool in [
            TOOL_MEMORY_REMEMBER,
            TOOL_MEMORY_SEARCH,
            TOOL_MEMORY_LIST,
            TOOL_MEMORY_INSPECT,
            TOOL_MEMORY_PIN,
            TOOL_MEMORY_UNPIN,
            TOOL_MEMORY_DELETE,
            TOOL_MEMORY_COMPACT_TO_RAG,
        ]:
            registry.register(tool)
        logger.info("RAG memory tools registered (memory_enabled=True)")

    # Phase 9 M9.1: chat_search tool (gated by config.chat_search.enabled)
    chat_search_manager = None
    if config.chat_search.enabled:
        from mini_claw.chat_search.manager import ChatSearchManager
        from mini_claw.tools.chat_tools import TOOL_SEARCH_CHAT

        chat_search_manager = ChatSearchManager(
            storage, config.chat_search.__dict__ if hasattr(config.chat_search, "__dict__") else {}
        )
        registry.register(TOOL_SEARCH_CHAT)
        logger.info("Chat search tool registered (chat_search.enabled=True)")

    # Workspaces — one filesystem dir per agent, co-located with the db
    workspace_manager = WorkspaceManager(base_dir=data_dir / "workspaces")
    workspace_manager.load_workspaces(config.agents)

    # Agent manager (Phase 1.2) persists config agents and runtime bindings.
    agent_manager = AgentManager(storage, config, workspace_manager)
    default_agent = agent_manager.resolve_for_chat("feishu", "")
    provider = provider_manager.get_provider_for_agent(default_agent)

    # Result processor for tool outputs
    result_processor = ToolResultProcessor()
    channel_manager = ChannelManager(config)
    plugin_manager = PluginManager(
        plugins_dir=data_dir / "plugins",
        registry=registry,
        channel_manager=channel_manager,
        provider_manager=provider_manager,
        storage=storage,
        integrity_mode=config.plugins.integrity_mode,
    )
    plugin_manager.load_enabled()

    # Gateway — central message orchestrator
    gateway = Gateway(
        config=config,
        storage=storage,
        provider_manager=provider_manager,
        registry=registry,
        permission_gate=permission_gate,
        result_processor=result_processor,
        workspace_manager=workspace_manager,
        agent_manager=agent_manager,
        channel_manager=channel_manager,
        skill_manager=skill_manager,
        rag_manager=rag_manager,
        chat_search_manager=chat_search_manager,
    )
    channel_manager.set_gateway(gateway)
    channel_manager.load_enabled()
    gateway.set_channel_manager(channel_manager)

    return {
        "provider": provider,
        "provider_manager": provider_manager,
        "registry": registry,
        "permission_gate": permission_gate,
        "storage": storage,
        "skills": skills,
        "skill_manager": skill_manager,
        "config": config,
        "workspace_manager": workspace_manager,
        "agent_manager": agent_manager,
        "channel_manager": channel_manager,
        "plugin_manager": plugin_manager,
        "result_processor": result_processor,
        "gateway": gateway,
        "rag_manager": rag_manager,  # Phase 8 M2
    }


def create_app(config: AppConfig, config_path: Path | None = None) -> FastAPI:
    """Factory function: assemble the full FastAPI application.

    Wires up all components and starts the Feishu long-connection client
    in a background thread when enabled, with on_message / on_card_action
    routed to the Gateway.
    """
    components = create_components(config, config_path=config_path)
    registry = components["registry"]
    channel_manager = components["channel_manager"]

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Startup: recover stale processing events from crashes
        _recover_stale_events(components["storage"])

        # Phase 8 M3: opportunistic RAG lifecycle cleanup at startup.
        # Cheap pass — async loop covers periodic cleanup later.
        rag_mgr = components.get("rag_manager")
        if rag_mgr is not None:
            try:
                counts = rag_mgr.cleanup_lifecycle()
                if any(counts.values()):
                    logger.info("RAG lifecycle cleanup at startup: %s", counts)
            except Exception as exc:  # noqa: BLE001
                logger.warning("RAG lifecycle cleanup failed at startup: %s", exc)

        # Phase 9 M9.6: memory maintenance scan at startup (gated by config)
        if (
            rag_mgr is not None
            and config.rag.memory_maintenance.run_on_startup
            and config.rag.namespaces.memory_enabled
        ):
            try:
                # Startup scan uses 'all' scope and always persists suggestions
                ctx = {
                    "agent_id": "system",
                    "chat_id": "startup",
                    "channel_name": "system",
                    "workspace_dir": None,
                }
                result = rag_mgr.run_memory_maintenance(ctx=ctx, scope="all", persist=True)
                if result.get("scanned_count", 0) > 0:
                    logger.info(
                        "Memory maintenance at startup: scanned=%d, duplicates=%d, conflicts=%d, stale=%d, suggestions=%d",
                        result.get("scanned_count", 0),
                        len(result.get("duplicates", [])),
                        len(result.get("conflicts", [])),
                        len(result.get("stale", [])),
                        len(result.get("suggestion_ids", [])),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Memory maintenance failed at startup: %s", exc)

        await channel_manager.start_all()
        try:
            yield
        finally:
            await channel_manager.stop_all()

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
        "on" if channel_manager.has_channel("feishu") else "off",
    )
    return app
