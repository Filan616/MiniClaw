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
    AppConfig,
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
tasks_app = typer.Typer(help="定时任务管理")
runs_app = typer.Typer(help="运行记录查看")

app.add_typer(agents_app, name="agents")
app.add_typer(tasks_app, name="tasks")
app.add_typer(runs_app, name="runs")

console = Console()


def _db_path(config_path: Optional[Path]) -> Path:
    return get_data_dir(config_path) / "mini_claw.db"

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
) -> None:
    """交互式聊天模式（本地测试，无需飞书）。"""
    from mini_claw.app import create_components

    cfg = load_config(config_path)
    components = create_components(cfg, config_path=config_path)

    console.print("[bold]Mini-Claw 交互模式[/] (输入 /quit 退出)\n")

    async def _chat_loop() -> None:
        from mini_claw.agent.loop import run_agent_step
        from mini_claw.agent.context import AgentContext
        from mini_claw.tools.registry import ToolContext

        provider = components["provider"]
        registry = components["registry"]
        agent_cfg = cfg.agents[0] if cfg.agents else None

        history: list[dict] = []
        system_prompt = (
            agent_cfg.system_prompt if agent_cfg else "你是一个有用的助手。"
        )

        while True:
            try:
                user_input = console.input("[bold blue]你:[/] ")
            except (EOFError, KeyboardInterrupt):
                break
            if user_input.strip() in ("/quit", "/exit", "/q"):
                break
            if not user_input.strip():
                continue

            history.append({"role": "user", "content": user_input})
            with console.status("思考中..."):
                response = await provider.chat(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        *history,
                    ],
                    tools=registry.schemas_for(
                        agent_cfg.tools if agent_cfg else []
                    ),
                )
            reply = response.text or "(无回复)"
            history.append({"role": "assistant", "content": reply})
            console.print(f"[bold green]助手:[/] {reply}\n")

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
    cfg = load_config(config_path)
    table = Table(title="Agents")
    table.add_column("ID", style="cyan")
    table.add_column("Tools", style="green")
    table.add_column("路由 Chat IDs")

    for agent in cfg.agents:
        table.add_row(
            agent.id,
            ", ".join(agent.tools),
            ", ".join(agent.route_chat_ids) or "(全部)",
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
