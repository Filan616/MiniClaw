"""Mini-Claw CLI: 命令行入口。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from mini_claw.config import (
    AgentConfig,
    AppConfig,
    ProviderConfig,
    get_config_path,
    get_data_dir,
    load_config,
)

app = typer.Typer(
    name="mini-claw",
    help="Mini-Claw: 本地优先的个人 AI Agent 助手",
    no_args_is_help=True,
)
agents_app = typer.Typer(help="Agent 管理")
skills_app = typer.Typer(help="Skill 管理")
plugins_app = typer.Typer(help="Plugin 管理")
tasks_app = typer.Typer(help="定时任务管理")
runs_app = typer.Typer(help="运行记录查看")
stats_app = typer.Typer(help="使用统计 (token/耗时)")
rag_app = typer.Typer(help="RAG 子系统状态 (Phase 8)")
chat_search_app = typer.Typer(help="对话搜索索引 (Phase 9 M9.1)")

app.add_typer(agents_app, name="agents")
app.add_typer(skills_app, name="skills")
app.add_typer(plugins_app, name="plugins")
app.add_typer(tasks_app, name="tasks")
app.add_typer(runs_app, name="runs")
app.add_typer(stats_app, name="stats")
app.add_typer(rag_app, name="rag")
app.add_typer(chat_search_app, name="chat-search")

console = Console()


def _db_path(config_path: Optional[Path]) -> Path:
    return get_data_dir(config_path) / "mini_claw.db"


def _agent_manager(config_path: Optional[Path]):
    from mini_claw.agent.manager import AgentManager
    from mini_claw.agent.workspace import WorkspaceManager
    from mini_claw.storage import Database

    cfg = load_config(config_path)
    data_dir = get_data_dir(config_path)
    db = Database(data_dir / "mini_claw.db")
    workspace_manager = WorkspaceManager(base_dir=data_dir / "workspaces")
    workspace_manager.load_workspaces(cfg.agents)
    return AgentManager(db, cfg, workspace_manager)


def _skill_manager(config_path: Optional[Path]):
    from mini_claw.skills.manager import SkillManager
    from mini_claw.storage import Database

    data_dir = get_data_dir(config_path)
    db = Database(data_dir / "mini_claw.db")
    return SkillManager(db, Path.cwd() / "skills")


def _plugin_manager(config_path: Optional[Path]):
    from mini_claw.channels.manager import ChannelManager
    from mini_claw.plugins.manager import PluginManager
    from mini_claw.providers.manager import ProviderManager
    from mini_claw.storage import Database
    from mini_claw.tools.builtin import BUILTIN_TOOLS
    from mini_claw.tools.registry import ToolRegistry

    cfg = load_config(config_path)
    data_dir = get_data_dir(config_path)
    db = Database(data_dir / "mini_claw.db")
    registry = ToolRegistry()
    for tool in BUILTIN_TOOLS:
        registry.register(tool)
    return PluginManager(
        plugins_dir=data_dir / "plugins",
        registry=registry,
        channel_manager=ChannelManager(cfg),
        provider_manager=ProviderManager(cfg),
        storage=db,
    )

# ---------------------------------------------------------------------------
# mini-claw run
# ---------------------------------------------------------------------------


@app.command()
def run(
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="配置文件路径"
    ),
    host: Optional[str] = typer.Option(None, "--host", help="监听地址"),
    port: Optional[int] = typer.Option(None, "--port", "-p", help="监听端口"),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="开启 DEBUG 级别日志"
    ),
) -> None:
    """启动 Mini-Claw 服务。"""
    import logging
    import uvicorn

    from mini_claw.app import create_app

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    cfg = load_config(config_path)
    if host:
        cfg.server.host = host
    if port:
        cfg.server.port = port

    fastapi_app = create_app(cfg, config_path=config_path)
    console.print(
        f"[bold green]Mini-Claw 启动中...[/] "
        f"http://{cfg.server.host}:{cfg.server.port}"
    )
    uvicorn.run(
        fastapi_app,
        host=cfg.server.host,
        port=cfg.server.port,
        log_level="info",
    )


# ---------------------------------------------------------------------------
# mini-claw chat
# ---------------------------------------------------------------------------


@app.command()
def chat(
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="配置文件路径"
    ),
    agent_id: str = typer.Option("default", "--agent", help="Agent ID"),
) -> None:
    """交互式聊天模式（本地测试，无需飞书）。"""
    from mini_claw.app import create_components
    from mini_claw.channels.cli_channel import CLIChannel

    cfg = load_config(config_path)
    components = create_components(cfg, config_path=config_path)
    agent_manager = components["agent_manager"]
    channel_manager = components["channel_manager"]
    gateway = components["gateway"]

    try:
        agent_manager.bind_chat("cli", "cli_local", agent_id)
    except KeyError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)

    cli_channel = CLIChannel(name="cli", agent_id=agent_id)
    channel_manager.register_instance(cli_channel)
    gateway.set_channel_manager(channel_manager)

    async def _chat_loop() -> None:
        await cli_channel.start()

    asyncio.run(_chat_loop())

# ---------------------------------------------------------------------------
# mini-claw setup
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_YAML = """\
provider:
  provider: deepseek
  api_key: ""
  model: deepseek-chat

