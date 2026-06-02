"""Tests for Plugin Integrity 强制拒绝 (Phase A.2)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mini_claw.plugins.manager import PluginManager
from mini_claw.storage.db import Database


def _make_plugin(plugin_dir: Path, integrity_sha256: str | None = None) -> str:
    """Create a minimal plugin in the given directory.

    Returns the plugin name.
    """
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.py").write_text(
        "def register_tools(registry, ctx):\n    pass\n",
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
    if integrity_sha256 is not None:
        manifest["integrity"] = {"sha256": integrity_sha256}
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )
    return plugin_dir.name


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "plugins_integrity.db")


@pytest.fixture
def plugins_dir(tmp_path: Path) -> Path:
    d = tmp_path / "plugins"
    d.mkdir()
    return d


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    """Separate directory where plugin sources are created (so install() can copy from it)."""
    d = tmp_path / "sources"
    d.mkdir()
    return d


def _make_manager(plugins_dir: Path, db: Database, integrity_mode: str = "strict") -> PluginManager:
    return PluginManager(
        plugins_dir=plugins_dir,
        registry=type("R", (), {"register": lambda self, t: None})(),
        channel_manager=None,
        provider_manager=None,
        storage=db,
        integrity_mode=integrity_mode,
    )


def test_enable_rejects_hash_mismatch_in_strict_mode(plugins_dir: Path, source_dir: Path, db: Database):
    """Phase A.2: enable with declared hash mismatch should fail in strict mode."""
    name = _make_plugin(source_dir / "test_plugin", integrity_sha256="0" * 64)
    manager = _make_manager(plugins_dir, db, integrity_mode="strict")
    manager.install(source_dir / name)

    # Enable without confirm — returns requires_confirmation
    result = manager.enable(name, confirmed=False)
    assert result["requires_confirmation"] is True

    # Enable with confirm should fail due to hash mismatch
    with pytest.raises(RuntimeError, match="integrity check failed"):
        manager.enable(name, confirmed=True)


def test_enable_force_bypasses_check_with_audit(plugins_dir: Path, source_dir: Path, db: Database):
    """Phase A.2: --force allows enable but logs audit."""
    name = _make_plugin(source_dir / "test_plugin", integrity_sha256="0" * 64)
    manager = _make_manager(plugins_dir, db, integrity_mode="strict")
    manager.install(source_dir / name)

    # Force enable should succeed
    result = manager.enable(name, confirmed=True, force=True)
    assert result["requires_confirmation"] is False
    assert result["integrity_ok"] is False

    # Audit event should exist
    audit_rows = db.fetchall(
        "SELECT event_type, details FROM security_audit "
        "WHERE event_type='plugin_integrity_mismatch'"
    )
    assert len(audit_rows) >= 1


def test_enable_warn_mode_allows_mismatch(plugins_dir: Path, source_dir: Path, db: Database):
    """Phase A.2: warn mode allows enable but still logs audit."""
    name = _make_plugin(source_dir / "test_plugin", integrity_sha256="0" * 64)
    manager = _make_manager(plugins_dir, db, integrity_mode="warn")
    manager.install(source_dir / name)

    # Warn mode allows enable without --force
    result = manager.enable(name, confirmed=True, force=False)
    assert result["requires_confirmation"] is False
    assert result["integrity_ok"] is False

    # Audit should still record
    audit_rows = db.fetchall(
        "SELECT event_type FROM security_audit "
        "WHERE event_type='plugin_integrity_mismatch'"
    )
    assert len(audit_rows) >= 1


def test_load_rejects_hash_mismatch_in_strict_mode(plugins_dir: Path, source_dir: Path, db: Database):
    """Phase A.2: load with hash mismatch should fail in strict mode."""
    name = _make_plugin(source_dir / "test_plugin", integrity_sha256="0" * 64)
    manager = _make_manager(plugins_dir, db, integrity_mode="strict")
    manager.install(source_dir / name)

    # load() catches the error and records to plugins.error_msg
    success = manager.load(name)
    assert success is False

    # Check error_msg was recorded
    row = db.fetchone("SELECT error_msg FROM plugins WHERE name=?", (name,))
    assert "integrity check failed" in (row["error_msg"] or "")


def test_enable_with_correct_hash_succeeds(plugins_dir: Path, source_dir: Path, db: Database):
    """Phase A.2: enable with correct hash should work."""
    # Create plugin with placeholder integrity field (empty sha256)
    name = _make_plugin(source_dir / "test_plugin", integrity_sha256="")
    manager = _make_manager(plugins_dir, db, integrity_mode="strict")
    manager.install(source_dir / name)

    # Compute the actual hash of the installed plugin
    # _compute_hash strips integrity.sha256 to "" before hashing, so
    # the hash is the same whether sha256 is empty or filled in
    actual_hash = manager._compute_hash(plugins_dir / name)

    # Update manifest in installed plugin with correct hash
    plugin_yaml = plugins_dir / name / "plugin.yaml"
    manifest = yaml.safe_load(plugin_yaml.read_text(encoding="utf-8"))
    manifest["integrity"] = {"sha256": actual_hash}
    plugin_yaml.write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )

    # Now hash matches the declared, enable should succeed
    result = manager.enable(name, confirmed=True)
    assert result["integrity_ok"] is True
