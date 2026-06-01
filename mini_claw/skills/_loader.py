"""Skills plugin loader: discover and load skill packages."""

from __future__ import annotations

import importlib.util
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mini_claw.tools.registry import Tool, ToolRegistry

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---", re.DOTALL
)


@dataclass(slots=True)
class SkillInfo:
    """Metadata and tools for a loaded skill."""

    name: str
    description: str
    trigger: str
    tools: list[Tool] = field(default_factory=list)


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Parse YAML-like frontmatter from SKILL.md content."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    result: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip()
    return result


def _load_tools_from_module(tools_py: Path) -> list[Tool]:
    """Import tools.py and collect all Tool objects from it."""
    spec = importlib.util.spec_from_file_location(
        f"skill_tools_{tools_py.parent.name}", tools_py
    )
    if spec is None or spec.loader is None:
        return []
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception:
        logger.exception("Failed to load skill tools from %s", tools_py)
        return []

    tools: list[Tool] = []
    for attr_name in dir(module):
        obj = getattr(module, attr_name)
        if isinstance(obj, Tool):
            tools.append(obj)
    return tools


def load_skills(skills_dir: Path) -> list[SkillInfo]:
    """Scan *skills_dir* for subdirectories containing SKILL.md.

    Each valid skill directory must have a SKILL.md with frontmatter
    defining name, description, and trigger. An optional tools.py can
    export Tool objects.

    Returns:
        List of successfully loaded SkillInfo instances.
    """
    loaded: list[SkillInfo] = []
    if not skills_dir.is_dir():
        logger.warning("Skills directory does not exist: %s", skills_dir)
        return loaded

    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir():
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.exists():
            continue

        text = skill_md.read_text(encoding="utf-8")
        meta = _parse_frontmatter(text)
        name = meta.get("name", child.name)
        description = meta.get("description", "")
        trigger = meta.get("trigger", "")

        # Load tools if tools.py exists
        tools_py = child / "tools.py"
        tools: list[Tool] = []
        if tools_py.exists():
            tools = _load_tools_from_module(tools_py)

        skill = SkillInfo(
            name=name,
            description=description,
            trigger=trigger,
            tools=tools,
        )
        loaded.append(skill)
        logger.info(
            "Loaded skill '%s' with %d tool(s)", name, len(tools)
        )

    return loaded


def register_skill_tools(registry: ToolRegistry, skills: list[SkillInfo]) -> None:
    """Register each skill's tools into the main ToolRegistry."""
    for skill in skills:
        for tool in skill.tools:
            try:
                registry.register(tool)
            except ValueError:
                logger.warning(
                    "Skill '%s': tool '%s' already registered, skipping",
                    skill.name,
                    tool.name,
                )
