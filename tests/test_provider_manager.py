from mini_claw.config import AgentConfig, AppConfig, ProviderConfig
from mini_claw.providers.base import LLMResponse, Provider
from mini_claw.providers.manager import ProviderManager


class DummyProvider(Provider):
    async def chat(self, messages, tools=None, stream=False, stream_callback=None):
        return LLMResponse(text="ok")

    def format_tools(self, tools):
        return tools


def test_default_provider_instance_is_reused_for_default_agent():
    cfg = AppConfig(provider=ProviderConfig(provider="deepseek", api_key="k", model="m"))
    provider = DummyProvider()
    manager = ProviderManager(cfg, default_provider=provider)

    assert manager.get_provider_for_agent(AgentConfig(id="default")) is provider


def test_agent_model_override_gets_distinct_provider():
    cfg = AppConfig(provider=ProviderConfig(provider="deepseek", api_key="k", model="m"))
    provider = DummyProvider()
    manager = ProviderManager(cfg, default_provider=provider)

    overridden = manager.get_provider_for_agent(AgentConfig(id="ops", model="other"))
    assert overridden is not provider
    assert manager.get_provider_for_agent(AgentConfig(id="ops", model="other")) is overridden


def test_health_check_shape():
    manager = ProviderManager(AppConfig(), default_provider=DummyProvider())
    health = manager.health_check("deepseek:m")

    assert health.provider_id == "deepseek:m"
    assert health.healthy is True
