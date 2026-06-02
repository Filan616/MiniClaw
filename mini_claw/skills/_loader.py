"""Skills plugin loader: discover and load skill packages."""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from mini_claw.tools.registry import Tool, ToolRegistry

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SkillInfo:
    """Metadata and legacy tools for a loaded skill.

    Security boundary:
    - Skills cannot elevate permissions. They only provide prompt text.
    - ``requires_tools`` is an audit hint only and never mutates an agent's
      tool allowlist.
    - SkillManager must not call ``register_skill_tools``. Legacy tool
      registration only happens during app bootstrap for backwards
      compatibility with existing skills such as ``daily_report``.
    """

    name: str
    description: str
    trigger: str
    prompt_fragment: str | None = None
    agents: list[str] = field(default_factory=list)
    max_chars: int = 8000
    risk_level: str = "low"
    requires_tools: list[str] = field(default_factory=list)
    tools: list[Tool] = field(default_factory=list)


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter and return ``(metadata, body)``."""
    if not text.startswith("---"):
        return {}, text.strip()
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text.strip()
    meta = yaml.safe_load(parts[1]) or {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, parts[2].strip()


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
        meta, body = _split_frontmatter(text)
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
            prompt_fragment=body or None,
            agents=list(meta.get("agents") or []),
            max_chars=int(meta.get("max_chars") or 8000),
            risk_level=meta.get("risk_level", "low"),
            requires_tools=list(meta.get("requires_tools") or []),
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
