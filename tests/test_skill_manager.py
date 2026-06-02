from pathlib import Path

import pytest

from mini_claw.agent.context import AgentContext
from mini_claw.agent.loop import AgentRun, RunOutcome, run_agent_step
from mini_claw.providers.base import LLMResponse, Provider
from mini_claw.skills.manager import SkillManager
from mini_claw.storage.db import Database
from mini_claw.tools.registry import ToolRegistry


class CapturingProvider(Provider):
    def __init__(self) -> None:
        self.messages = []

    async def chat(self, messages, tools=None, stream=False, stream_callback=None):
        self.messages = messages
        return LLMResponse(text="done")

    def format_tools(self, tools):
        return tools


class AllowGate:
    def evaluate(self, tool, args, ctx):
        raise AssertionError("no tool calls expected")


def _write_skill(base: Path, name: str, body: str, extra_meta: str = "") -> None:
    skill_dir = base / name
    skill_dir.mkdir()
    skill_dir.joinpath("SKILL.md").write_text(
        f"""---
name: {name}
description: Test skill
trigger: test
{extra_meta}---

{body}
""",
        encoding="utf-8",
    )


def test_skill_manager_composes_prompt_and_notice(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(
        skills_dir,
        "report",
        "Use a structured report format.",
        "requires_tools:\n  - read_file\n  - run_shell\n",
    )
    db = Database(tmp_path / "skills.db")
    manager = SkillManager(db, skills_dir)
    manager.enable_for_agent("default", "report")

    prompt = manager.compose_prompt_fragment("default", ["read_file"])

    assert "[skill:report]" in prompt
    assert "Use a structured report format." in prompt
    assert "[notice] skill report suggests tools ['run_shell']" in prompt


def test_skill_agent_allowlist_is_enforced(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(
        skills_dir,
        "default_only",
        "Only default sees this.",
        "agents:\n  - default\n",
    )
    db = Database(tmp_path / "skills.db")
    manager = SkillManager(db, skills_dir)
    manager.enable_for_agent("ops", "default_only")

    assert manager.active_skills_for("ops") == []


def test_skill_prompt_budget_truncates(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir, "long", "x" * 200)
    db = Database(tmp_path / "skills.db")
    manager = SkillManager(db, skills_dir)
    manager.enable_for_agent("default", "long")

    prompt = manager.compose_prompt_fragment("default", [], budget=80)

    assert "skill long truncated due to budget" in prompt
    assert len(prompt) <= 120


@pytest.mark.asyncio
async def test_skill_prompt_is_injected_without_registering_tools(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir, "report", "Always answer in report style.")
    db = Database(tmp_path / "skills.db")
    registry = ToolRegistry()
    manager = SkillManager(db, skills_dir, registry)
    manager.enable_for_agent("default", "report")
    provider = CapturingProvider()

    run = AgentRun(
        id="run",
        chat_id="chat",
        agent_id="default",
        status=RunOutcome.DONE,
        messages=[{"role": "user", "content": "hi"}],
        allowed_tools=["echo"],
    )
    ctx = AgentContext(
        chat_id="chat",
        agent_id="default",
        workspace_dir=tmp_path,
        system_prompt="Base prompt.",
        skill_manager=manager,
    )

    await run_agent_step(
        run=run,
        provider=provider,
        registry=registry,
        permission_gate=AllowGate(),
        result_processor=None,
        ctx=ctx,
    )

    assert provider.messages[0]["role"] == "system"
    assert "Base prompt." in provider.messages[0]["content"]
    assert "Always answer in report style." in provider.messages[0]["content"]
    assert registry.list_tools() == []
