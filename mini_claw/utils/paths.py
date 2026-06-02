"""Path safety helpers: workspace containment and sensitive-file rejection.

Two chokepoints used by every file-I/O tool:

- ``ensure_inside(path, base)``: expands ``~``, joins relative paths to
  ``base``, calls ``.resolve()`` (normalizing ``..`` and symlinks), and
  raises ``ValueError`` if the result is not under ``base``.

- ``assert_not_sensitive(path)``: rejects paths whose name or any segment
  matches a known-sensitive pattern (``.env``, ``id_rsa``, ``*.pem``,
  ``.ssh/*``, ``.git/config``, ``*_token*``, ``*secret*``…). Matching is
  case-insensitive so it works on NTFS too.

These are *defense in depth* on top of the permission system — even if a
permission decision goes wrong, file tools can never reach outside the
workspace or touch a credential file.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path


_SENSITIVE_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "id_rsa",
    "id_rsa.*",
    "id_ed25519",
    "id_ed25519.*",
    "*.key",
    "credentials.json",
    "*.crt",
    "*_token*",
    "*secret*",
    "*.htpasswd",
    "secrets.yaml",
    "secret.yaml",
    "secrets.yml",
    "secret.yml",
)

# Path segments (case-insensitive) that must never appear together in
# the resolved path. Each tuple is matched as a contiguous slice.
_SENSITIVE_SEGMENTS: tuple[tuple[str, ...], ...] = (
    (".ssh",),               # any file under .ssh/
    (".git", "config"),      # specifically .git/config
    (".aws",),               # ~/.aws/credentials etc.
    (".docker",),            # ~/.docker/config.json
    (".kube",),              # ~/.kube/config
    (".gnupg",),             # GPG keyrings
)


def ensure_inside(path: str | Path, base: Path) -> Path:
    """Resolve *path* and confirm it lives inside *base*.

    - Expands ``~`` on both sides.
    - Resolves ``..`` and symlinks (``Path.resolve(strict=False)``).
    - Relative inputs are joined to *base* before resolving.
    - Absolute inputs are resolved as-is, then containment-checked.

    Raises ``ValueError('path escapes workspace')`` if the resolved path is
    not under the resolved *base*.
    """
    base_resolved = Path(base).expanduser().resolve()
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = base_resolved / p
    resolved = p.resolve()
    try:
        resolved.relative_to(base_resolved)
    except ValueError as exc:
        raise ValueError(f"path escapes workspace: {path!r}") from exc
    return resolved


def assert_not_sensitive(path: str | Path) -> None:
    """Reject *path* if any segment looks like a credential or secret file.

    Matching is case-insensitive (Windows NTFS friendly). Works on both
    absolute and relative paths — caller usually passes a path *relative
    to the workspace* so workspace-prefix doesn't trigger false positives.

    Raises ``ValueError`` with the matched pattern in the message.
    """
    p = Path(path)
    parts_lower = tuple(part.lower() for part in p.parts)
    name_lower = p.name.lower()

    for pattern in _SENSITIVE_PATTERNS:
        # fnmatch.fnmatchcase is case-sensitive; we already lowercased.
        if fnmatch.fnmatchcase(name_lower, pattern):
            raise ValueError(
                f"path matches sensitive pattern {pattern!r}: {path}"
            )
        for seg in parts_lower:
            if fnmatch.fnmatchcase(seg, pattern):
                raise ValueError(
                    f"path segment matches sensitive pattern {pattern!r}: {path}"
                )

    for seq in _SENSITIVE_SEGMENTS:
        n = len(seq)
        for i in range(len(parts_lower) - n + 1):
            if parts_lower[i : i + n] == seq:
                raise ValueError(
                    f"path contains sensitive segment {'/'.join(seq)!r}: {path}"
                )
