"""Permission and registry tests for open_app."""

from mini_claw.tools.open_app import TOOL_OPEN_APP
from mini_claw.tools.registry import ToolRegistry


def test_open_app_permission_level_is_l2():
    assert TOOL_OPEN_APP.permission_level == "L2"


def test_open_app_registry_schema_visibility_depends_on_agent_tools():
    registry = ToolRegistry()
    registry.register(TOOL_OPEN_APP)

    visible = registry.schemas_for(["open_app"])
    hidden = registry.schemas_for(["read_file"])

    assert visible[0]["name"] == "open_app"
    assert hidden == []
