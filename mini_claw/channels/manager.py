"""Channel registry and lifecycle manager."""

from __future__ import annotations

from typing import Any

from mini_claw.channels.base import Channel
from mini_claw.config import AppConfig, ChannelConfig


CHANNEL_REGISTRY: dict[str, type[Channel]] = {}


def register_channel(channel_type: str, cls: type[Channel]) -> None:
    CHANNEL_REGISTRY[channel_type] = cls


class ChannelManager:
    """Instantiate, wire, and route channels."""

    def __init__(self, config: AppConfig, gateway: Any | None = None) -> None:
        self._config = config
        self._gateway = gateway
        self._channels: dict[str, Channel] = {}

    def set_gateway(self, gateway: Any) -> None:
        self._gateway = gateway
        for channel in self._channels.values():
            self._wire(channel)

    def _channel_configs(self) -> list[ChannelConfig]:
        if self._config.channels:
            return self._config.channels
        feishu = self._config.channels_feishu
        return [
            ChannelConfig(
                name="feishu",
                type="feishu",
                enabled=feishu.enabled,
                options={
                    "app_id": feishu.app_id,
                    "app_secret": feishu.app_secret,
                },
            )
        ]

    def _wire(self, channel: Channel) -> None:
        if self._gateway is None:
            return
        channel.on_message = self._gateway.handle_message
        channel.on_card_action = self._gateway.handle_card_action

    def _create_channel(self, cfg: ChannelConfig) -> Channel:
        cls = CHANNEL_REGISTRY.get(cfg.type)
        if cls is None:
            raise ValueError(f"Unknown channel type: {cfg.type!r}")

        if cfg.type == "feishu":
            return cls(
                name=cfg.name,
                app_id=cfg.options.get("app_id", ""),
                app_secret=cfg.options.get("app_secret", ""),
            )
        if cfg.type == "cli":
            return cls(name=cfg.name)
        return cls(name=cfg.name, **cfg.options)

    def load_enabled(self) -> None:
        for cfg in self._channel_configs():
            if not cfg.enabled:
                continue
            if cfg.name in self._channels:
                continue
            channel = self._create_channel(cfg)
            self._wire(channel)
            self._channels[cfg.name] = channel

    async def start_all(self) -> None:
        self.load_enabled()
        for channel in self._channels.values():
            await channel.start()

    async def stop_all(self) -> None:
        for channel in reversed(list(self._channels.values())):
            await channel.stop()

    def register_instance(self, channel: Channel) -> None:
        self._wire(channel)
        self._channels[channel.name] = channel

    def get_channel(self, name: str) -> Channel:
        channel = self._channels.get(name)
        if channel is None:
            raise KeyError(f"Channel not found: {name}")
        return channel

    def has_channel(self, name: str) -> bool:
        return name in self._channels

    def list_channels(self) -> list[Channel]:
        return list(self._channels.values())


def _register_builtin_channels() -> None:
    from mini_claw.channels.cli_channel import CLIChannel
    from mini_claw.channels.feishu import FeishuChannel

    register_channel("feishu", FeishuChannel)
    register_channel("cli", CLIChannel)


_register_builtin_channels()
