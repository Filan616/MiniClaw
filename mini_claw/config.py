"""Pydantic-based configuration for Mini-Claw.

Configuration is stored as ``config.yaml`` in the current working directory.
Run ``mini-claw setup`` in your project directory to generate it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


CONFIG_FILENAME = "config.yaml"


def get_config_path(path: Path | None = None) -> Path:
    """Resolve the config path. Defaults to ``./config.yaml`` in the cwd."""
    if path is not None:
        return path
    return Path.cwd() / CONFIG_FILENAME


def get_data_dir(config_path: Path | None = None) -> Path:
    """Directory used for runtime data (sqlite db, etc).

    Co-located with the config file so the project directory stays
    self-contained.
    """
    return get_config_path(config_path).parent


class ConcurrencyConfig(BaseModel):
    """Concurrency and locking configuration.

    For single-process deployments, use default settings.
    For multi-process deployments, set lock_backend to 'file' and cache_mode to 'db'.
    """

    lock_backend: Literal["asyncio", "file", "sqlite"] = "asyncio"
    """Lock backend: 'asyncio' (single-process), 'file' (multi-process), 'sqlite' (cross-platform multi-process)."""

    cache_mode: Literal["memory", "db"] = "memory"
    """Cache mode: 'memory' (single-process), 'db' (multi-process, disables in-memory cache)."""

    file_lock_dir: str = "./data/locks"
    """Directory for file locks (only used when lock_backend='file')."""

    lock_timeout: float = 30.0
    """Lock acquisition timeout in seconds."""


class PluginsConfig(BaseModel):
    """Plugin system configuration."""

    integrity_mode: Literal["strict", "warn"] = "strict"
    """Integrity check mode: 'strict' (reject hash mismatch) or 'warn' (log only, allow load)."""


class ProviderConfig(BaseModel):
    provider: str = "deepseek"
    api_key: str = ""
    model: str = "deepseek-chat"
    base_url: Optional[str] = None


class FeishuChannelConfig(BaseModel):
    """Feishu (Lark) channel — long-connection mode.

    Long-connection (WebSocket) only requires ``app_id`` / ``app_secret``;
    the SDK opens an outbound connection to Feishu and events are pushed
    over it, so no Webhook URL, ``verification_token``, or ``encrypt_key``
    is needed.
    """

    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""


class ChannelConfig(BaseModel):
    name: str
    type: str
    enabled: bool = True
    options: dict = Field(default_factory=dict)


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    public_url: str = ""


class PermissionsHighRiskConfig(BaseModel):
    allow_explicit: bool = False
    allowed_command_templates: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Shell blacklist defaults
# ---------------------------------------------------------------------------
# Patterns matched via ``re.search`` (substring-anywhere) on the command
# string. Keep word-bounded (``\b``) and flag-anchored where possible to
# avoid firing on safe commands like ``echo 'hello'``, ``ls -la``, or
# ``pytest -k 'rm or curl'``. Mirror updates here in any project-level
# ``config.yaml`` overrides.

_DEFAULT_SHELL_BLACKLIST: tuple[str, ...] = (
    # --- shell-internal destruction ---
    r"\brm\s+(?:-[rRfv]+\s+|--recursive\s+|--force\s+)+/(?:\s|$)",
    r"\brm\s+(?:-[rRfv]+\s+|--recursive\s+|--force\s+)+~(?:/|\s|$)",
    r"\brm\s+(?:-[rRfv]+\s+|--recursive\s+|--force\s+)+\$HOME\b",
    r"\brm\s+(?:-[rRfv]+\s+|--recursive\s+|--force\s+)+/\*",
    r"\bmkfs(?:\.|\s)",
    r"\bdd\s+if=",
    r":\(\)\s*\{",
    r"\bshred\s+-",
    r"\bswapoff\s+-a\b",
    # --- destructive find ---
    r"\bfind\b[^|;&]*\s-delete\b",
    r"\bfind\b[^|;&]*-exec\s+rm\b",
    # --- network -> shell pipes ---
    r"\bcurl\b[^|]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b",
    r"\bwget\b[^|]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b",
    r"\bfetch\b[^|]*\|\s*(?:ba)?sh\b",
    # --- inline interpreters (-c / -e / -r) ---
    r"\bbash\s+-c\b",
    r"\bsh\s+-c\b",
    r"\bzsh\s+-c\b",
    r"\bpython3?\s+-c\b",
    r"\bnode\s+-e\b",
    r"\bperl\s+-e\b",
    r"\bruby\s+-e\b",
    r"\bphp\s+-r\b",
    # --- encoding / decoding bypass ---
    r"\bbase64\s+(?:-d|--decode)\b[^|]*\|\s*(?:ba)?sh\b",
    r"\bxxd\s+-r\b[^|]*\|\s*(?:ba)?sh\b",
    r"\bopenssl\s+enc\s+-d\b[^|]*\|\s*(?:ba)?sh\b",
    # --- eval / command substitution feeding shell ---
    r"\beval\s+[\"']?\$\(\s*curl\b",
    r"\beval\s+[\"']?\$\(\s*wget\b",
    r"`\s*curl\b",
    r"`\s*wget\b",
    # --- credential / system file overwrite (Linux) ---
    r">\s*~/\.ssh/",
    r">\s*\$HOME/\.ssh/",
    r">\s*/etc/passwd\b",
    r">\s*/etc/shadow\b",
    r">\s*/etc/sudoers\b",
    # --- Windows PowerShell vectors ---
    r"(?i)\bpowershell(?:\.exe)?\s+(?:-|/)e(?:nc(?:odedcommand)?)?\b",
    r"(?i)\bpowershell(?:\.exe)?\s+(?:-|/)c(?:ommand)?\b",
    r"(?i)\b(?:iex|Invoke-Expression)\b",
    r"(?i)\bDownloadString\s*\(",
    r"(?i)\biwr\b[^|]*\|\s*iex\b",
)


class ChainDetectorConfig(BaseModel):
    """Chain attack detector configuration (Phase A.3)."""

    enabled: bool = True
    """Enable chain detection. If False, no chain detection runs."""

    session_scope: bool = False
    """Default False: only run-level detection. True: also persists state for cross-message detection."""

    session_ttl: int = 604800
    """Session-level state TTL in seconds (default 7 days)."""


class PermissionsConfig(BaseModel):
    default_level: str = "L2"
    sandbox_mode: str = "safe"  # "safe" = path sandbox + sensitive check; "bypass" = no restrictions
    require_confirm: list[str] = Field(default_factory=lambda: ["L3"])
    deny_by_default: list[str] = Field(default_factory=lambda: ["L4"])
    shell_blacklist: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_SHELL_BLACKLIST)
    )
    high_risk: PermissionsHighRiskConfig = Field(
        default_factory=PermissionsHighRiskConfig
    )
    chain_detector: ChainDetectorConfig = Field(default_factory=ChainDetectorConfig)


class WorkflowTemplateConfig(BaseModel):
    enabled: bool = True


class WorkflowRiskPolicyConfig(BaseModel):
    write_file: str = "approval"
    run_shell: str = "approval"
    dynamic_workflow: str = "approval"


class WorkflowTemplatesConfig(BaseModel):
    debug_fix: WorkflowTemplateConfig = Field(default_factory=WorkflowTemplateConfig)
    code_review: WorkflowTemplateConfig = Field(default_factory=WorkflowTemplateConfig)
    migration: WorkflowTemplateConfig = Field(default_factory=WorkflowTemplateConfig)


class WorkflowConfig(BaseModel):
    enabled: bool = False
    auto_detect: bool = False
    require_approval: bool = True
    max_nodes_per_workflow: int = 8
    max_parallel_nodes: int = 3
    max_total_agent_runs: int = 12
    allow_dynamic: bool = False
    allow_llm_generated_script: bool = False
    max_prompt_chars: int = 12000
    templates: WorkflowTemplatesConfig = Field(default_factory=WorkflowTemplatesConfig)
    risk_policy: WorkflowRiskPolicyConfig = Field(default_factory=WorkflowRiskPolicyConfig)


class AgentConfig(BaseModel):
    id: str = "default"
    name: str | None = None
    system_prompt: str = "你是一个高效的个人助手，能调用工具帮用户完成各种任务。"
    workspace: str | None = None
    provider: ProviderConfig | None = None
    model: str | None = None
    enabled: bool = True
    tools: list[str] = Field(
        default_factory=lambda: ["run_shell", "read_file", "write_file"]
    )
    skills: list[str] = Field(default_factory=list)
    route_chat_ids: list[str] = Field(default_factory=list)


class AppConfig(BaseModel):
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    channels_feishu: FeishuChannelConfig = Field(default_factory=FeishuChannelConfig)
    channels: list[ChannelConfig] = Field(default_factory=list)
    server: ServerConfig = Field(default_factory=ServerConfig)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    agents_defaults: AgentConfig | None = None
    agents: list[AgentConfig] = Field(default_factory=lambda: [AgentConfig()])


def _merge_agent_defaults(data: dict) -> dict:
    defaults = data.get("agents_defaults")
    agents = data.get("agents")
    if not defaults or not isinstance(defaults, dict) or not isinstance(agents, list):
        return data

    merged_agents = []
    for agent in agents:
        if isinstance(agent, dict):
            merged_agents.append({**defaults, **agent})
        else:
            merged_agents.append(agent)

    data = dict(data)
    data["agents"] = merged_agents
    return data


def _normalize_channels(data: dict) -> dict:
    data = dict(data)
    if data.get("channels"):
        return data

    feishu = data.get("channels_feishu")
    if isinstance(feishu, dict):
        data["channels"] = [
            {
                "name": "feishu",
                "type": "feishu",
                "enabled": feishu.get("enabled", False),
                "options": {
                    "app_id": feishu.get("app_id", ""),
                    "app_secret": feishu.get("app_secret", ""),
                },
            }
        ]
    return data


def load_config(path: Path | None = None) -> AppConfig:
    config_path = get_config_path(path)
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if "channels" in data and isinstance(data["channels"], dict) and "feishu" in data["channels"]:
            data["channels_feishu"] = data["channels"]["feishu"]
            del data["channels"]
        data = _normalize_channels(data)
        data = _merge_agent_defaults(data)
        return AppConfig(**data)
    return AppConfig()
