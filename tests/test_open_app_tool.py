"""Tests for the controlled open_app tool."""

from pathlib import Path

import pytest

from mini_claw.tools.open_app import (
    AppCandidate,
    AppSpec,
    LinkTarget,
    TOOL_OPEN_APP,
    _validate_exe_target,
    discover_app,
    normalize_app_name,
)
from mini_claw.tools.registry import ToolContext


def test_open_app_aliases_normalize_to_wechat():
    assert normalize_app_name("微信") == "wechat"
    assert normalize_app_name("wechat") == "wechat"
    assert normalize_app_name("WeChat") == "wechat"


def test_open_app_rejects_lnk_target_with_wrong_exe(tmp_path: Path):
    cmd = tmp_path / "cmd.exe"
    cmd.write_text("", encoding="utf-8")

    candidate = _validate_exe_target(
        AppSpec("wechat", aliases=("wechat",), exe_names=("WeChat.exe",)),
        cmd,
        source="start_menu:test.lnk",
        arguments="/c calc.exe",
    )

    assert candidate is None


def test_open_app_rejects_lnk_target_with_arguments(tmp_path: Path):
    wechat = tmp_path / "WeChat.exe"
    wechat.write_text("", encoding="utf-8")

    candidate = _validate_exe_target(
        AppSpec("wechat", aliases=("wechat",), exe_names=("WeChat.exe",)),
        wechat,
        source="start_menu:test.lnk",
        arguments="https://example.invalid",
    )

    assert candidate is None


def test_open_app_accepts_lnk_target_with_allowed_exe(tmp_path: Path):
    wechat = tmp_path / "WeChat.exe"
    wechat.write_text("", encoding="utf-8")

    candidate = _validate_exe_target(
        AppSpec("wechat", aliases=("wechat",), exe_names=("WeChat.exe",)),
        wechat,
        source="start_menu:test.lnk",
    )

    assert candidate is not None
    assert candidate.app_id == "wechat"
    assert candidate.path.endswith("WeChat.exe")


def test_open_app_skips_unresolved_lnk_and_uses_common_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    start_dir = tmp_path / "Start Menu"
    start_dir.mkdir()
    (start_dir / "微信.lnk").write_text("", encoding="utf-8")
    wechat = tmp_path / "WeChat.exe"
    wechat.write_text("", encoding="utf-8")
    spec = AppSpec(
        "wechat",
        aliases=("微信", "wechat"),
        exe_names=("WeChat.exe",),
        common_paths=(str(wechat),),
    )

    monkeypatch.setattr("mini_claw.tools.open_app._is_windows", lambda: True)
    monkeypatch.setattr("mini_claw.tools.open_app._start_menu_dirs", lambda: [start_dir])
    monkeypatch.setattr("mini_claw.tools.open_app.resolve_lnk_target", lambda _path: None)
    monkeypatch.setattr("mini_claw.tools.open_app._iter_app_path_registry_values", lambda _spec: [])

    candidate, checked = discover_app(spec)

    assert candidate is not None
    assert candidate.source == "common_path"
    assert any(step.startswith("lnk_unresolved:") for step in checked)


@pytest.mark.asyncio
async def test_open_app_tool_non_windows_returns_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("mini_claw.tools.open_app._is_windows", lambda: False)

    result = await TOOL_OPEN_APP.handler(
        app="wechat",
        ctx=ToolContext(workspace_dir=Path(".")),
    )

    assert result == "[ERROR] open_app is only supported on Windows in v1"


@pytest.mark.asyncio
async def test_open_app_tool_unknown_app_is_rejected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("mini_claw.tools.open_app._is_windows", lambda: True)

    result = await TOOL_OPEN_APP.handler(
        app="totally-not-allowed",
        ctx=ToolContext(workspace_dir=Path(".")),
    )

    assert result.startswith("[ERROR] app is not allowed")


@pytest.mark.asyncio
async def test_open_app_tool_opens_discovered_candidate(monkeypatch: pytest.MonkeyPatch):
    opened: list[AppCandidate] = []
    candidate = AppCandidate("wechat", r"C:\Program Files\Tencent\WeChat\WeChat.exe", "registry")

    monkeypatch.setattr("mini_claw.tools.open_app._is_windows", lambda: True)
    monkeypatch.setattr("mini_claw.tools.open_app.discover_app", lambda _spec: (candidate, ["registry"]))
    monkeypatch.setattr("mini_claw.tools.open_app._open_candidate", lambda item: opened.append(item))

    result = await TOOL_OPEN_APP.handler(
        app="微信",
        ctx=ToolContext(workspace_dir=Path(".")),
    )

    assert opened == [candidate]
    assert "Opened app: wechat" in result
    assert "Source: registry" in result


@pytest.mark.asyncio
async def test_open_app_tool_not_found_does_not_claim_opened(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("mini_claw.tools.open_app._is_windows", lambda: True)
    monkeypatch.setattr(
        "mini_claw.tools.open_app.discover_app",
        lambda _spec: (None, ["start_menu_missing:X", "path:WeChat.exe"]),
    )

    result = await TOOL_OPEN_APP.handler(
        app="wechat",
        ctx=ToolContext(workspace_dir=Path(".")),
    )

    assert result.startswith("[ERROR] app not found: wechat")
    assert "Opened app" not in result
