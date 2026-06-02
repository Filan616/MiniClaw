"""Tests for Plugin 热摘除 (Phase B.5)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mini_claw.plugins.manager import PluginManager
from mini_claw.storage.db import Database
from mini_claw.tools.registry import Tool, ToolRegistry


def _make_plugin_with_tool(plugin_dir: Path, tool_name: str = "echo") -> str:
    """Create a plugin that registers a single L0 tool."""
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.py").write_text(
        f"""
async def _echo(text: str = '', ctx=None):
    return text

def register_tools(registry, ctx):
    from mini_claw.tools.registry import Tool
    tool = Tool(
        name='{tool_name}',
        description='Echo input',
        input_schema={{
            'type': 'object',
            'properties': {{'text': {{'type': 'string'}}}},
            'required': ['text'],
        }},
        handler=_echo,
        permission_level='L0',
    )
    registry.register(tool)
""",
        encoding="utf-8",
    )
    manifest = {
        "name": plugin_dir.name,
        "version": "0.1.0",
        "type": "tool",
        "entry": "plugin",
        "permissions": ["L0"],
        "enabled": False,
    }
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )
    return plugin_dir.name


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "plugin_hot_remove.db")


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry()


@pytest.fixture
def plugins_dir(tmp_path: Path) -> Path:
    d = tmp_path / "plugins"
    d.mkdir()
    return d


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    d = tmp_path / "sources"
    d.mkdir()
    return d


def test_registry_unregister_returns_true_when_present(registry: ToolRegistry):
    """Phase B.5: unregister returns True when tool exists."""
    async def handler(ctx=None):
        return "ok"
    registry.register(Tool(
        name="test", description="t", input_schema={"type": "object"}, handler=handler
    ))
    assert registry.unregister("test") is True
    assert registry.get("test") is None


def test_registry_unregister_returns_false_when_missing(registry: ToolRegistry):
    """Phase B.5: unregister returns False when tool not in registry."""
    assert registry.unregister("nonexistent") is False


def test_registry_version_increments_on_register(registry: ToolRegistry):
    """Phase B.5: version increments on each register/unregister."""
    async def handler(ctx=None):
        return "ok"
    initial = registry.version

    registry.register(Tool(
        name="t1", description="t", input_schema={"type": "object"}, handler=handler
    ))
    assert registry.version == initial + 1

    registry.unregister("t1")
    assert registry.version == initial + 2


def test_plugin_disable_removes_registered_tools(
    plugins_dir: Path, source_dir: Path, db: Database, registry: ToolRegistry
):
    """Phase B.5: plugin disable hot-removes its registered tools."""
    name = _make_plugin_with_tool(source_dir / "test_echo", tool_name="echo_tool")
    manager = PluginManager(
        plugins_dir=plugins_dir,
        registry=registry,
        channel_manager=None,
        provider_manager=None,
        storage=db,
        integrity_mode="warn",  # Skip integrity for this test
    )
    manager.install(source_dir / name)
    manager.enable(name, confirmed=True)

    # Load plugin (this should register the tool)
    success = manager.load(name)
    assert success
    assert registry.get("echo_tool") is not None

    # Disable should hot-remove the tool
    manager.disable(name)
    assert registry.get("echo_tool") is None


def test_disable_audit_event_recorded(
    plugins_dir: Path, source_dir: Path, db: Database, registry: ToolRegistry
):
    """Phase B.5: disable triggers plugin_disabled audit event."""
    name = _make_plugin_with_tool(source_dir / "audit_test", tool_name="audit_echo")
    manager = PluginManager(
        plugins_dir=plugins_dir,
        registry=registry,
        channel_manager=None,
        provider_manager=None,
        storage=db,
        integrity_mode="warn",
    )
    manager.install(source_dir / name)
    manager.enable(name, confirmed=True)
    manager.load(name)
    manager.disable(name)

    rows = db.fetchall(
        "SELECT event_type FROM security_audit WHERE event_type='plugin_disabled'"
    )
    assert len(rows) >= 1


def test_disable_idempotent_for_unknown_plugin(
    plugins_dir: Path, db: Database, registry: ToolRegistry
):
    """Phase B.5: disabling an unknown plugin doesn't crash."""
    manager = PluginManager(
        plugins_dir=plugins_dir,
        registry=registry,
        channel_manager=None,
        provider_manager=None,
        storage=db,
    )
    # Should return False but not raise
    result = manager.disable("nonexistent_plugin")
    assert result is False
