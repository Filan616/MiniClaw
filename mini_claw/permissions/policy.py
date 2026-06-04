"""Permission policy: blacklist checks, path validation, high-risk matching."""

from __future__ import annotations

import re
from pathlib import Path

from mini_claw.config import PermissionsConfig
from mini_claw.utils.paths import assert_not_sensitive


# Phase 8 M2.5: keywords that signal a search query is fishing for secrets.
# Used by ChainDetector to flag "search_context for tokens → exfil" chain (link A).
EXFIL_QUERY_KEYWORDS: tuple[str, ...] = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "api-key",
    "credential",
    ".env",
    "private_key",
    "private-key",
    "privatekey",
    "jwt",
    "oauth",
    "ssh_key",
    "ssh-key",
    "aws_access",
    "aws_secret",
)


# Phase 8 M2.5: phrases that suggest a memory_remember call is trying to
# install a "policy override" rule. Memory is long-term — these must never
# be stored. Used by ChainDetector for link D.
POLICY_LIKE_PHRASES: tuple[str, ...] = (
    "bypass",
    "all permissions",
    "ignore previous",
    "ignore previous instructions",
    "ignore permission",
    "ignore the rule",
    "ignore the rules",
    "no approval",
    "no confirmation",
    "auto approve",
    "auto-approve",
    "always allow",
    "skip approval",
    "skip the gate",
    "绕过",
    "忽略权限",
    "忽略之前",
    "自动允许",
    "自动批准",
    "无需审批",
    "跳过审批",
    "跳过权限",
    "不需要审批",
    "不需要确认",
)


# Phase 8 M2.5: directories where writing retrieved content would exfiltrate
# beyond the agent's intended scope. Used by ChainDetector for link B.
EXFIL_WRITE_DIR_PATTERNS: tuple[str, ...] = (
    "public/",
    "public\\",
    "export/",
    "export\\",
    "exports/",
    "exports\\",
    "dist/",
    "dist\\",
    "/tmp/",
    "\\tmp\\",
    "/var/tmp/",
)


# Phase 8 M2.5: shell commands that signal external network exfiltration.
# Distinguishes localhost (allowed) from public hosts (blocked when
# preceded by a sensitive search). Used by ChainDetector for link A.
EXFIL_NETWORK_TOOLS: tuple[str, ...] = (
    "curl",
    "wget",
    "scp",
    "rsync",
    "nc ",
    "netcat",
    "ftp ",
    "sftp",
)


def looks_like_exfil_query(query: str) -> bool:
    """True if *query* fishes for secrets (case-insensitive substring match)."""
    if not query:
        return False
    q = query.lower()
    return any(kw in q for kw in EXFIL_QUERY_KEYWORDS)


def get_exfil_query_keywords(query: str) -> list[str]:
    """Return the list of matched EXFIL_QUERY_KEYWORDS in the query.

    Used by ChainDetector to store keyword_class in chat_search_queries
    for audit trail and granular detection (cs-4).

    Args:
        query: The search query to analyze

    Returns:
        List of matched keywords (e.g., ["token", "password"])
    """
    if not query:
        return []
    q = query.lower()
    return [kw for kw in EXFIL_QUERY_KEYWORDS if kw in q]


def looks_like_policy_override(content: str) -> bool:
    """True if *content* contains policy-override language."""
    if not content:
        return False
    c = content.lower()
    return any(phrase.lower() in c for phrase in POLICY_LIKE_PHRASES)


def looks_like_exfil_write_path(path: str) -> bool:
    """True if *path* points at a directory commonly used for exfil drops."""
    if not path:
        return False
    p = path.replace("\\", "/").lower()
    # Match the patterns against normalized forward-slash path
    norm_patterns = (
        "public/",
        "export/",
        "exports/",
        "dist/",
        "/tmp/",
        "/var/tmp/",
    )
    return any(pat in p for pat in norm_patterns)


def looks_like_external_network_command(cmd: str) -> bool:
    """True if *cmd* invokes a network tool against a non-localhost target."""
    if not cmd:
        return False
    c = cmd.lower()
    if not any(tool in c for tool in EXFIL_NETWORK_TOOLS):
        return False
    # If localhost / 127.0.0.1 / ::1 only, allow
    localhost_markers = ("localhost", "127.0.0.1", "::1", "0.0.0.0")
    if any(m in c for m in localhost_markers):
        # But still flag if it ALSO contains an external host (defensive)
        # Heuristic: presence of 'http://' or 'https://' followed by non-localhost
        for marker in ("http://", "https://"):
            if marker in c:
                tail = c.split(marker, 1)[1]
                # Take host portion until / or whitespace
                host = tail.split("/", 1)[0].split()[0] if tail else ""
                if host and not any(lm in host for lm in localhost_markers):
                    return True
        return False
    return True


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
        """Return True if *cmd* matches any shell_blacklist regex.

        Patterns are matched via ``re.search`` (anywhere-in-string). Do not
        switch to ``fullmatch`` — the default patterns assume substring
        semantics.
        """
        return any(p.search(cmd) for p in self._blacklist_patterns)

    def first_blacklist_match(self, cmd: str) -> str | None:
        """Return the first blacklist pattern that matches *cmd*, or None.

        Used for audit logging to record which specific pattern was triggered.
        """
        for p in self._blacklist_patterns:
            if p.search(cmd):
                return p.pattern
        return None

    def is_sensitive_path(self, path: str) -> bool:
        """Return True if *path* points at a known-sensitive credential file.

        Wraps :func:`mini_claw.utils.paths.assert_not_sensitive` and converts
        the raised ``ValueError`` into a boolean. Useful at the policy layer
        as a defense-in-depth complement to the tool-level check.
        """
        if not path:
            return False
        try:
            assert_not_sensitive(path)
        except ValueError:
            return True
        return False

    def is_sensitive_path_allowlisted(self, path: str) -> bool:
        """Exact-match allowlist for sensitive paths.

        Reuses ``high_risk.allowed_command_templates`` as an opt-in list of
        specific files the user wants to permit (e.g. a project-local
        ``.env`` they explicitly want the agent to read). Match is exact
        string equality — regex metacharacters are not honoured here, so
        a stray ``.*`` cannot accidentally widen the allowlist.
        """
        templates = self._config.high_risk.allowed_command_templates or []
        return any(t == path for t in templates)

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
