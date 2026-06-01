"""LLM provider package with factory function."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import LLMResponse, Provider, ToolCall

if TYPE_CHECKING:
    from mini_claw.config import ProviderConfig

__all__ = ["LLMResponse", "Provider", "ToolCall", "get_provider"]


def get_provider(cfg: ProviderConfig) -> Provider:
    """Instantiate the correct provider based on config."""
    name = cfg.provider.lower()

    if name == "deepseek":
        from .deepseek import DeepSeekProvider

        return DeepSeekProvider(
            api_key=cfg.api_key,
            model=cfg.model,
            base_url=cfg.base_url,
        )
    elif name == "openai":
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(
            api_key=cfg.api_key,
            model=cfg.model or "gpt-4o",
            base_url=cfg.base_url,
        )
    elif name == "ollama":
        from .ollama import OllamaProvider

        return OllamaProvider(
            model=cfg.model or "qwen2.5",
            base_url=cfg.base_url,
            api_key=cfg.api_key or "ollama",
        )
    else:
        raise ValueError(f"Unknown provider: {cfg.provider!r}")
