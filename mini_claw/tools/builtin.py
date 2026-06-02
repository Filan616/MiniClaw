"""Built-in tools: shell, file I/O, and directory listing."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..utils.paths import assert_not_sensitive, ensure_inside
from .registry import Tool, ToolContext


def _bypass_resolve(path: str, workspace: Path) -> Path:
    """In bypass mode: relative paths join to workspace, absolute paths pass through.

    This gives the agent a default working directory (the workspace) for convenience,
    while still allowing it to specify absolute system paths when needed.
    """
    p = Path(path).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (workspace / p).resolve()


# ---------------------------------------------------------------------------
# run_shell
# ---------------------------------------------------------------------------

async def _run_shell(command: str, *, ctx: ToolContext) -> str:
    """Execute a shell command with timeout support."""
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(ctx.workspace_dir),
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=ctx.timeout
        )
    except asyncio.TimeoutError:
        proc.kill()  # type: ignore[union-attr]
        return f"[ERROR] Command timed out after {ctx.timeout}s"

    output_parts: list[str] = []
    if stdout:
        output_parts.append(stdout.decode(errors="replace"))
    if stderr:
        output_parts.append(f"[STDERR]\n{stderr.decode(errors='replace')}")
    if proc.returncode != 0:
        output_parts.append(f"[EXIT CODE] {proc.returncode}")

    return "\n".join(output_parts) if output_parts else "(no output)"


TOOL_RUN_SHELL = Tool(
    name="run_shell",
    description="Execute a shell command in the workspace directory.",
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run"},
        },
        "required": ["command"],
    },
    handler=_run_shell,
    permission_level="L2",
)


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------

async def _read_file(path: str, *, ctx: ToolContext) -> str:
    """Read a file and return its content as a string."""
    try:
        if ctx.sandbox_mode == "bypass":
            file_path = _bypass_resolve(path, ctx.workspace_dir)
        else:
            file_path = ensure_inside(path, ctx.workspace_dir)
            assert_not_sensitive(file_path.relative_to(ctx.workspace_dir.resolve()))
    except ValueError as exc:
        return f"[ERROR] {exc}"
    if not file_path.is_file():
        return f"[ERROR] File not found: {file_path}"
    try:
        return file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"[ERROR] Cannot read file: {exc}"


TOOL_READ_FILE = Tool(
    name="read_file",
    description="Read the contents of a file.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path (relative to workspace or absolute)"},
        },
        "required": ["path"],
    },
    handler=_read_file,
    permission_level="L0",
)


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------

async def _write_file(path: str, content: str, *, ctx: ToolContext) -> str:
    """Write content to a file, creating parent directories as needed."""
    try:
        if ctx.sandbox_mode == "bypass":
            file_path = _bypass_resolve(path, ctx.workspace_dir)
        else:
            file_path = ensure_inside(path, ctx.workspace_dir)
            assert_not_sensitive(file_path.relative_to(ctx.workspace_dir.resolve()))
    except ValueError as exc:
        return f"[ERROR] {exc}"
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Written {len(content)} chars to {file_path}"
    except OSError as exc:
        return f"[ERROR] Cannot write file: {exc}"


TOOL_WRITE_FILE = Tool(
    name="write_file",
    description="Write content to a file (creates parent dirs if needed).",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path"},
            "content": {"type": "string", "description": "Content to write"},
        },
        "required": ["path", "content"],
    },
    handler=_write_file,
    permission_level="L1",
)


# ---------------------------------------------------------------------------
# list_directory
# ---------------------------------------------------------------------------

async def _list_directory(path: str = ".", *, ctx: ToolContext) -> str:
    """List contents of a directory."""
    try:
        if ctx.sandbox_mode == "bypass":
            dir_path = _bypass_resolve(path, ctx.workspace_dir)
        else:
            dir_path = ensure_inside(path, ctx.workspace_dir)
    except ValueError as exc:
        return f"[ERROR] {exc}"
    if not dir_path.is_dir():
        return f"[ERROR] Not a directory: {dir_path}"
    try:
        entries = sorted(dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        lines: list[str] = []
        for entry in entries:
            prefix = "d " if entry.is_dir() else "f "
            lines.append(f"{prefix}{entry.name}")
        return "\n".join(lines) if lines else "(empty directory)"
    except OSError as exc:
        return f"[ERROR] Cannot list directory: {exc}"


TOOL_LIST_DIRECTORY = Tool(
    name="list_directory",
    description="List files and subdirectories in a directory.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path", "default": "."},
        },
    },
    handler=_list_directory,
    permission_level="L0",
)


# ---------------------------------------------------------------------------
# All built-in tools for easy import
# ---------------------------------------------------------------------------

BUILTIN_TOOLS: list[Tool] = [
    TOOL_RUN_SHELL,
    TOOL_READ_FILE,
    TOOL_WRITE_FILE,
    TOOL_LIST_DIRECTORY,
]