channels_feishu:
  # 长连接（WebSocket）模式：只需 app_id / app_secret。
  enabled: false
  app_id: ""
  app_secret: ""

server:
  host: 0.0.0.0
  port: 8000
  public_url: ""

permissions:
  default_level: L2
  # sandbox_mode: "safe"（默认）= 路径只能在 workspace 内 + 敏感文件拦截；
  #                "bypass"     = 关闭沙箱，agent 可读写整台电脑（仅在完全信任时使用）。
  # bash 黑名单（rm -rf /、curl|sh 等）不论模式都生效。
  sandbox_mode: safe

agents:
  - id: default
    system_prompt: "你是一个高效的个人助手，能调用工具帮用户完成各种任务。"
    tools:
      - run_shell
      - read_file
      - write_file
      - list_directory

# Phase 9 — chat search (messages_fts)
chat_search:
  enabled: false
  allow_global: false
  fts_max_results: 50
  include_inferred: false

# Phase 9 — memory control & maintenance.
# Top-level keys are mirrored into rag.memory_control / rag.memory_maintenance
# at load time, so either layout works. Top-level wins on conflict.
memory:
  control:
    auto_candidate: true
    auto_write: false
    require_approval: true
    allow_export: true
    allow_clear_scope: true
    auto_candidate_from_agent: false
    export_large_threshold: 50
    batch_approve_max: 20
  maintenance:
    enabled: true
    auto_apply: false
    suggest_only: true
    run_on_startup: false
    run_every_days: 7
    dedupe_text_threshold: 0.85
    dedupe_embedding_threshold: 0.92

# Phase 9 — auto retrieval (default OFF; manual tools always work)
rag:
  retrieval:
    auto_chat_retrieval: false
    auto_context_retrieval: false
    auto_user_memory_retrieval: false
    auto_workspace_memory_retrieval: false
