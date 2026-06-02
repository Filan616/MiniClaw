"""ProviderManager: per-agent provider resolution with health check and fallback (Phase B.7)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

from mini_claw.config import AgentConfig, AppConfig, ProviderConfig
from mini_claw.providers import get_provider
from mini_claw.providers.base import Provider


@dataclass
class ProviderHealth:
    """Health record for a provider instance."""

    provider_id: str
    last_check_at: int | None = None
    last_ok_at: int | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    healthy: bool = True


# Failure threshold for marking unhealthy
FAILURE_THRESHOLD = 3


class ProviderManager:
    """Resolve the Provider instance an agent should use.

    Phase B.7: supports health checks, fallback chains, and session-consistent
    provider binding (avoids mid-session swaps).
    """

    def __init__(
        self,
        config: AppConfig,
        default_provider: Provider | None = None,
        storage: Any = None,
    ) -> None:
        self._config = config
        self._storage = storage
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

    def provider_id_for(self, cfg: ProviderConfig) -> str:
        """Stable identifier used in DB tables."""
        return f"{cfg.provider}:{cfg.model}"

    def _get_or_create(self, cfg: ProviderConfig) -> Provider:
        key = self._key_for(cfg)
        provider = self._providers.get(key)
        if provider is None:
            provider = get_provider(cfg)
            self._providers[key] = provider
        return provider

    def get_provider_for_agent(self, agent_cfg: AgentConfig) -> Provider:
        """Backward-compatible API: return the primary provider for an agent.

        Does not consult health state. Use ``resolve_provider_for_session`` for
        fallback-aware resolution.
        """
        cfg = self._effective_config(agent_cfg)
        return self._get_or_create(cfg)

    def resolve_provider_for_session(
        self, agent_cfg: AgentConfig, bound_provider_id: str | None = None
    ) -> tuple[Provider, str]:
        """Resolve provider with fallback + session consistency (Phase B.7).

        Returns (provider, provider_id). The provider_id should be persisted
        on the session so subsequent turns reuse the same provider, even if
        the primary recovers mid-session (avoids context inconsistency).

        Resolution order:
        1. If session has bound_provider_id and that provider is reachable,
           use it.
        2. Otherwise, walk primary -> fallback chain, picking first healthy.
        3. If all unhealthy, fall through to primary (last resort).
        """
        primary_cfg = self._effective_config(agent_cfg)
        candidates: list[ProviderConfig] = [primary_cfg]
        candidates.extend(agent_cfg.provider_fallback)

        # 1. Honor existing session binding if still available
        if bound_provider_id:
            for cfg in candidates:
                if self.provider_id_for(cfg) == bound_provider_id:
                    if self._is_healthy(bound_provider_id):
                        return self._get_or_create(cfg), bound_provider_id
                    break  # bound provider is unhealthy; fall through to fallback

        # 2. Walk candidates and pick first healthy
        for cfg in candidates:
            pid = self.provider_id_for(cfg)
            if self._is_healthy(pid):
                return self._get_or_create(cfg), pid

        # 3. Last resort: primary
        return self._get_or_create(primary_cfg), self.provider_id_for(primary_cfg)

    def _is_healthy(self, provider_id: str) -> bool:
        """Check current health state for a provider."""
        if self._storage is None:
            return True
        row = self._storage.fetchone(
            "SELECT healthy FROM provider_health WHERE provider_id = ?",
            (provider_id,),
        )
        if row is None:
            return True  # no record yet, assume healthy
        return bool(row.get("healthy"))

    def record_success(self, provider_id: str) -> None:
        """Record a successful provider call."""
        if self._storage is None:
            return
        now = int(time.time())
        self._storage.execute(
            "INSERT INTO provider_health "
            "(provider_id, last_check_at, last_ok_at, last_error, "
            " consecutive_failures, healthy) VALUES (?, ?, ?, NULL, 0, 1) "
            "ON CONFLICT(provider_id) DO UPDATE SET "
            "last_check_at=excluded.last_check_at, "
            "last_ok_at=excluded.last_ok_at, "
            "last_error=NULL, consecutive_failures=0, healthy=1",
            (provider_id, now, now),
        )

    def record_failure(self, provider_id: str, error: str) -> None:
        """Record a failed provider call. Marks unhealthy after FAILURE_THRESHOLD."""
        if self._storage is None:
            return
        now = int(time.time())
        # Atomic UPDATE OR INSERT pattern
        existing = self._storage.fetchone(
            "SELECT consecutive_failures FROM provider_health WHERE provider_id = ?",
            (provider_id,),
        )
        if existing is None:
            failures = 1
        else:
            failures = (existing.get("consecutive_failures") or 0) + 1
        healthy = 0 if failures >= FAILURE_THRESHOLD else 1
        self._storage.execute(
            "INSERT INTO provider_health "
            "(provider_id, last_check_at, last_error, consecutive_failures, healthy) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(provider_id) DO UPDATE SET "
            "last_check_at=excluded.last_check_at, "
            "last_error=excluded.last_error, "
            "consecutive_failures=excluded.consecutive_failures, "
            "healthy=excluded.healthy",
            (provider_id, now, error[:500], failures, healthy),
        )

    async def probe(self, provider_id: str, cfg: ProviderConfig) -> ProviderHealth:
        """Active probe: send a minimal request to the provider.

        Updates provider_health table based on result.
        """
        provider = self._get_or_create(cfg)
        try:
            # Minimal request — most providers expose a complete()/chat() method
            # We don't strictly require this to succeed; any non-exception is OK
            if hasattr(provider, "complete"):
                await provider.complete(
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=1,
                )
            self.record_success(provider_id)
            return self.health_check(provider_id)
        except Exception as exc:
            self.record_failure(provider_id, str(exc))
            return self.health_check(provider_id)

    def reload_agent_provider(self, agent_id: str) -> None:
        raise NotImplementedError("Provider hot reload is not implemented yet")

    def health_check(self, provider_id: str) -> ProviderHealth:
        """Return current health state for a provider (read-only)."""
        if self._storage is None:
            return ProviderHealth(provider_id=provider_id)
        row = self._storage.fetchone(
            "SELECT * FROM provider_health WHERE provider_id = ?",
            (provider_id,),
        )
        if row is None:
            return ProviderHealth(provider_id=provider_id)
        return ProviderHealth(
            provider_id=provider_id,
            last_check_at=row.get("last_check_at"),
            last_ok_at=row.get("last_ok_at"),
            last_error=row.get("last_error"),
            consecutive_failures=row.get("consecutive_failures") or 0,
            healthy=bool(row.get("healthy", 1)),
        )

    def list_provider_ids(self) -> list[str]:
        return [f"{p}:{m}" for p, m, _base, _key in self._providers.keys()]
