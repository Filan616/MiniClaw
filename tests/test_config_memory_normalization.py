"""Phase 9 ct-1: Verify top-level memory config normalization covers all runtime paths.

Tests:
1. Direct RagConfig() instantiation provides rag.memory property accessor
2. AppConfig programmatic construction propagates memory: to rag.memory_control/maintenance
3. YAML load path propagates memory: to rag.memory_control/maintenance
4. Top-level memory: values override rag.memory_control/maintenance when both present
5. RagManager can access config.memory_control and config.memory_maintenance
"""

from __future__ import annotations

import tempfile
import yaml
from pathlib import Path

import pytest

from mini_claw.config import (
    AppConfig,
    RagConfig,
    MemoryControlConfig,
    MemoryMaintenanceConfig,
    load_config,
)
from mini_claw.permissions.policy import PermissionPolicy
from mini_claw.rag.manager import RagManager
from mini_claw.storage.db import Database


def test_rag_config_provides_memory_property():
    """Direct RagConfig() should provide rag.memory property accessor."""
    cfg = RagConfig()
    assert hasattr(cfg, "memory")
    assert cfg.memory.control.allow_hard_delete == False
    assert cfg.memory.maintenance.dupe_threshold == 0.85


def test_app_config_programmatic_propagation():
    """AppConfig programmatic construction should propagate memory: to rag."""
    cfg = AppConfig()
    # Set top-level memory values
    cfg.memory.control.allow_hard_delete = True
    cfg.memory.control.batch_approve_max = 100
    cfg.memory.maintenance.dupe_threshold = 0.95
    cfg.memory.maintenance.mode = "hybrid"

    # Trigger normalization
    cfg.model_post_init(None)

    # Verify propagation to rag.memory_control/maintenance
    assert cfg.rag.memory_control.allow_hard_delete == True
    assert cfg.rag.memory_control.batch_approve_max == 100
    assert cfg.rag.memory_maintenance.dupe_threshold == 0.95
    assert cfg.rag.memory_maintenance.mode == "hybrid"


def test_yaml_load_propagation():
    """YAML load should propagate top-level memory: to rag."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False, encoding='utf-8') as f:
        yaml_content = """
rag:
  enabled: true
  namespaces:
    memory_enabled: true

memory:
  control:
    allow_hard_delete: true
    batch_approve_max: 50
  maintenance:
    dupe_threshold: 0.92
    mode: hybrid
"""
        f.write(yaml_content)
        temp_path = f.name

    try:
        cfg = load_config(Path(temp_path))

        # Verify propagation
        assert cfg.rag.memory_control.allow_hard_delete == True
        assert cfg.rag.memory_control.batch_approve_max == 50
        assert cfg.rag.memory_maintenance.dupe_threshold == 0.92
        assert cfg.rag.memory_maintenance.mode == "hybrid"

        # Verify rag.memory property accessor works
        assert cfg.rag.memory.control.allow_hard_delete == True
        assert cfg.rag.memory.maintenance.dupe_threshold == 0.92
    finally:
        Path(temp_path).unlink()


def test_top_level_overrides_rag_level():
    """Top-level memory: should override rag.memory_control/maintenance when both present."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False, encoding='utf-8') as f:
        yaml_content = """
rag:
  memory_control:
    allow_hard_delete: false
    batch_approve_max: 20
  memory_maintenance:
    dupe_threshold: 0.85

memory:
  control:
    allow_hard_delete: true
    batch_approve_max: 50
  maintenance:
    dupe_threshold: 0.92
"""
        f.write(yaml_content)
        temp_path = f.name

    try:
        cfg = load_config(Path(temp_path))

        # Top-level wins
        assert cfg.rag.memory_control.allow_hard_delete == True
        assert cfg.rag.memory_control.batch_approve_max == 50
        assert cfg.rag.memory_maintenance.dupe_threshold == 0.92
    finally:
        Path(temp_path).unlink()


def test_rag_manager_accesses_memory_config(tmp_path: Path):
    """RagManager should access memory config via config.memory_control/maintenance."""
    cfg = RagConfig()
    cfg.enabled = True
    cfg.namespaces.memory_enabled = True
    cfg.memory_control.batch_approve_max = 75
    cfg.memory_maintenance.dupe_threshold = 0.88

    storage = Database(tmp_path / "test.db")
    policy = PermissionPolicy(AppConfig().permissions)
    manager = RagManager(storage, cfg, policy)

    # Verify manager can access config attributes
    assert manager.config.memory_control.batch_approve_max == 75
    assert manager.config.memory_maintenance.dupe_threshold == 0.88

    # Verify manager can also access via .memory property
    assert manager.config.memory.control.batch_approve_max == 75
    assert manager.config.memory.maintenance.dupe_threshold == 0.88


def test_backward_compatibility_rag_only():
    """Configs without top-level memory: should still work (backward compat)."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False, encoding='utf-8') as f:
        yaml_content = """
rag:
  enabled: true
  memory_control:
    allow_hard_delete: true
  memory_maintenance:
    dupe_threshold: 0.90
"""
        f.write(yaml_content)
        temp_path = f.name

    try:
        cfg = load_config(Path(temp_path))

        # Values from rag.memory_control/maintenance should be preserved
        assert cfg.rag.memory_control.allow_hard_delete == True
        assert cfg.rag.memory_maintenance.dupe_threshold == 0.90

        # Property accessor should work
        assert cfg.rag.memory.control.allow_hard_delete == True
        assert cfg.rag.memory.maintenance.dupe_threshold == 0.90
    finally:
        Path(temp_path).unlink()


def test_empty_config_has_defaults():
    """Empty config should have sensible defaults."""
    cfg = AppConfig()

    # Defaults from MemoryControlConfig
    assert cfg.rag.memory_control.allow_hard_delete == False
    assert cfg.rag.memory_control.batch_approve_max == 20

    # Defaults from MemoryMaintenanceConfig
    assert cfg.rag.memory_maintenance.dupe_threshold == 0.85
    assert cfg.rag.memory_maintenance.mode == "auto"

    # Property accessor works
    assert cfg.rag.memory.control.allow_hard_delete == False
    assert cfg.rag.memory.maintenance.dupe_threshold == 0.85