"""


@app.command()
def setup(
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="配置文件路径（默认：当前目录 config.yaml）"
    ),
) -> None:
    """在当前目录生成默认 config.yaml。"""
    config_file = get_config_path(config_path)

    if config_file.exists():
        overwrite = typer.confirm(
            f"配置文件已存在 ({config_file})，是否覆盖？", default=False
        )
        if not overwrite:
            console.print("[yellow]已取消。[/]")
            raise typer.Exit()

    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(_DEFAULT_CONFIG_YAML, encoding="utf-8")
    console.print(f"[green]配置文件已创建:[/] {config_file}")
    console.print("请编辑配置文件填入 API Key 和飞书凭据。")

# ---------------------------------------------------------------------------
# mini-claw doctor
# ---------------------------------------------------------------------------


@app.command()
def doctor(
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="配置文件路径"
    ),
) -> None:
    """检查配置状态：API Key、飞书、数据库等。"""
    cfg = load_config(config_path)
    all_ok = True

    # Check config file
    path = get_config_path(config_path)
    if path.exists():
        console.print(f"[green][OK][/] 配置文件: {path}")
    else:
        console.print(f"[red][FAIL][/] 配置文件不存在: {path}")
        all_ok = False

    # Check API key
    if cfg.provider.api_key:
        masked = cfg.provider.api_key[:4] + "****"
        console.print(f"[green][OK][/] API Key ({cfg.provider.provider}): {masked}")
    else:
        console.print(f"[red][FAIL][/] API Key 未设置 (provider: {cfg.provider.provider})")
        all_ok = False

    # Check Feishu
    if cfg.channels_feishu.enabled:
        if cfg.channels_feishu.app_id and cfg.channels_feishu.app_secret:
            console.print("[green][OK][/] 飞书: 已配置")
        else:
            console.print("[red][FAIL][/] 飞书: 已启用但缺少 app_id/app_secret")
            all_ok = False
    else:
        console.print("[yellow][-][/] 飞书: 未启用")

    # Check database
    db_path = _db_path(config_path)
    if db_path.exists():
        console.print(f"[green][OK][/] 数据库: {db_path}")
    else:
        console.print(f"[yellow][-][/] 数据库: 尚未创建 (首次运行时自动创建)")

    if all_ok:
        console.print("\n[bold green]所有检查通过![/]")
    else:
        console.print("\n[bold yellow]部分配置需要修正。[/]")

# ---------------------------------------------------------------------------
# mini-claw agents list
# ---------------------------------------------------------------------------


@agents_app.command("list")
def agents_list(
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="配置文件路径"
    ),
) -> None:
    """列出已配置的 Agent。"""
    manager = _agent_manager(config_path)
    table = Table(title="Agents")
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Tools", style="green")
    table.add_column("路由 Chat IDs")

    for agent in manager.list_agents():
        table.add_row(
            agent.id,
            agent.name or agent.id,
            ", ".join(agent.tools),
            ", ".join(agent.route_chat_ids) or "(全部)",
        )
    console.print(table)


@agents_app.command("add")
def agents_add(
    agent_id: str = typer.Argument(help="Agent ID"),
    name: Optional[str] = typer.Option(None, "--name", help="Display name"),
    provider: Optional[str] = typer.Option(None, "--provider", help="Provider name"),
    model: Optional[str] = typer.Option(None, "--model", help="Model override"),
    tools: str = typer.Option(
        "run_shell,read_file,write_file", "--tools", help="Comma-separated tools"
    ),
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="配置文件路径"
    ),
) -> None:
    """Add a runtime agent."""
    manager = _agent_manager(config_path)
    cfg = load_config(config_path)
    provider_cfg = None
    if provider:
        provider_cfg = ProviderConfig(
            provider=provider,
            api_key=cfg.provider.api_key,
            model=model or cfg.provider.model,
            base_url=cfg.provider.base_url,
        )
    agent_cfg = AgentConfig(
        id=agent_id,
        name=name,
        provider=provider_cfg,
        model=None if provider_cfg else model,
        tools=[tool.strip() for tool in tools.split(",") if tool.strip()],
    )
    try:
        manager.add_agent(agent_cfg)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    console.print(f"[green]Added agent[/] {agent_id}")


@agents_app.command("remove")
def agents_remove(
    agent_id: str = typer.Argument(help="Agent ID"),
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="配置文件路径"
    ),
) -> None:
    """Remove a runtime agent."""
    manager = _agent_manager(config_path)
    try:
        removed = manager.remove_agent(agent_id)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    if not removed:
        console.print(f"[red]Agent not found:[/] {agent_id}")
        raise typer.Exit(1)
    console.print(f"[green]Removed agent[/] {agent_id}")


@agents_app.command("bind")
def agents_bind(
    channel: str = typer.Argument(help="Channel name"),
    chat_id: str = typer.Argument(help="Chat ID"),
    agent_id: str = typer.Argument(help="Agent ID"),
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="配置文件路径"
    ),
) -> None:
    """Bind a channel chat to an agent."""
    manager = _agent_manager(config_path)
    try:
        manager.bind_chat(channel, chat_id, agent_id)
    except KeyError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    console.print(f"[green]Bound[/] {channel}:{chat_id} -> {agent_id}")


@agents_app.command("inspect")
def agents_inspect(
    agent_id: str = typer.Argument(help="Agent ID"),
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="配置文件路径"
    ),
) -> None:
    """Inspect an agent and its bindings."""
    manager = _agent_manager(config_path)
    try:
        agent = manager.get_agent(agent_id)
    except KeyError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)

    provider_cfg = agent.provider
    provider_name = provider_cfg.provider if provider_cfg else "(default)"
    model = agent.model or (provider_cfg.model if provider_cfg else "(default)")
    console.print(f"[bold]ID:[/] {agent.id}")
    console.print(f"[bold]Name:[/] {agent.name or agent.id}")
    console.print(f"[bold]Workspace:[/] {agent.workspace or f'workspaces/{agent.id}'}")
    console.print(f"[bold]Provider:[/] {provider_name}")
    console.print(f"[bold]Model:[/] {model}")
    console.print(f"[bold]Tools:[/] {', '.join(agent.tools) or '(none)'}")

    bindings = manager.bindings_for(agent_id)
    if bindings:
        table = Table(title="Bindings")
        table.add_column("Channel")
        table.add_column("Chat ID")
        for row in bindings:
            table.add_row(row["channel_name"], row["chat_id"])
        console.print(table)
    else:
        console.print("[yellow]No runtime bindings.[/]")


# ---------------------------------------------------------------------------
# mini-claw skills
# ---------------------------------------------------------------------------


@skills_app.command("list")
def skills_list(
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="配置文件路径"
    ),
) -> None:
    """List discovered skills."""
    manager = _skill_manager(config_path)
    table = Table(title="Skills")
    table.add_column("Name", style="cyan")
    table.add_column("Risk")
    table.add_column("Agents")
    table.add_column("Requires Tools")
    table.add_column("Description")
    for skill in manager.list_skills():
        table.add_row(
            skill.name,
            skill.risk_level,
            ", ".join(skill.agents) or "(all)",
            ", ".join(skill.requires_tools) or "(none)",
            skill.description,
        )
    console.print(table)


@skills_app.command("enable")
def skills_enable(
    agent_id: str = typer.Argument(help="Agent ID"),
    skill_name: str = typer.Argument(help="Skill name"),
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="配置文件路径"
    ),
) -> None:
    """Enable a prompt skill for an agent."""
    manager = _skill_manager(config_path)
    try:
        manager.enable_for_agent(agent_id, skill_name)
    except KeyError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    console.print(f"[green]Enabled skill[/] {skill_name} -> {agent_id}")


@skills_app.command("disable")
def skills_disable(
    agent_id: str = typer.Argument(help="Agent ID"),
    skill_name: str = typer.Argument(help="Skill name"),
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="配置文件路径"
    ),
) -> None:
    """Disable a prompt skill for an agent."""
    manager = _skill_manager(config_path)
    try:
        manager.disable_for_agent(agent_id, skill_name)
    except KeyError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    console.print(f"[green]Disabled skill[/] {skill_name} -> {agent_id}")


@skills_app.command("inspect")
def skills_inspect(
    skill_name: str = typer.Argument(help="Skill name"),
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="配置文件路径"
    ),
) -> None:
    """Inspect a skill manifest and bindings."""
    manager = _skill_manager(config_path)
    agent_manager = _agent_manager(config_path)
    try:
        skill = manager.get_skill(skill_name)
    except KeyError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)

    console.print(f"[bold]Name:[/] {skill.name}")
    console.print(f"[bold]Description:[/] {skill.description}")
    console.print(f"[bold]Trigger:[/] {skill.trigger}")
    console.print(f"[bold]Risk:[/] {skill.risk_level}")
    console.print(f"[bold]Agents:[/] {', '.join(skill.agents) or '(all)'}")
    console.print(f"[bold]Requires tools:[/] {', '.join(skill.requires_tools) or '(none)'}")
    fragment = (skill.prompt_fragment or "").strip()
    console.print(f"[bold]Prompt preview:[/] {fragment[:300] or '(empty)'}")

    bindings = manager.bindings_for_skill(skill_name)
    if bindings:
        table = Table(title="Bindings")
        table.add_column("Agent")
        table.add_column("Enabled")
        for row in bindings:
            table.add_row(row["agent_id"], "yes" if row["enabled"] else "no")
        console.print(table)

    diff_table = Table(title="Requires Tools vs Agent Tools")
    diff_table.add_column("Agent")
    diff_table.add_column("Missing")
    for agent in agent_manager.list_agents():
        missing = sorted(set(skill.requires_tools) - set(agent.tools))
        diff_table.add_row(agent.id, ", ".join(missing) or "(none)")
    console.print(diff_table)


# ---------------------------------------------------------------------------
# mini-claw plugins
# ---------------------------------------------------------------------------


@plugins_app.command("list")
def plugins_list(
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="配置文件路径"
    ),
) -> None:
    """List installed plugins."""
    manager = _plugin_manager(config_path)
    table = Table(title="Plugins")
    table.add_column("Name", style="cyan")
    table.add_column("Enabled")
    table.add_column("Hash")
    table.add_column("Error")
    for row in manager.list_plugins():
        table.add_row(
            row["name"],
            "yes" if row["enabled"] else "no",
            (row.get("manifest_hash") or "")[:12],
            row.get("error_msg") or "",
        )
    console.print(table)


@plugins_app.command("install")
def plugins_install(
    path: Path = typer.Argument(help="Local plugin directory"),
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="配置文件路径"
    ),
) -> None:
    """Install a local plugin directory without enabling it."""
    manager = _plugin_manager(config_path)
    try:
        manifest = manager.install(path)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    console.print(f"[green]Installed plugin disabled by default:[/] {manifest['name']}")


@plugins_app.command("enable")
def plugins_enable(
    name: str = typer.Argument(help="Plugin name"),
    yes: bool = typer.Option(False, "--yes", help="Confirm manifest permissions"),
    force: bool = typer.Option(
        False,
        "--force",
        help="Bypass integrity check (audited). Use only when you trust the changes.",
    ),
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="配置文件路径"
    ),
) -> None:
    """Enable an installed plugin after manifest confirmation.

    Integrity check runs before enabling. In strict mode, hash mismatch fails
    unless --force is passed (which is always audited).
    """
    manager = _plugin_manager(config_path)
    try:
        result = manager.enable(name, confirmed=yes, force=force)
    except RuntimeError as exc:
        console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(code=1)
    manifest = result["manifest"]
    console.print("[bold]Plugin manifest[/]")
    console.print_json(data=manifest)
    if result["requires_confirmation"]:
        if not typer.confirm("Enable this plugin?", default=False):
            console.print("[yellow]Cancelled.[/]")
            raise typer.Exit()
        try:
            result = manager.enable(name, confirmed=True, force=force)
        except RuntimeError as exc:
            console.print(f"[red]Error:[/] {exc}")
            raise typer.Exit(code=1)
    integrity_ok = result.get("integrity_ok", True)
    if not integrity_ok:
        console.print(f"[yellow]⚠️ Integrity check bypassed for plugin[/] {name}")
    console.print(f"[green]Enabled plugin[/] {name}")


@plugins_app.command("disable")
def plugins_disable(
    name: str = typer.Argument(help="Plugin name"),
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="配置文件路径"
    ),
) -> None:
    """Disable a plugin. Tools are hot-removed (Phase B.5).

    Effective immediately for new agent runs. Runs in-flight that have
    already obtained a tool handler reference complete their current call
    normally without disruption.
    """
    manager = _plugin_manager(config_path)
    if not manager.disable(name):
        console.print(f"[red]Plugin not found:[/] {name}")
        raise typer.Exit(1)
    console.print(
        f"[green]Disabled plugin[/] {name}. Tools removed from registry; "
        "new agent runs won't see them."
    )


@plugins_app.command("inspect")
def plugins_inspect(
    name: str = typer.Argument(help="Plugin name"),
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="配置文件路径"
    ),
) -> None:
    """Inspect a plugin manifest, hash, and static audit result."""
    manager = _plugin_manager(config_path)
    try:
        info = manager.inspect(name)
    except Exception as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    console.print("[bold]Manifest[/]")
    console.print_json(data=info["manifest"])
    console.print(f"[bold]Hash:[/] {info['hash']}")
    console.print(f"[bold]Static issues:[/] {info['static_issues'] or '(none)'}")
    if info["row"]:
        console.print("[bold]Database row[/]")
        console.print_json(data=info["row"])


@plugins_app.command("audit")
def plugins_audit(
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="配置文件路径"
    ),
) -> None:
    """Recompute plugin hashes and report integrity drift."""
    manager = _plugin_manager(config_path)
    table = Table(title="Plugin Audit")
    table.add_column("Name", style="cyan")
    table.add_column("Matches")
    table.add_column("Declared")
    table.add_column("Actual")
    table.add_column("Static Issues")
    for row in manager.audit():
        table.add_row(
            row["name"],
            "yes" if row["matches"] else "no",
            (row.get("declared") or "")[:12],
            row["actual"][:12],
            "; ".join(row["static_issues"]) or "(none)",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# mini-claw tasks list / remove
# ---------------------------------------------------------------------------


@tasks_app.command("list")
def tasks_list(
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="配置文件路径"
    ),
) -> None:
    """列出定时任务。"""
    from mini_claw.storage import Database

    db_path = _db_path(config_path)
    if not db_path.exists():
        console.print("[yellow]数据库尚未创建，无定时任务。[/]")
        raise typer.Exit()

    db = Database(db_path)
    rows = db.list_scheduled_tasks()
    if not rows:
        console.print("暂无定时任务。")
        raise typer.Exit()

    table = Table(title="定时任务")
    table.add_column("ID", style="cyan")
    table.add_column("Cron", style="green")
    table.add_column("描述")
    table.add_column("状态")

    for row in rows:
        table.add_row(
            str(row.get("id", "")),
            row.get("cron", ""),
            row.get("description", ""),
            row.get("status", "active"),
        )
    console.print(table)

@tasks_app.command("remove")
def tasks_remove(
    task_id: str = typer.Argument(help="要删除的任务 ID"),
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="配置文件路径"
    ),
) -> None:
    """删除指定的定时任务。"""
    from mini_claw.storage import Database

    db_path = _db_path(config_path)
    if not db_path.exists():
        console.print("[red]数据库不存在。[/]")
        raise typer.Exit(1)

    db = Database(db_path)
    success = db.remove_scheduled_task(task_id)
    if success:
        console.print(f"[green]已删除任务:[/] {task_id}")
    else:
        console.print(f"[red]未找到任务:[/] {task_id}")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# mini-claw runs show
# ---------------------------------------------------------------------------


@runs_app.command("show")
def runs_show(
    run_id: str = typer.Argument(help="运行记录 ID"),
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="配置文件路径"
    ),
) -> None:
    """查看 Agent 运行详情（工具调用链、耗时、Token 用量）。"""
    from mini_claw.storage import Database

    db_path = _db_path(config_path)
    if not db_path.exists():
        console.print("[red]数据库不存在。[/]")
        raise typer.Exit(1)

    db = Database(db_path)
    run_data = db.get_agent_run(run_id)
    if not run_data:
        console.print(f"[red]未找到运行记录:[/] {run_id}")
        raise typer.Exit(1)

    console.print(f"[bold]运行 ID:[/] {run_data.get('id', run_id)}")
    console.print(f"[bold]Agent:[/] {run_data.get('agent_id', 'unknown')}")
    console.print(f"[bold]状态:[/] {run_data.get('status', 'unknown')}")
    console.print(
        f"[bold]耗时:[/] {run_data.get('duration_ms', 0)}ms"
    )
    console.print(
        f"[bold]Token 用量:[/] "
        f"输入 {run_data.get('input_tokens', 0)} / "
        f"输出 {run_data.get('output_tokens', 0)}"
    )

    tool_calls = run_data.get("tool_calls", [])
    if tool_calls:
        console.print("\n[bold]工具调用链:[/]")
        for i, tc in enumerate(tool_calls, 1):
            name = tc.get("tool", tc.get("name", "?"))
            status = tc.get("status", "ok")
            console.print(f"  {i}. {name} [{status}]")


# ---------------------------------------------------------------------------
# Stats commands (Phase B.4)
# ---------------------------------------------------------------------------


def _stats_storage(config_path: Optional[Path]) -> "Database":
    """Open Database for stats queries (no full app bootstrap needed)."""
    from mini_claw.config import get_data_dir
    from mini_claw.storage import Database
    data_dir = get_data_dir(config_path)
    return Database(data_dir / "mini_claw.db")


@stats_app.command("session")
def stats_session(
    chat_id: str = typer.Argument(help="Chat ID to summarize"),
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="配置文件路径"
    ),
) -> None:
    """Show token usage and tool call summary for a session."""
    storage = _stats_storage(config_path)

    # Aggregate runs for this chat_id
    runs = storage.fetchall(
        "SELECT COUNT(*) AS n_runs, "
        "SUM(COALESCE(prompt_tokens, 0)) AS prompt, "
        "SUM(COALESCE(completion_tokens, 0)) AS completion, "
        "SUM(COALESCE(total_tokens, 0)) AS total, "
        "SUM(COALESCE(total_cost_usd, 0)) AS cost "
        "FROM agent_runs WHERE chat_id = ?",
        (chat_id,),
    )
    summary = runs[0] if runs else {}
    n_runs = summary.get("n_runs") or 0
    prompt_t = summary.get("prompt") or 0
    completion_t = summary.get("completion") or 0
    total_t = summary.get("total") or 0
    cost = summary.get("cost") or 0.0

    # Tool call stats
    tools = storage.fetchall(
        "SELECT COUNT(*) AS n, AVG(COALESCE(duration_ms, 0)) AS avg_ms, "
        "MAX(COALESCE(duration_ms, 0)) AS max_ms "
        "FROM tool_calls tc JOIN agent_runs ar ON tc.run_id = ar.id "
        "WHERE ar.chat_id = ?",
        (chat_id,),
    )
    t_summary = tools[0] if tools else {}
    n_calls = t_summary.get("n") or 0
    avg_ms = t_summary.get("avg_ms") or 0
    max_ms = t_summary.get("max_ms") or 0

    table = Table(title=f"Session stats: {chat_id}")
    table.add_column("Metric", style="cyan")
    table.add_column("Value")
    table.add_row("Total runs", str(n_runs))
    table.add_row("Prompt tokens", str(int(prompt_t)))
    table.add_row("Completion tokens", str(int(completion_t)))
    table.add_row("Total tokens", str(int(total_t)))
    table.add_row("Estimated cost (USD)", f"${cost:.4f}")
    table.add_row("Tool calls", str(n_calls))
    table.add_row("Avg tool duration (ms)", f"{avg_ms:.1f}")
    table.add_row("Max tool duration (ms)", str(int(max_ms)))
    console.print(table)


@stats_app.command("top-tools")
def stats_top_tools(
    limit: int = typer.Option(10, "--limit", "-n", help="Number of tools to show"),
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="配置文件路径"
    ),
) -> None:
    """Show top tools by average duration."""
    storage = _stats_storage(config_path)
    rows = storage.fetchall(
        "SELECT tool_name, COUNT(*) AS n, "
        "AVG(COALESCE(duration_ms, 0)) AS avg_ms, "
        "MAX(COALESCE(duration_ms, 0)) AS max_ms, "
        "SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors "
        "FROM tool_calls "
        "WHERE duration_ms IS NOT NULL "
        "GROUP BY tool_name "
        "ORDER BY avg_ms DESC "
        "LIMIT ?",
        (limit,),
    )

    table = Table(title=f"Top {limit} tools by avg duration")
    table.add_column("Tool", style="cyan")
    table.add_column("Calls", justify="right")
    table.add_column("Avg (ms)", justify="right")
    table.add_column("Max (ms)", justify="right")
    table.add_column("Errors", justify="right")
    for row in rows:
        table.add_row(
            row["tool_name"],
            str(row["n"]),
            f"{(row['avg_ms'] or 0):.1f}",
            str(int(row["max_ms"] or 0)),
            str(row["errors"] or 0),
        )
    console.print(table)


# ============================================================
# Phase 8 M4.5: rag status
# ============================================================


@rag_app.command("status")
def rag_status(
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="配置文件路径"),
    as_json: bool = typer.Option(False, "--json", help="输出 JSON 格式"),
) -> None:
    """显示 RAG 子系统的健康快照 (FTS / 向量后端 / embedding / lifecycle 计数)。"""
    from mini_claw.permissions.policy import PermissionPolicy
    from mini_claw.rag.manager import RagManager
    from mini_claw.storage import Database

    cfg = load_config(config)
    if not cfg.rag.enabled:
        if as_json:
            import json as _json
            console.print_json(_json.dumps({"enabled": False, "reason": "rag.enabled=false"}))
        else:
            console.print("RAG is disabled. Set rag.enabled=true in config.yaml to use it.")
        raise typer.Exit(code=0)

    storage = Database(_db_path(config))
    policy = PermissionPolicy(cfg.permissions)
    mgr = RagManager(storage, cfg.rag, policy)

    if as_json:
        import json as _json
        console.print_json(_json.dumps(mgr.status_dict()))
    else:
        console.print(mgr.status_text())


# ============================================================
# Phase 9 M9.1: chat-search rebuild / status CLI
# ============================================================


@chat_search_app.command("rebuild")
def chat_search_rebuild(
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="配置文件路径"),
    as_json: bool = typer.Option(False, "--json", help="输出 JSON 格式"),
) -> None:
    """重建 messages_fts 镜像索引 (Phase 9 M9.1)。

    扫描 messages 全表，DELETE FROM messages_fts，然后逐行 INSERT。
    审计三事件：started / completed / failed。
    """
    from mini_claw.audit.logger import AuditLogger
    from mini_claw.chat_search.manager import ChatSearchManager
    from mini_claw.storage import Database

    cfg = load_config(config)
    if not getattr(cfg, "chat_search", None) or not cfg.chat_search.enabled:
        if as_json:
            import json as _json
            console.print_json(
                _json.dumps({"enabled": False, "reason": "chat_search.enabled=false"})
            )
        else:
            console.print(
                "Chat search is disabled. Set chat_search.enabled=true in config.yaml."
            )
        raise typer.Exit(code=0)

    storage = Database(_db_path(config))
    audit = AuditLogger(storage)
    chat_search_cfg = {
        "enabled": cfg.chat_search.enabled,
        "allow_global": cfg.chat_search.allow_global,
        "fts_max_results": cfg.chat_search.fts_max_results,
        "include_inferred": getattr(cfg.chat_search, "include_inferred", False),
    }
    mgr = ChatSearchManager(storage, chat_search_cfg)

    total_row = storage.fetchone(
        "SELECT COUNT(*) AS cnt FROM messages WHERE content IS NOT NULL"
    )
    total_messages = int(total_row["cnt"]) if total_row else 0

    audit.log_security_event(
        event_type="chat_search_rebuild_started",
        details={"total_messages": total_messages, "scope": "all"},
    )

    try:
        result = mgr.rebuild_index()
    except Exception as exc:
        audit.log_security_event(
            event_type="chat_search_rebuild_failed",
            details={"error": str(exc), "partial_count": 0},
        )
        if as_json:
            import json as _json
            console.print_json(_json.dumps({"ok": False, "error": str(exc)}))
        else:
            console.print(f"[red][ERROR][/red] rebuild failed: {exc}")
        raise typer.Exit(code=1)

    audit.log_security_event(
        event_type="chat_search_rebuild_completed",
        details={
            "indexed": result["indexed"],
            "skipped": result["skipped"],
            "duration_ms": result["duration_ms"],
        },
    )

    if as_json:
        import json as _json
        console.print_json(_json.dumps({"ok": True, **result}))
    else:
        console.print(
            f"[green]✓[/green] Rebuilt messages_fts: "
            f"total={result['total']}, indexed={result['indexed']}, "
            f"skipped={result['skipped']}, duration_ms={result['duration_ms']}"
        )


@chat_search_app.command("status")
def chat_search_status(
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="配置文件路径"),
    as_json: bool = typer.Option(False, "--json", help="输出 JSON 格式"),
) -> None:
    """显示 chat search 索引状态 (FTS 可用性 / 行数对齐)。"""
    from mini_claw.chat_search.manager import ChatSearchManager
    from mini_claw.storage import Database

    cfg = load_config(config)
    storage = Database(_db_path(config))
    chat_search_cfg = {
        "enabled": getattr(cfg.chat_search, "enabled", False) if hasattr(cfg, "chat_search") else False,
        "allow_global": getattr(cfg.chat_search, "allow_global", False) if hasattr(cfg, "chat_search") else False,
        "fts_max_results": getattr(cfg.chat_search, "fts_max_results", 50) if hasattr(cfg, "chat_search") else 50,
    }
    mgr = ChatSearchManager(storage, chat_search_cfg)
    status = mgr.get_status()
    status["enabled"] = chat_search_cfg["enabled"]

    if as_json:
        import json as _json
        console.print_json(_json.dumps(status))
    else:
        console.print(
            f"chat_search enabled       : {status['enabled']}\n"
            f"FTS5 available            : {status['fts_available']}\n"
            f"messages (content!=NULL)  : {status['total_messages']}\n"
            f"messages_fts rows         : {status['fts_count']}\n"
            f"index drift               : {status['total_messages'] - status['fts_count']}"
        )
