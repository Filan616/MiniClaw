"""Permission policy: blacklist checks, path validation, high-risk matching."""

from __future__ import annotations

import re
from pathlib import Path

from mini_claw.config import PermissionsConfig


class PermissionPolicy:
    """Stateless policy evaluator driven by PermissionsConfig."""

    def __init__(self, config: PermissionsConfig) -> None:
        self._config = config
        self._blacklist_patterns: list[re.Pattern[str]] = [
            re.compile(p) for p in config.shell_blacklist
        ]

    @property
    def config(self) -> PermissionsConfig:
        return self._config

    def is_blacklisted(self, cmd: str) -> bool:
        """Return True if *cmd* matches any shell_blacklist regex."""
        return any(p.search(cmd) for p in self._blacklist_patterns)

    @staticmethod
    def path_in_workspace(path: str, workspace_dir: Path) -> bool:
        """Return True if *path* resolves to a location within *workspace_dir*."""
        resolved = Path(path).resolve()
        workspace_resolved = workspace_dir.resolve()
        try:
            return resolved.is_relative_to(workspace_resolved)
        except TypeError:
            # Python < 3.9 fallback
            try:
                resolved.relative_to(workspace_resolved)
                return True
            except ValueError:
                return False

    def matches_high_risk_template(self, tool: str, args: dict) -> bool:
        """Check if a tool call matches an allowed high-risk command template.

        Only applies when high_risk.allow_explicit is True.
        Templates are matched against "{tool}:{command}" or just the command string.
        """
        if not self._config.high_risk.allow_explicit:
            return False

        templates = self._config.high_risk.allowed_command_templates
        if not templates:
            return False

        # Build the candidate string to match against templates
        cmd = args.get("command", args.get("cmd", ""))
        candidates = [f"{tool}:{cmd}", cmd]

        for template in templates:
            pattern = re.compile(template)
            if any(pattern.fullmatch(c) for c in candidates):
                return True

        return False
