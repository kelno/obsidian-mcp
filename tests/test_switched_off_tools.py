"""Upstream's tools the fork switches off are not offered (CLAUDE.md, "Following upstream").

The suite runs with them switched on (`conftest.pytest_configure`),
so this test switches them off again, the state the server module leaves at
import, and checks no client is offered them.
"""

import asyncio

from src.mcp_server import server


def test_the_switched_off_tools_are_not_listed_and_come_back_on():
    assert server.SWITCHED_OFF == {
        "upstream_list_tasks", "task_agenda", "complete_task", "update_task", "add_task",
        "get_changes", "hybrid_search",
    }
    server.switch_off_again()
    try:
        names = {t.name for t in asyncio.run(server.mcp.list_tools())}
        assert not names & server.SWITCHED_OFF
        # The fork's own task tools keep their names.
        assert {"list_tasks", "create_task", "edit_task", "set_task_status"} <= names
    finally:
        server.switch_on()
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert server.SWITCHED_OFF <= names
