"""Phase 10 M10.1: ReActUserUpdate pipeline tests."""

import pytest

from mini_claw.agent.react_update import (
    ACTION_PLANNED_TEMPLATES,
    GENERIC_ACTION_PLANNED,
    GENERIC_PARALLEL_READONLY,
    contains_completion_claim,
    generate_action_planned_from_tools,
    hash_text,
    prepare_react_update_text,
    sanitize_react_update_text,
    should_send_update,
)
from mini_claw.agent.react_models import ReActUserUpdate


def test_generate_action_planned_known_tool():
    assert generate_action_planned_from_tools(["read_file"]) == ACTION_PLANNED_TEMPLATES["read_file"]


def test_generate_action_planned_unknown_tool_uses_generic():
    """plugin/custom tools未命中模板时走通用句，且不调 LLM。"""
    out = generate_action_planned_from_tools(["my_company_tool"])
    assert out == GENERIC_ACTION_PLANNED


def test_generate_action_planned_parallel_readonly():
    out = generate_action_planned_from_tools(["read_file", "list_directory"])
    assert out == GENERIC_PARALLEL_READONLY


def test_generate_action_planned_empty_returns_empty():
    assert generate_action_planned_from_tools([]) == ""


def test_sanitize_rejects_completion_claim():
    assert sanitize_react_update_text("文件已创建。", event_type="action_planned") is None


def test_sanitize_allows_future_tense():
    out = sanitize_react_update_text("我先读取这个文件。", event_type="action_planned")
    assert out == "我先读取这个文件。"


def test_sanitize_truncates_long_text():
    text = "a" * 500
    out = sanitize_react_update_text(text, max_chars=160, event_type="action_planned")
    assert out is not None
    assert len(out) <= 163


def test_sanitize_strips_code_blocks():
    out = sanitize_react_update_text(
        "好的，先看一眼 ```py\nprint(1)\n``` 再继续",
        event_type="action_planned",
    )
    assert out is not None
    assert "```" not in out


def test_completion_claim_detection():
    assert contains_completion_claim("已创建文件")
    assert not contains_completion_claim("我将创建文件")


def test_prepare_pipeline_returns_hash_of_final_text():
    """text_hash 必须等于实际发送文本的 hash —— 不是原始 candidate 的 hash。"""
    candidate = "  好的，我先读取这个文件。  "
    out = prepare_react_update_text(candidate, max_chars=160, event_type="action_planned")
    assert out is not None
    final_text, text_hash = out
    assert text_hash == hash_text(final_text)
    # Final text whitespace-normalized — different from raw candidate.
    assert final_text.strip() == final_text


def test_prepare_pipeline_drops_invalid():
    assert prepare_react_update_text("已完成", max_chars=160, event_type="action_planned") is None


def _make_update(event_type: str, *, is_important: bool = False) -> ReActUserUpdate:
    return ReActUserUpdate(
        update_id="u",
        step_id="s",
        run_id="r",
        chat_id="c",
        agent_id="a",
        event_type=event_type,  # type: ignore[arg-type]
        text="x",
        text_hash="h",
        is_important=is_important,
    )


def test_mode_silent_blocks_everything():
    for event in ("action_planned", "observation_summary", "decision_summary"):
        upd = _make_update(event, is_important=True)
        assert should_send_update(upd, "silent") is False


def test_mode_normal_allows_action_planned_only():
    assert should_send_update(_make_update("action_planned"), "normal") is True
    assert should_send_update(_make_update("observation_summary"), "normal") is False


def test_mode_normal_allows_important_decision_summary():
    upd = _make_update("decision_summary", is_important=True)
    assert should_send_update(upd, "normal") is True
    upd_unimportant = _make_update("decision_summary", is_important=False)
    assert should_send_update(upd_unimportant, "normal") is False


def test_mode_verbose_includes_observation():
    assert should_send_update(_make_update("observation_summary"), "verbose") is True
    assert should_send_update(_make_update("reflection_summary"), "verbose") is False


def test_mode_debug_includes_all_event_types():
    for event in (
        "action_planned",
        "observation_summary",
        "reflection_summary",
        "decision_summary",
    ):
        assert should_send_update(_make_update(event), "debug") is True
