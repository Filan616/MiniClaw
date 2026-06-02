"""PluginManager skeleton with conservative safety checks."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from mini_claw.audit.logger import SecurityAuditLogger
from mini_claw.permissions.levels import L0, L1, L2, L3, L4
from mini_claw.plugins.base import PluginContext


VALID_PLUGIN_TYPES = {"tool", "channel", "provider", "hook"}
VALID_PERMISSIONS = {L0, L1, L2, L3, L4}
FORBIDDEN_MODULE_PREFIXES = {"subprocess", "socket", "urllib", "requests", "httpx"}
FORBIDDEN_CALLS = {
    "open",
    "exec",
    "eval",
    "__import__",
    "os.system",
}


class PluginManager:
    """Discover, install, enable, audit, and load local plugins."""

    def __init__(
        self,
        plugins_dir: Path,
        registry: Any,
        channel_manager: Any,
        provider_manager: Any,
        storage: Any,
        audit_logger: Any | None = None,
    ) -> None:
        self._plugins_dir = plugins_dir
        self._registry = registry
        self._channel_manager = channel_manager
        self._provider_manager = provider_manager
        self._storage = storage
        self._audit_logger = audit_logger or SecurityAuditLogger(storage)
        self._plugins_dir.mkdir(parents=True, exist_ok=True)

    def discover(self) -> list[dict[str, Any]]:
        manifests = []
        for child in sorted(self._plugins_dir.iterdir()):
            if not child.is_dir():
                continue
            manifest_path = child / "plugin.yaml"
            if manifest_path.exists():
                manifests.append(self._read_manifest(manifest_path))
        return manifests

    def install(self, source: Path | str) -> dict[str, Any]:
        source_str = str(source)
        if source_str.startswith(("http://", "https://")):
            raise ValueError("plugins install only accepts local directories")
        source_path = Path(source).resolve()
        if not source_path.is_dir():
            raise ValueError(f"Plugin source is not a directory: {source_path}")

        manifest = self._read_manifest(source_path / "plugin.yaml")
        name = manifest["name"]
        target = self._plugins_dir / name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source_path, target)
        manifest = self._read_manifest(target / "plugin.yaml")
        manifest_hash = self._compute_hash(target)
        now = int(time.time())
        self._upsert_plugin_row(
            manifest,
            enabled=0,
            manifest_hash=manifest_hash,
            error_msg=None,
            installed_at=now,
        )
        return manifest

    def enable(self, name: str, confirmed: bool = False) -> dict[str, Any]:
        manifest = self._manifest_for(name)
        if not confirmed:
            return {"requires_confirmation": True, "manifest": manifest}

        manifest_hash = self._compute_hash(self._plugins_dir / name)
        now = int(time.time())
        self._storage.execute(
            "UPDATE plugins SET enabled=1, enabled_at=?, manifest_hash=?, "
            "manifest_json=?, declared_permissions=?, error_msg=NULL WHERE name=?",
            (
                now,
                manifest_hash,
                json.dumps(manifest),
                json.dumps(manifest.get("permissions", [])),
                name,
            ),
        )
        self._audit_logger.log_security_event(
            "plugin_enabled",
            {
                "name": name,
                "manifest_hash": manifest_hash,
                "declared_permissions": manifest.get("permissions", []),
            },
        )
        return {"requires_confirmation": False, "manifest": manifest}

    def disable(self, name: str) -> bool:
        cur = self._storage.execute(
            "UPDATE plugins SET enabled=0 WHERE name=?", (name,)
        )
        return cur.rowcount > 0

    def load(self, name: str) -> bool:
        plugin_dir = self._plugins_dir / name
        try:
            manifest = self._manifest_for(name)
            self._validate_manifest(manifest)
            entry_path = self._entry_path(plugin_dir, manifest)
            issues = self._audit_static(entry_path)
            if issues:
                raise RuntimeError("; ".join(issues))

            manifest_hash = self._compute_hash(plugin_dir)
            declared_hash = (
                (manifest.get("integrity") or {}).get("sha256")
                if isinstance(manifest.get("integrity"), dict)
                else None
            )
            error_msg = None
            if declared_hash and declared_hash != manifest_hash:
                error_msg = f"integrity mismatch: declared={declared_hash} actual={manifest_hash}"

            module = self._import_entry(name, entry_path)
            ctx = PluginContext(
                manifest=manifest,
                declared_permissions=list(manifest.get("permissions") or []),
                workspace_dir=plugin_dir,
                storage=self._storage,
            )
            self._call_registers(module, ctx)
            self._upsert_plugin_row(
                manifest,
                enabled=1 if manifest.get("enabled") else self._enabled_from_db(name),
                manifest_hash=manifest_hash,
                error_msg=error_msg,
                last_loaded_at=int(time.time()),
            )
            return True
        except Exception as exc:
            self._record_error(name, str(exc))
            return False

    def load_enabled(self) -> None:
        rows = self._storage.fetchall(
            "SELECT name FROM plugins WHERE enabled=1 ORDER BY name"
        )
        for row in rows:
            self.load(row["name"])

    def audit(self) -> list[dict[str, Any]]:
        results = []
        for manifest in self.discover():
            name = manifest["name"]
            plugin_dir = self._plugins_dir / name
            actual = self._compute_hash(plugin_dir)
            declared = None
            if isinstance(manifest.get("integrity"), dict):
                declared = manifest["integrity"].get("sha256")
            results.append(
                {
                    "name": name,
                    "declared": declared,
                    "actual": actual,
                    "matches": declared in (None, actual),
                    "static_issues": self._audit_static(self._entry_path(plugin_dir, manifest)),
                }
            )
        return results

    def list_plugins(self) -> list[dict[str, Any]]:
        return self._storage.fetchall(
            "SELECT name, version, enabled, manifest_hash, error_msg, installed_at, enabled_at "
            "FROM plugins ORDER BY name"
        )

    def inspect(self, name: str) -> dict[str, Any]:
        manifest = self._manifest_for(name)
        plugin_dir = self._plugins_dir / name
        return {
            "manifest": manifest,
            "row": self._storage.fetchone("SELECT * FROM plugins WHERE name=?", (name,)),
            "static_issues": self._audit_static(self._entry_path(plugin_dir, manifest)),
            "hash": self._compute_hash(plugin_dir),
        }

    def _read_manifest(self, manifest_path: Path) -> dict[str, Any]:
        if not manifest_path.exists():
            raise ValueError(f"Missing plugin.yaml: {manifest_path}")
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        if not isinstance(manifest, dict):
            raise ValueError("plugin.yaml must be a mapping")
        self._validate_manifest(manifest)
        return manifest

    def _validate_manifest(self, manifest: dict[str, Any]) -> None:
        for key in ("name", "version", "type", "entry"):
            if not manifest.get(key):
                raise ValueError(f"plugin manifest missing {key}")
        if manifest["type"] not in VALID_PLUGIN_TYPES:
            raise ValueError(f"invalid plugin type: {manifest['type']}")
        entry = str(manifest["entry"])
        if entry.startswith(("http://", "https://")) or Path(entry).is_absolute():
            raise ValueError("plugin entry must be a local relative module/path")
        permissions = list(manifest.get("permissions") or [])
        invalid = sorted(set(permissions) - VALID_PERMISSIONS)
        if invalid:
            raise ValueError(f"invalid plugin permissions: {invalid}")

    def _manifest_for(self, name: str) -> dict[str, Any]:
        return self._read_manifest(self._plugins_dir / name / "plugin.yaml")

    def _entry_path(self, plugin_dir: Path, manifest: dict[str, Any]) -> Path:
        entry = str(manifest["entry"])
        if entry.endswith(".py"):
            path = plugin_dir / entry
        else:
            path = plugin_dir / f"{entry}.py"
        if not path.exists():
            raise ValueError(f"Plugin entry not found: {path}")
        return path

    def _audit_static(self, entry_path: Path) -> list[str]:
        tree = ast.parse(entry_path.read_text(encoding="utf-8"), filename=str(entry_path))
        issues: list[str] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] in FORBIDDEN_MODULE_PREFIXES:
                            issues.append(f"forbidden top-level import {alias.name}")
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if module.split(".")[0] in FORBIDDEN_MODULE_PREFIXES:
                        issues.append(f"forbidden top-level import {module}")
                continue
            for call in [n for n in ast.walk(node) if isinstance(n, ast.Call)]:
                name = self._call_name(call.func)
                if name in FORBIDDEN_CALLS or name.split(".")[0] in FORBIDDEN_MODULE_PREFIXES:
                    issues.append(f"forbidden top-level call {name}")
        return issues

    def _call_name(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = self._call_name(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        return ""

    def _compute_hash(self, plugin_dir: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(p for p in plugin_dir.rglob("*") if p.is_file()):
            rel = path.relative_to(plugin_dir).as_posix()
            digest.update(rel.encode())
            digest.update(b"\0")
            if rel == "plugin.yaml":
                manifest = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if isinstance(manifest, dict) and isinstance(manifest.get("integrity"), dict):
                    manifest["integrity"] = dict(manifest["integrity"])
                    manifest["integrity"]["sha256"] = ""
                digest.update(
                    yaml.safe_dump(manifest, sort_keys=True).encode()
                )
            else:
                digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    def _import_entry(self, name: str, entry_path: Path) -> Any:
        module_name = f"mini_claw_plugin_{name}_{hashlib.md5(str(entry_path).encode()).hexdigest()}"
        spec = importlib.util.spec_from_file_location(module_name, entry_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot import plugin entry {entry_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module

    def _call_registers(self, module: Any, ctx: PluginContext) -> None:
        for name, target in (
            ("register_tools", self._registry),
            ("register_channels", self._channel_manager),
            ("register_providers", self._provider_manager),
            ("register_hooks", None),
        ):
            func = getattr(module, name, None)
            if callable(func):
                func(target, ctx)

    def _enabled_from_db(self, name: str) -> int:
        row = self._storage.fetchone("SELECT enabled FROM plugins WHERE name=?", (name,))
        return int(row["enabled"]) if row else 0

    def _upsert_plugin_row(
        self,
        manifest: dict[str, Any],
        enabled: int,
        manifest_hash: str,
        error_msg: str | None,
        installed_at: int | None = None,
        last_loaded_at: int | None = None,
    ) -> None:
        now = int(time.time())
        installed_at = installed_at or now
        self._storage.execute(
            "INSERT INTO plugins "
            "(name, version, enabled, manifest_json, manifest_hash, declared_permissions, "
            "error_msg, last_loaded_at, installed_at, enabled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT(name) DO UPDATE SET "
            "version=excluded.version, manifest_json=excluded.manifest_json, "
            "manifest_hash=excluded.manifest_hash, declared_permissions=excluded.declared_permissions, "
            "error_msg=excluded.error_msg, last_loaded_at=excluded.last_loaded_at, "
            "installed_at=COALESCE(plugins.installed_at, excluded.installed_at), "
            "enabled=CASE WHEN excluded.enabled IS NULL THEN plugins.enabled ELSE excluded.enabled END",
            (
                manifest["name"],
                manifest.get("version", ""),
                enabled,
                json.dumps(manifest),
                manifest_hash,
                json.dumps(manifest.get("permissions", [])),
                error_msg,
                last_loaded_at,
                installed_at,
            ),
        )

    def _record_error(self, name: str, error_msg: str) -> None:
        self._storage.execute(
            "INSERT INTO plugins (name, enabled, error_msg, installed_at) "
            "VALUES (?, 0, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET error_msg=excluded.error_msg",
            (name, error_msg, int(time.time())),
        )
