"""SkillManager: per-agent prompt skill activation.

Skills are prompt-only in Phase 3. They cannot register tools, cannot mutate
agent tool allowlists, and cannot elevate permissions. Legacy tool
registration remains in app bootstrap via ``register_skill_tools`` only.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from mini_claw.skills._loader import SkillInfo, load_skills


class SkillManager:
    """Manage discovered skills and per-agent prompt bindings."""

    def __init__(self, storage: Any, skills_dir: Path, registry: Any | None = None) -> None:
        self._storage = storage
        self._skills_dir = skills_dir
        self._registry = registry
        self._skills: dict[str, SkillInfo] = {
            skill.name: skill for skill in load_skills(skills_dir)
        }

    def list_skills(self) -> list[SkillInfo]:
        return [self._skills[name] for name in sorted(self._skills)]

    def get_skill(self, name: str) -> SkillInfo:
        skill = self._skills.get(name)
        if skill is None:
            raise KeyError(f"Unknown skill: {name}")
        return skill

    def enable_for_agent(self, agent_id: str, skill_name: str) -> None:
        self.get_skill(skill_name)
        now = int(time.time())
        self._storage.execute(
            "INSERT OR REPLACE INTO skill_bindings "
            "(agent_id, skill_name, enabled, created_at) VALUES (?, ?, 1, ?)",
            (agent_id, skill_name, now),
        )

    def disable_for_agent(self, agent_id: str, skill_name: str) -> None:
        self.get_skill(skill_name)
        now = int(time.time())
        self._storage.execute(
            "INSERT OR REPLACE INTO skill_bindings "
            "(agent_id, skill_name, enabled, created_at) VALUES (?, ?, 0, ?)",
            (agent_id, skill_name, now),
        )

    def active_skills_for(self, agent_id: str) -> list[SkillInfo]:
        rows = self._storage.fetchall(
            "SELECT skill_name FROM skill_bindings "
            "WHERE agent_id=? AND enabled=1 ORDER BY created_at, skill_name",
            (agent_id,),
        )
        result: list[SkillInfo] = []
        for row in rows:
            skill = self._skills.get(row["skill_name"])
            if skill is None:
                continue
            if skill.agents and agent_id not in skill.agents:
                continue
            result.append(skill)
        return result

    def bindings_for_skill(self, skill_name: str) -> list[dict[str, Any]]:
        return self._storage.fetchall(
            "SELECT agent_id, enabled, created_at FROM skill_bindings "
            "WHERE skill_name=? ORDER BY agent_id",
            (skill_name,),
        )

    def compose_prompt_fragment(
        self,
        agent_id: str,
        agent_tools: list[str],
        budget: int = 8000,
    ) -> str:
        """Compose prompt fragments for active skills.

        The composed text is advisory prompt context only. Missing
        ``requires_tools`` entries generate notice lines instead of changing
        the agent's tool allowlist.
        """
        remaining = max(0, budget)
        blocks: list[str] = []
        notices: list[str] = []
        active = self.active_skills_for(agent_id)[:5]
        enabled_tools = set(agent_tools)

        for skill in active:
            fragment = (skill.prompt_fragment or "").strip()
            max_chars = max(0, min(skill.max_chars, remaining))
            if fragment and max_chars > 0:
                if len(fragment) > max_chars:
                    suffix = f"...(skill {skill.name} truncated due to budget)"
                    keep = max(0, max_chars - len(suffix))
                    fragment = fragment[:keep] + suffix
                block = f"[skill:{skill.name}]\n{fragment}"
                blocks.append(block)
                remaining -= len(block)

            missing = sorted(set(skill.requires_tools) - enabled_tools)
            if missing:
                notices.append(
                    f"[notice] skill {skill.name} suggests tools {missing} "
                    "which are not enabled for this agent"
                )

            if remaining <= 0:
                break

        return "\n\n".join([*blocks, *notices]).strip()
