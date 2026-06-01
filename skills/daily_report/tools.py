"""日报生成技能的工具集。"""

from __future__ import annotations

from datetime import date

from mini_claw.tools.registry import Tool


async def _generate_report(
    tasks: str,
    summary: str = "",
    *,
    ctx=None,
) -> str:
    """根据任务列表生成格式化的每日工作报告。"""
    today = date.today().isoformat()
    lines = [
        f"# 每日工作报告 - {today}",
        "",
        "## 今日完成",
    ]
    for item in tasks.split("\n"):
        item = item.strip()
        if item:
            lines.append(f"- {item}")
    lines.append("")
    if summary:
        lines.append("## 总结")
        lines.append(summary)
        lines.append("")
    lines.append("---")
    lines.append("*由 Mini-Claw 自动生成*")
    return "\n".join(lines)


TOOL_GENERATE_REPORT = Tool(
    name="generate_report",
    description="根据任务列表生成结构化的每日工作报告。",
    input_schema={
        "type": "object",
        "properties": {
            "tasks": {
                "type": "string",
                "description": "今日完成的任务，每行一项",
            },
            "summary": {
                "type": "string",
                "description": "可选的总结说明",
                "default": "",
            },
        },
        "required": ["tasks"],
    },
    handler=_generate_report,
    permission_level="L0",
)
