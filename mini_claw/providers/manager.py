"""ProviderManager: per-agent provider resolution with shared instances."""

from __future__ import annotations

from dataclasses import dataclass

from mini_claw.config import AgentConfig, AppConfig, ProviderConfig
from mini_claw.providers import get_provider
from mini_claw.providers.base import Provider


@dataclass(frozen=True)
class ProviderHealth:
    provider_id: str
    last_ok_at: int | None = None
    last_error: str | None = None
    healthy: bool = True


class ProviderManager:
    """Resolve the Provider instance an agent should use."""

    def __init__(
        self,
        config: AppConfig,
        default_provider: Provider | None = None,
    ) -> None:
        self._config = config
        self._providers: dict[tuple[str, str, str | None, str], Provider] = {}
        if default_provider is not None:
            key = self._key_for(config.provider)
            self._providers[key] = default_provider

    def _effective_config(self, agent_cfg: AgentConfig) -> ProviderConfig:
        cfg = agent_cfg.provider or self._config.provider
        if agent_cfg.model:
            cfg = cfg.model_copy(update={"model": agent_cfg.model})
        return cfg

    def _key_for(self, cfg: ProviderConfig) -> tuple[str, str, str | None, str]:
        return (cfg.provider, cfg.model, cfg.base_url, cfg.api_key)

    def get_provider_for_agent(self, agent_cfg: AgentConfig) -> Provider:
        cfg = self._effective_config(agent_cfg)
        key = self._key_for(cfg)
        provider = self._providers.get(key)
        if provider is None:
            provider = get_provider(cfg)
            self._providers[key] = provider
        return provider

    def reload_agent_provider(self, agent_id: str) -> None:
        raise NotImplementedError("Provider hot reload is not implemented yet")

    def health_check(self, provider_id: str) -> ProviderHealth:
        return ProviderHealth(provider_id=provider_id)

    def list_provider_ids(self) -> list[str]:
        return [f"{p}:{m}" for p, m, _base, _key in self._providers.keys()]
