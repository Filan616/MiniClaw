"""Controlled Windows application launcher tool."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .registry import Tool, ToolContext


_SCRIPT_EXTENSIONS = {".bat", ".cmd", ".ps1", ".vbs", ".js", ".jse", ".wsf", ".wsh", ".url"}


@dataclass(frozen=True)
class AppSpec:
    id: str
    aliases: tuple[str, ...]
    exe_names: tuple[str, ...]
    common_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class AppCandidate:
    app_id: str
    path: str
    source: str


@dataclass(frozen=True)
class LinkTarget:
    target_path: str
    arguments: str = ""


APP_SPECS: dict[str, AppSpec] = {
    "wechat": AppSpec(
        id="wechat",
        aliases=("wechat", "weixin", "微信"),
        exe_names=("WeChat.exe",),
        common_paths=(
            r"%ProgramFiles%\Tencent\WeChat\WeChat.exe",
            r"%ProgramFiles(x86)%\Tencent\WeChat\WeChat.exe",
            r"%LOCALAPPDATA%\Tencent\WeChat\WeChat.exe",
        ),
    ),
    "wecom": AppSpec(
        id="wecom",
        aliases=("wecom", "企业微信", "wxwork", "企业微信客户端"),
        exe_names=("WXWork.exe",),
        common_paths=(
            r"%ProgramFiles%\WXWork\WXWork.exe",
            r"%ProgramFiles(x86)%\WXWork\WXWork.exe",
            r"%LOCALAPPDATA%\WXWork\WXWork.exe",
        ),
    ),
    "vscode": AppSpec(
        id="vscode",
        aliases=("vscode", "vs code", "visual studio code", "code", "代码"),
        exe_names=("Code.exe",),
        common_paths=(
            r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",
            r"%ProgramFiles%\Microsoft VS Code\Code.exe",
        ),
    ),
    "chrome": AppSpec(
        id="chrome",
        aliases=("chrome", "google chrome", "谷歌浏览器", "谷歌"),
        exe_names=("chrome.exe",),
        common_paths=(
            r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
            r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
            r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
        ),
    ),
    "edge": AppSpec(
        id="edge",
        aliases=("edge", "microsoft edge", "msedge", "浏览器"),
        exe_names=("msedge.exe",),
        common_paths=(
            r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
            r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        ),
    ),
    "notepad": AppSpec(
        id="notepad",
        aliases=("notepad", "记事本"),
        exe_names=("notepad.exe",),
        common_paths=(r"%WINDIR%\System32\notepad.exe",),
    ),
    "calculator": AppSpec(
        id="calculator",
        aliases=("calculator", "calc", "计算器"),
        exe_names=("calc.exe",),
        common_paths=(r"%WINDIR%\System32\calc.exe",),
    ),
    "powershell": AppSpec(
        id="powershell",
        aliases=("powershell", "pwsh", "power shell"),
        exe_names=("powershell.exe", "pwsh.exe"),
        common_paths=(
            r"%WINDIR%\System32\WindowsPowerShell\v1.0\powershell.exe",
            r"%ProgramFiles%\PowerShell\7\pwsh.exe",
        ),
    ),
    "windows_terminal": AppSpec(
        id="windows_terminal",
        aliases=("windows terminal", "terminal", "wt", "终端"),
        exe_names=("wt.exe", "WindowsTerminal.exe"),
    ),
    "pycharm": AppSpec(
        id="pycharm",
        aliases=("pycharm", "py charm"),
        exe_names=("pycharm64.exe", "pycharm.exe"),
        common_paths=(
            r"%ProgramFiles%\JetBrains\PyCharm Community Edition 2024.3\bin\pycharm64.exe",
            r"%ProgramFiles%\JetBrains\PyCharm Professional 2024.3\bin\pycharm64.exe",
        ),
    ),
    "git_bash": AppSpec(
        id="git_bash",
        aliases=("git bash", "git-bash", "bash"),
        exe_names=("git-bash.exe",),
        common_paths=(
            r"%ProgramFiles%\Git\git-bash.exe",
            r"%ProgramFiles(x86)%\Git\git-bash.exe",
        ),
    ),
}


def normalize_app_name(value: str) -> str | None:
    needle = _normalize_alias(value)
    if not needle:
        return None
    for spec in APP_SPECS.values():
        if needle in {_normalize_alias(alias) for alias in spec.aliases}:
            return spec.id
    return None


def _normalize_alias(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", value.strip().lower())


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _start_menu_dirs() -> list[Path]:
    dirs: list[Path] = []
    appdata = os.environ.get("APPDATA")
    program_data = os.environ.get("ProgramData")
    if appdata:
        dirs.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    if program_data:
        dirs.append(Path(program_data) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return dirs


def _link_name_matches(spec: AppSpec, link: Path) -> bool:
    stem = _normalize_alias(link.stem)
    aliases = {_normalize_alias(alias) for alias in spec.aliases}
    return stem in aliases


def resolve_lnk_target(link_path: Path) -> LinkTarget | None:
    """Resolve a Windows shortcut through WScript.Shell.

    The .lnk itself is never trusted. Callers must validate the resolved target.
    """
    script = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($args[0]);"
        "[pscustomobject]@{TargetPath=$s.TargetPath;Arguments=$s.Arguments}|"
        "ConvertTo-Json -Compress"
    )
    try:
        proc = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
                str(link_path),
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    target = str(data.get("TargetPath") or "").strip()
    args = str(data.get("Arguments") or "").strip()
    if not target:
        return None
    return LinkTarget(target_path=target, arguments=args)


def _validate_exe_target(
    spec: AppSpec,
    target_path: str | Path,
    *,
    source: str,
    arguments: str = "",
) -> AppCandidate | None:
    raw = str(target_path).strip().strip('"')
    if not raw or re.match(r"^[a-z]+://", raw, flags=re.IGNORECASE):
        return None
    path = Path(os.path.expandvars(raw))
    if path.suffix.lower() in _SCRIPT_EXTENSIONS:
        return None
    if path.suffix.lower() != ".exe":
        return None
    allowed_exes = {name.lower() for name in spec.exe_names}
    if path.name.lower() not in allowed_exes:
        return None
    # V1 does not accept command-line arguments from shortcuts. This prevents
    # trusted app aliases from opening URLs, scripts, or shell payloads.
    if arguments.strip():
        return None
    if not path.exists() and not shutil.which(str(path)):
        return None
    return AppCandidate(app_id=spec.id, path=str(path), source=source)


def _discover_from_start_menu(spec: AppSpec) -> tuple[AppCandidate | None, list[str]]:
    checked: list[str] = []
    for root in _start_menu_dirs():
        if not root.exists():
            checked.append(f"start_menu_missing:{root}")
            continue
        for link in root.rglob("*.lnk"):
            if not _link_name_matches(spec, link):
                continue
            checked.append(f"lnk:{link}")
            target = resolve_lnk_target(link)
            if target is None:
                checked.append(f"lnk_unresolved:{link}")
                continue
            candidate = _validate_exe_target(
                spec,
                target.target_path,
                source=f"start_menu:{link}",
                arguments=target.arguments,
            )
            if candidate is not None:
                return candidate, checked
            checked.append(f"lnk_rejected:{link}")
    return None, checked


def _iter_app_path_registry_values(spec: AppSpec) -> Iterable[str]:
    try:
        import winreg
    except ImportError:
        return []

    values: list[str] = []
    roots = (
        winreg.HKEY_CURRENT_USER,
        winreg.HKEY_LOCAL_MACHINE,
    )
    for root in roots:
        for exe_name in spec.exe_names:
            subkey = rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"
            try:
                with winreg.OpenKey(root, subkey) as key:
                    value, _kind = winreg.QueryValueEx(key, None)
                    if value:
                        values.append(str(value))
            except OSError:
                continue
    return values


def _discover_from_registry(spec: AppSpec) -> tuple[AppCandidate | None, list[str]]:
    checked: list[str] = []
    for value in _iter_app_path_registry_values(spec):
        checked.append(f"registry:{value}")
        candidate = _validate_exe_target(spec, value, source="registry")
        if candidate is not None:
            return candidate, checked
        checked.append(f"registry_rejected:{value}")
    return None, checked


def _discover_from_common_paths(spec: AppSpec) -> tuple[AppCandidate | None, list[str]]:
    checked: list[str] = []
    for raw in spec.common_paths:
        path = Path(os.path.expandvars(raw))
        checked.append(f"common:{path}")
        candidate = _validate_exe_target(spec, path, source="common_path")
        if candidate is not None:
            return candidate, checked
    return None, checked


def _discover_from_path(spec: AppSpec) -> tuple[AppCandidate | None, list[str]]:
    checked: list[str] = []
    for exe_name in spec.exe_names:
        found = shutil.which(exe_name)
        checked.append(f"path:{exe_name}")
        if not found:
            continue
        candidate = _validate_exe_target(spec, found, source="path")
        if candidate is not None:
            return candidate, checked
        checked.append(f"path_rejected:{found}")
    return None, checked


def discover_app(spec: AppSpec) -> tuple[AppCandidate | None, list[str]]:
    checked: list[str] = []
    for finder in (
        _discover_from_start_menu,
        _discover_from_registry,
        _discover_from_common_paths,
        _discover_from_path,
    ):
        candidate, steps = finder(spec)
        checked.extend(steps)
        if candidate is not None:
            return candidate, checked
    return None, checked


def _open_candidate(candidate: AppCandidate) -> None:
    subprocess.Popen(
        [candidate.path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
    )


async def _open_app(app: str, *, ctx: ToolContext) -> str:
    if not _is_windows():
        return "[ERROR] open_app is only supported on Windows in v1"

    app_id = normalize_app_name(app)
    if app_id is None:
        allowed = ", ".join(sorted(APP_SPECS))
        return f"[ERROR] app is not allowed: {app}. Allowed apps: {allowed}"

    spec = APP_SPECS[app_id]
    candidate, checked = discover_app(spec)
    if candidate is None:
        sample = checked[:12]
        suffix = "" if len(checked) <= 12 else f"\n... {len(checked) - 12} more checks omitted"
        return (
            f"[ERROR] app not found: {spec.id}\n"
            f"Checked:\n- " + "\n- ".join(sample or ["(no discovery paths available)"]) + suffix
        )

    try:
        _open_candidate(candidate)
    except OSError as exc:
        return f"[ERROR] cannot open app {spec.id}: {exc}"

    return (
        f"Opened app: {spec.id}\n"
        f"Path: {candidate.path}\n"
        f"Source: {candidate.source}"
    )


TOOL_OPEN_APP = Tool(
    name="open_app",
    description=(
        "Open a whitelisted Windows desktop application by name, such as WeChat, "
        "VS Code, Chrome, Edge, Notepad, Calculator, PowerShell, Windows Terminal, "
        "PyCharm, or Git Bash. Does not accept arbitrary paths or command arguments."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "app": {
                "type": "string",
                "description": "Whitelisted app name or alias, e.g. 微信, wechat, vscode, chrome.",
            },
        },
        "required": ["app"],
    },
    handler=_open_app,
    permission_level="L2",
)
