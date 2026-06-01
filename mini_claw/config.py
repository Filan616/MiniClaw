"""Pydantic-based configuration for Mini-Claw."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


DEFAULT_CONFIG_DIR = Path.home() / ".mini-claw"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.toml"


class ProviderConfig(BaseModel):
    provider: str = "deepseek"
    api_key: str = ""
    model: str = "deepseek-chat"
    base_url: Optional[str] = None


class FeishuChannelConfig(BaseModel):
    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""
    verification_token: str = ""
    encrypt_key: str = ""


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    public_url: str = ""


class PermissionsHighRiskConfig(BaseModel):
    allow_explicit: bool = False
    allowed_command_templates: list[str] = Field(default_factory=list)


class PermissionsConfig(BaseModel):
    default_level: str = "L2"
    require_confirm: list[str] = Field(default_factory=lambda: ["L3"])
    deny_by_default: list[str] = Field(default_factory=lambda: ["L4"])
    shell_blacklist: list[str] = Field(
        default_factory=lambda: [r"rm\s+-rf\s+/", r"mkfs", r":\(\)\{"]
    )
    high_risk: PermissionsHighRiskConfig = Field(
        default_factory=PermissionsHighRiskConfig
    )


class AgentConfig(BaseModel):
    id: str = "default"
    system_prompt: str = "你是一个高效的个人助手，能调用工具帮用户完成各种任务。"
    tools: list[str] = Field(
        default_factory=lambda: ["run_shell", "read_file", "write_file"]
    )
    route_chat_ids: list[str] = Field(default_factory=list)


class AppConfig(BaseModel):
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    channels_feishu: FeishuChannelConfig = Field(default_factory=FeishuChannelConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    agents: list[AgentConfig] = Field(default_factory=lambda: [AgentConfig()])


def load_config(path: Path | None = None) -> AppConfig:
    config_path = path or DEFAULT_CONFIG_PATH
    if config_path.exists():
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        if "channels" in data and "feishu" in data["channels"]:
            data["channels_feishu"] = data["channels"]["feishu"]
            del data["channels"]
        return AppConfig(**data)
    return AppConfig()


def ensure_config_dir() -> Path:
    DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_CONFIG_DIR
