"""Tests for path sandbox helpers (mini_claw/utils/paths.py)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mini_claw.utils.paths import assert_not_sensitive, ensure_inside


# ---------------------------------------------------------------------------
# ensure_inside
# ---------------------------------------------------------------------------


def test_ensure_inside_relative_path(tmp_path: Path) -> None:
    result = ensure_inside("subdir/file.txt", tmp_path)
    assert result == (tmp_path / "subdir" / "file.txt").resolve()


def test_ensure_inside_absolute_inside(tmp_path: Path) -> None:
    target = tmp_path / "ok.txt"
    result = ensure_inside(str(target), tmp_path)
    assert result == target.resolve()


def test_ensure_inside_rejects_absolute_outside(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    with pytest.raises(ValueError, match="escapes workspace"):
        ensure_inside(str(outside), tmp_path)


def test_ensure_inside_rejects_dotdot_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes workspace"):
        ensure_inside("../../etc/passwd", tmp_path)


def test_ensure_inside_rejects_system_path(tmp_path: Path) -> None:
    sentinel = "C:\\Windows\\System32\\drivers\\etc\\hosts" if os.name == "nt" else "/etc/passwd"
    with pytest.raises(ValueError, match="escapes workspace"):
        ensure_inside(sentinel, tmp_path)


def test_ensure_inside_handles_nonexistent_leaf(tmp_path: Path) -> None:
    """Path doesn't have to exist yet — write_file uses this to create new files."""
    result = ensure_inside("new/dir/file.txt", tmp_path)
    assert result == (tmp_path / "new" / "dir" / "file.txt").resolve()


# ---------------------------------------------------------------------------
# assert_not_sensitive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.production",
        ".env.local",
        "server.pem",
        "id_rsa",
        "id_rsa.pub",
        "id_ed25519",
        "app.key",
        "credentials.json",
        "cert.crt",
        ".ssh/known_hosts",
        ".ssh/id_rsa",
        ".git/config",
        "github_token.txt",
        "api_token",
        "my_secret_value.yaml",
        "secrets.yaml",
        "secret.yml",
        ".htpasswd",
        ".aws/credentials",
        ".docker/config.json",
        ".kube/config",
        # Case-insensitivity (NTFS):
        ".ENV",
        "ID_RSA",
        ".SSH/KNOWN_HOSTS",
        # Nested under a workspace:
        "subdir/.env",
        "config/credentials.json",
    ],
)
def test_assert_not_sensitive_blocks(path: str) -> None:
    with pytest.raises(ValueError):
        assert_not_sensitive(path)


@pytest.mark.parametrize(
    "path",
    [
        "main.py",
        "README.md",
        "src/app.ts",
        "test.txt",
        "config/app.yaml",      # not "secrets.yaml"
        "data/users.json",
        "envvars.txt",          # superset of .env but not the file
        "pemfile.txt",
        "envoy.conf",
    ],
)
def test_assert_not_sensitive_allows(path: str) -> None:
    assert_not_sensitive(path)  # should not raise
