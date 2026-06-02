from pathlib import Path

from mini_claw.channels.manager import ChannelManager
from mini_claw.config import AppConfig
from mini_claw.plugins.manager import PluginManager
from mini_claw.providers.manager import ProviderManager
from mini_claw.storage.db import Database
from mini_claw.tools.registry import ToolRegistry


def _manager(tmp_path: Path) -> PluginManager:
    db = Database(tmp_path / "plugins.db")
    return PluginManager(
        plugins_dir=tmp_path / "installed",
        registry=ToolRegistry(),
        channel_manager=ChannelManager(AppConfig()),
        provider_manager=ProviderManager(AppConfig()),
        storage=db,
    )


def _write_plugin(base: Path, name: str, code: str) -> Path:
    plugin_dir = base / name
    plugin_dir.mkdir()
    plugin_dir.joinpath("plugin.yaml").write_text(
        f"""name: {name}
version: 0.1.0
description: test plugin
author: tester
type: tool
entry: plugin
permissions: [L0]
enabled: false
integrity:
  sha256: ""
""",
        encoding="utf-8",
    )
    plugin_dir.joinpath("plugin.py").write_text(code, encoding="utf-8")
    return plugin_dir


def test_plugin_install_does_not_enable(tmp_path: Path):
    source = _write_plugin(tmp_path, "p", "def register_tools(registry, ctx):\n    pass\n")
    manager = _manager(tmp_path)

    manager.install(source)
    row = manager.list_plugins()[0]

    assert row["name"] == "p"
    assert row["enabled"] == 0


def test_plugin_enable_writes_audit_event(tmp_path: Path):
    source = _write_plugin(tmp_path, "p", "def register_tools(registry, ctx):\n    pass\n")
    manager = _manager(tmp_path)
    manager.install(source)

    result = manager.enable("p", confirmed=False)
    assert result["requires_confirmation"] is True
    manager.enable("p", confirmed=True)

    rows = manager._storage.fetchall(
        "SELECT event_type FROM security_audit WHERE event_type='plugin_enabled'"
    )
    assert rows == [{"event_type": "plugin_enabled"}]


def test_static_scan_rejects_top_level_os_system(tmp_path: Path):
    source = _write_plugin(
        tmp_path,
        "bad",
        "import os\nos.system('echo bad')\ndef register_tools(registry, ctx):\n    pass\n",
    )
    manager = _manager(tmp_path)
    manager.install(source)
    manager.enable("bad", confirmed=True)

    assert manager.load("bad") is False
    row = manager._storage.fetchone("SELECT error_msg FROM plugins WHERE name='bad'")
    assert "forbidden top-level call os.system" in row["error_msg"]


def test_plugin_audit_detects_hash_drift(tmp_path: Path):
    source = _write_plugin(tmp_path, "p", "def register_tools(registry, ctx):\n    pass\n")
    manager = _manager(tmp_path)
    manifest = manager.install(source)
    installed = manager._plugins_dir / manifest["name"]
    actual = manager._compute_hash(installed)
    yaml_text = installed.joinpath("plugin.yaml").read_text(encoding="utf-8")
    installed.joinpath("plugin.yaml").write_text(
        yaml_text.replace('sha256: ""', f"sha256: {actual}"),
        encoding="utf-8",
    )
    installed.joinpath("plugin.py").write_text(
        "def register_tools(registry, ctx):\n    x = 1\n",
        encoding="utf-8",
    )

    audit = manager.audit()[0]
    assert audit["matches"] is False


def test_example_echo_plugin_loads_tool(tmp_path: Path):
    source = Path("plugins/example_echo").resolve()
    manager = _manager(tmp_path)
    manifest = manager.install(source)
    manager.enable(manifest["name"], confirmed=True)

    assert manager.load("example_echo") is True
    assert manager._registry.get("echo") is not None
