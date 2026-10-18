import json
from unittest.mock import MagicMock

import pytest

from exchange_mcp_server.backend import BackendError
from exchange_mcp_server.config import Settings
from exchange_mcp_server.server import build_server

READ_TOOLS = {
    "exchange_whoami", "exchange_list_folders", "exchange_list_messages", "exchange_search_messages",
    "exchange_get_message", "exchange_resolve_names", "exchange_list_calendars", "exchange_list_events",
    "exchange_get_availability", "exchange_list_rooms", "exchange_find_available_rooms",
    "exchange_list_meeting_requests",
}
SEND_TOOLS = {"exchange_send_email", "exchange_reply_or_forward", "exchange_respond_to_meeting"}


def settings(**kw):
    return Settings(email="me@x.com", server="https://x", password="p", timezone="Asia/Taipei", **kw)


async def names(server):
    return {t.name for t in await server.list_tools()}


def text_of(result):
    """Extract the text payload from call_tool across mcp 1.x / 2.x return shapes."""
    if isinstance(result, tuple):
        result = result[0]
    if hasattr(result, "content"):
        result = result.content
    return result[0].text


async def test_modes_register_expected_tools():
    full = await names(build_server(settings(), MagicMock()))
    ro = await names(build_server(settings(read_only=True), MagicMock()))
    nosend = await names(build_server(settings(allow_send=False), MagicMock()))
    assert ro == READ_TOOLS
    assert SEND_TOOLS <= full and not (SEND_TOOLS & nosend)
    assert len(full) == 22


async def test_destructive_tools_default_to_dry_run():
    server = build_server(settings(), MagicMock())
    for tool in await server.list_tools():
        schema = getattr(tool, "inputSchema", None) or tool.input_schema
        if "dry_run" in schema["properties"]:
            assert schema["properties"]["dry_run"]["default"] is True, tool.name


async def test_errors_are_returned_as_json():
    backend = MagicMock()
    backend.get_message.side_effect = BackendError("Item not found: zzz")
    server = build_server(settings(), backend)
    payload = json.loads(text_of(await server.call_tool("exchange_get_message", {"item_id": "zzz"})))
    assert payload == {"error": "Item not found: zzz"}


async def test_search_requires_criterion():
    server = build_server(settings(), MagicMock())
    payload = json.loads(text_of(await server.call_tool("exchange_search_messages", {})))
    assert "at least one" in payload["error"]


async def test_create_event_blocked_when_send_disabled():
    backend = MagicMock()
    server = build_server(settings(allow_send=False), backend)
    payload = json.loads(text_of(await server.call_tool(
        "exchange_create_event", {"subject": "x", "start": "2026-07-01 10:00", "attendees": ["a@x.com"]})))
    assert "disabled" in payload["error"]
    backend.create_event.assert_not_called()


@pytest.mark.parametrize("start,end,hours", [("2026-07-01", None, 7 * 24), ("2026-07-01", "2026-07-01", 24),
                                             ("2026-07-01 09:00", "2026-07-01 11:30", 2.5)])
async def test_list_events_window(start, end, hours):
    backend = MagicMock()
    from exchange_mcp_server import timeutil
    backend.tz = timeutil.get_timezone("Asia/Taipei")
    backend.list_events.return_value = []
    server = build_server(settings(), backend)
    args = {"start": start} | ({"end": end} if end else {})
    await server.call_tool("exchange_list_events", args)
    lo, hi = backend.list_events.call_args.args[:2]
    assert (hi - lo).total_seconds() / 3600 == hours
