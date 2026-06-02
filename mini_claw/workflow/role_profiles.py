"""Built-in role profiles used by the workflow prompt compiler."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RoleProfile:
    name: str
    mission_style: str
    default_tools: list[str] = field(default_factory=list)
    forbidden_tools: list[str] = field(default_factory=list)
    output_schema: dict[str, Any] = field(default_factory=dict)


ROLE_PROFILES: dict[str, RoleProfile] = {
    "researcher": RoleProfile(
        name="researcher",
        mission_style="Investigate facts and report evidence. Do not modify files.",
        default_tools=["read_file", "list_directory"],
        forbidden_tools=["write_file", "run_shell"],
        output_schema={
            "summary": "string",
            "findings": [{"area": "string", "evidence": "string", "uncertain": False}],
            "needs_followup": False,
        },
    ),
    "planner": RoleProfile(
        name="planner",
        mission_style="Create an implementation plan. Do not execute changes.",
        default_tools=["read_file", "list_directory"],
        forbidden_tools=["write_file", "run_shell"],
        output_schema={
            "summary": "string",
            "plan": [{"step": "string", "files": ["string"], "risk": "low|medium|high"}],
            "needs_more_info": False,
        },
    ),
    "implementer": RoleProfile(
        name="implementer",
        mission_style="Make minimal changes according to an approved plan.",
        default_tools=["read_file", "write_file"],
        forbidden_tools=[],
        output_schema={
            "summary": "string",
            "files_changed": ["string"],
            "notes": ["string"],
            "needs_more_info": False,
        },
    ),
    "tester": RoleProfile(
        name="tester",
        mission_style="Run or inspect tests and explain failures. Do not modify files.",
        default_tools=["run_shell", "read_file", "list_directory"],
        forbidden_tools=["write_file"],
        output_schema={
            "summary": "string",
            "tests_run": [{"command": "string", "result": "passed|failed|not_run"}],
            "failures": ["string"],
        },
    ),
    "security_reviewer": RoleProfile(
        name="security_reviewer",
        mission_style="Audit security boundaries. Do not modify files.",
        default_tools=["read_file", "list_directory"],
        forbidden_tools=["write_file", "run_shell"],
        output_schema={
            "summary": "string",
            "risks": [
                {
                    "severity": "high|medium|low",
                    "area": "string",
                    "evidence": "string",
                    "recommendation": "string",
                }
            ],
            "needs_escalation": False,
        },
    ),
    "summarizer": RoleProfile(
        name="summarizer",
        mission_style="Synthesize upstream results into a concise final summary.",
        default_tools=[],
        forbidden_tools=["read_file", "list_directory", "write_file", "run_shell"],
        output_schema={
            "final_summary": "string",
            "key_findings": ["string"],
            "files_changed": ["string"],
            "tests_run": ["string"],
            "remaining_risks": ["string"],
        },
    ),
    "prompt_reviewer": RoleProfile(
        name="prompt_reviewer",
        mission_style="Review compiled prompts for clarity and safety. Do not modify files.",
        default_tools=[],
        forbidden_tools=["read_file", "list_directory", "write_file", "run_shell"],
        output_schema={
            "summary": "string",
            "prompt_issues": [{"node_id": "string", "issue": "string", "severity": "high|medium|low"}],
            "approved": True,
        },
    ),
}


def get_role_profile(name: str) -> RoleProfile:
    return ROLE_PROFILES.get(name, ROLE_PROFILES["researcher"])
