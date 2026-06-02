"""Tests for Provider Health Check + Fallback (Phase B.7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mini_claw.config import AgentConfig, AppConfig, ProviderConfig
from mini_claw.providers.base import Provider
from mini_claw.providers.manager import (
    FAILURE_THRESHOLD,
    ProviderHealth,
    ProviderManager,
)
from mini_claw.storage.db import Database


class _FakeProvider:
    """Stand-in Provider for tests; doesn't make network calls."""

    def __init__(self, cfg: ProviderConfig) -> None:
        self.cfg = cfg

    async def complete(self, messages, max_tokens=None, **kwargs):
        return None


@pytest.fixture
def storage(tmp_path: Path) -> Database:
    return Database(tmp_path / "provider_health.db")


@pytest.fixture
def app_config() -> AppConfig:
    return AppConfig(
        provider=ProviderConfig(
            provider="deepseek", model="deepseek-chat", api_key="test"
        ),
    )


def test_provider_health_table_exists(storage: Database):
    """Phase B.7: provider_health table should exist."""
    rows = storage.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='provider_health'"
    )
    assert len(rows) == 1


def test_sessions_provider_id_column_exists(storage: Database):
    """Phase B.7: sessions table should have provider_id column."""
    cols = storage.fetchall("PRAGMA table_info(sessions)")
    col_names = [c["name"] for c in cols]
    assert "provider_id" in col_names


def test_record_success_marks_healthy(storage: Database, app_config: AppConfig):
    """Phase B.7: record_success resets failures and marks healthy."""
    pm = ProviderManager(app_config, storage=storage)
    pm.record_failure("p1", "boom")
    pm.record_failure("p1", "boom")
    pm.record_success("p1")

    health = pm.health_check("p1")
    assert health.healthy is True
    assert health.consecutive_failures == 0
    assert health.last_ok_at is not None


def test_record_failure_threshold_marks_unhealthy(
    storage: Database, app_config: AppConfig
):
    """Phase B.7: After FAILURE_THRESHOLD failures, provider marked unhealthy."""
    pm = ProviderManager(app_config, storage=storage)
    for _ in range(FAILURE_THRESHOLD):
        pm.record_failure("flaky", "timeout")

    health = pm.health_check("flaky")
    assert health.healthy is False
    assert health.consecutive_failures >= FAILURE_THRESHOLD
    assert health.last_error == "timeout"


def test_resolve_picks_fallback_when_primary_unhealthy(
    storage: Database, app_config: AppConfig, monkeypatch
):
    """Phase B.7: resolver picks first healthy fallback when primary fails."""
    primary = ProviderConfig(provider="primary", model="m1", api_key="k")
    fallback = ProviderConfig(provider="fallback", model="m2", api_key="k")
    agent = AgentConfig(
        id="a1",
        provider=primary,
        provider_fallback=[fallback],
    )

    pm = ProviderManager(app_config, storage=storage)
    # Stub out provider creation to avoid real provider lookup
    monkeypatch.setattr(
        "mini_claw.providers.manager.get_provider",
        lambda cfg: _FakeProvider(cfg),
    )

    # Mark primary unhealthy
    for _ in range(FAILURE_THRESHOLD):
        pm.record_failure("primary:m1", "down")

    provider, pid = pm.resolve_provider_for_session(agent)
    assert pid == "fallback:m2"


def test_resolve_honors_session_binding_when_healthy(
    storage: Database, app_config: AppConfig, monkeypatch
):
    """Phase B.7: same session keeps using bound provider even if primary recovers."""
    primary = ProviderConfig(provider="primary", model="m1", api_key="k")
    fallback = ProviderConfig(provider="fallback", model="m2", api_key="k")
    agent = AgentConfig(id="a1", provider=primary, provider_fallback=[fallback])

    pm = ProviderManager(app_config, storage=storage)
    monkeypatch.setattr(
        "mini_claw.providers.manager.get_provider",
        lambda cfg: _FakeProvider(cfg),
    )

    # Mark fallback healthy explicitly (so binding check passes)
    pm.record_success("fallback:m2")

    # Both primary and fallback are healthy; session was previously bound to fallback
    provider, pid = pm.resolve_provider_for_session(
        agent, bound_provider_id="fallback:m2"
    )
    # Should keep using fallback (session consistency)
    assert pid == "fallback:m2"


def test_resolve_drops_session_binding_when_bound_unhealthy(
    storage: Database, app_config: AppConfig, monkeypatch
):
    """Phase B.7: bound provider unhealthy -> fall through to next healthy."""
    primary = ProviderConfig(provider="primary", model="m1", api_key="k")
    fallback = ProviderConfig(provider="fallback", model="m2", api_key="k")
    agent = AgentConfig(id="a1", provider=primary, provider_fallback=[fallback])

    pm = ProviderManager(app_config, storage=storage)
    monkeypatch.setattr(
        "mini_claw.providers.manager.get_provider",
        lambda cfg: _FakeProvider(cfg),
    )

    # Bound provider (fallback) is unhealthy
    for _ in range(FAILURE_THRESHOLD):
        pm.record_failure("fallback:m2", "down")

    provider, pid = pm.resolve_provider_for_session(
        agent, bound_provider_id="fallback:m2"
    )
    # Should fall through to primary (next in chain that's healthy)
    assert pid == "primary:m1"


def test_resolve_new_session_picks_primary_when_healthy(
    storage: Database, app_config: AppConfig, monkeypatch
):
    """Phase B.7: new session (no binding) prefers primary if healthy."""
    primary = ProviderConfig(provider="primary", model="m1", api_key="k")
    fallback = ProviderConfig(provider="fallback", model="m2", api_key="k")
    agent = AgentConfig(id="a1", provider=primary, provider_fallback=[fallback])

    pm = ProviderManager(app_config, storage=storage)
    monkeypatch.setattr(
        "mini_claw.providers.manager.get_provider",
        lambda cfg: _FakeProvider(cfg),
    )

    # No binding, both healthy
    provider, pid = pm.resolve_provider_for_session(agent, bound_provider_id=None)
    assert pid == "primary:m1"


def test_health_check_no_storage_returns_default(app_config: AppConfig):
    """Phase B.7: health_check without storage returns default healthy state."""
    pm = ProviderManager(app_config)
    health = pm.health_check("any:thing")
    assert health.healthy is True
    assert health.consecutive_failures == 0
