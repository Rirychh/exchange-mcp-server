"""MCP tool definitions."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Annotated, Any, Literal

import anyio
from mcp.types import ToolAnnotations
from pydantic import Field

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from . import timeutil
from .backend import BackendError, EwsBackend, _split_ids
from .config import ConfigError, Settings

log = logging.getLogger("exchange_mcp_server")

READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}
DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}

IdList = Annotated[list[str], Field(min_length=1, max_length=200, description="Item ids returned by other tools.")]
DryRun = Annotated[bool, Field(description="true (default) only previews; call again with false to apply.")]


def _friendly(exc: Exception) -> str:
    text = str(exc)
    if isinstance(exc, (BackendError, ConfigError, ValueError)):
        return text
    low = text.lower()
    if "401" in text or "unauthorized" in low:
        return "Authentication failed (401). Check EXCHANGE_EMAIL / EXCHANGE_USERNAME / EXCHANGE_PASSWORD / EXCHANGE_DOMAIN."
    if "timed out" in low or "connectionerror" in low or "max retries" in low:
        return f"Cannot reach the Exchange server ({text}). Check EXCHANGE_SERVER and network/VPN."
    if "ErrorNonExistentMailbox" in text:
        return "Mailbox does not exist."
    if "ErrorAccessDenied" in text:
        return f"Access denied by Exchange: {text}"
    return f"{type(exc).__name__}: {text}"


def ann(base: dict[str, bool], title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, **base)


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def build_server(settings: Settings, backend: EwsBackend | None = None) -> _Server:
    backend = backend or EwsBackend(settings)
    mcp = _Server(
        "exchange",
        instructions=(
            "Tools for one Microsoft Exchange mailbox (mail, calendar, free/busy, rooms). "
            "Times are interpreted in the server's configured time zone and use 'YYYY-MM-DD HH:MM'. "
            "Every tool that changes or sends something defaults to dry_run=true: show the preview to the "
            "user and only repeat the call with dry_run=false after they confirm."
        ),
    )

    async def run(fn: Callable[[], Any]) -> str:
        try:
            return _dump(await anyio.to_thread.run_sync(fn))
        except Exception as exc:  # noqa: BLE001
            log.debug("tool failed", exc_info=True)
            return _dump({"error": _friendly(exc)})

    def window(start: str | None, end: str | None, default_days: int):
        """Resolve a window; a bare date means the whole day (end date inclusive)."""
        tz = backend.tz
        if start:
            s = start.strip()
            lo = timeutil.day_bounds(timeutil.parse_date(s), timeutil.parse_date(s), tz)[0] if len(s) == 10 \
                else timeutil.parse_datetime(s, tz)
        else:
            today = timeutil.now(tz).date()
            lo = timeutil.day_bounds(today, today, tz)[0]
        if end:
            e = end.strip()
            hi = timeutil.day_bounds(timeutil.parse_date(e), timeutil.parse_date(e), tz)[1] if len(e) == 10 \
                else timeutil.parse_datetime(e, tz)
        else:
            hi = lo + timedelta(days=default_days)
        if hi <= lo:
            raise ValueError("end must be after start.")
        return lo, hi

    # ================================================================ read tools
    @mcp.tool(annotations=ann(READ, "Check connection"))
    async def exchange_whoami() -> str:
        """Check the connection and show the mailbox, server, time zone and inbox counts."""
        return await run(backend.whoami)

    @mcp.tool(annotations=ann(READ, "List mail folders"))
    async def exchange_list_folders() -> str:
        """List mail folders (path, total and unread counts). Paths can be passed as `folder` to other tools."""
        return await run(backend.list_folders)

    @mcp.tool(annotations=ann(READ, "List messages"))
    async def exchange_list_messages(
        folder: Annotated[str, Field(description="inbox, sent, drafts, deleted, junk, a folder name or a path like 'Inbox/Projects'.")] = "inbox",
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
        offset: Annotated[int, Field(ge=0, le=10000)] = 0,
        unread_only: bool = False,
    ) -> str:
        """List the newest messages in a folder (id, subject, from, received, read state)."""
        return await run(lambda: backend.list_messages(folder, limit, offset, unread_only))

    @mcp.tool(annotations=ann(READ, "Search messages"))
    async def exchange_search_messages(
        query: Annotated[str, Field(description="Text matched against subject OR body (server side).")] = "",
        subject: Annotated[str, Field(description="Text the subject must contain.")] = "",
        sender: Annotated[str, Field(description="Part of the sender's display name or address.")] = "",
        folder: str = "inbox",
        days_back: Annotated[int, Field(ge=0, le=3650, description="0 = no date limit.")] = 30,
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
    ) -> str:
        """Search a folder by text, subject and/or sender. At least one criterion is required."""
        if not (query or subject or sender):
            return _dump({"error": "Give at least one of query, subject or sender."})
        return await run(lambda: backend.search_messages(folder=folder, query=query, subject=subject, sender=sender,
                                                         days_back=days_back, limit=limit))

    @mcp.tool(annotations=ann(READ, "Read message"))
    async def exchange_get_message(
        item_id: Annotated[str, Field(min_length=1)],
        max_chars: Annotated[int, Field(ge=200, le=200000)] = 20000,
    ) -> str:
        """Read one message: headers, recipients, attachment list and plain-text body."""
        return await run(lambda: backend.get_message(item_id, max_chars))

    @mcp.tool(annotations=ann(READ, "Look up people"))
    async def exchange_resolve_names(query: Annotated[str, Field(min_length=1, max_length=200)]) -> str:
        """Resolve a name or partial address against the directory and contacts to get email addresses."""
        return await run(lambda: backend.resolve_names(query))

    @mcp.tool(annotations=ann(READ, "List calendars"))
    async def exchange_list_calendars() -> str:
        """List calendar folders (the default calendar and any sub-calendars)."""
        return await run(backend.list_calendars)

    @mcp.tool(annotations=ann(READ, "List events"))
    async def exchange_list_events(
        start: Annotated[str | None, Field(description="YYYY-MM-DD or YYYY-MM-DD HH:MM; default today.")] = None,
        end: Annotated[str | None, Field(description="YYYY-MM-DD (inclusive) or YYYY-MM-DD HH:MM; default start + days.")] = None,
        days: Annotated[int, Field(ge=1, le=366, description="Window length when end is omitted.")] = 7,
        calendar: Annotated[str | None, Field(description="Sub-calendar name; default calendar when omitted.")] = None,
        subject_contains: str = "",
    ) -> str:
        """List calendar events (recurring occurrences expanded) in a time window."""
        def go():
            lo, hi = window(start, end, days)
            return backend.list_events(lo, hi, calendar, subject_contains)
        return await run(go)

    @mcp.tool(annotations=ann(READ, "Check people's availability"))
    async def exchange_get_availability(
        emails: Annotated[list[str], Field(min_length=1, max_length=100)],
        start: Annotated[str, Field(description="YYYY-MM-DD HH:MM")],
        end: Annotated[str, Field(description="YYYY-MM-DD HH:MM")],
    ) -> str:
        """Free/busy for people or rooms in a window (no delegate access needed)."""
        def go():
            lo, hi = window(start, end, 1)
            fb = backend.free_busy(emails, lo, hi)
            return {"window": f"{timeutil.fmt(lo, backend.tz)} - {timeutil.fmt(hi, backend.tz)}",
                    "all_free": all(v["status"] == "free" for v in fb.values()), "results": fb}
        return await run(go)

    @mcp.tool(annotations=ann(READ, "List meeting rooms"))
    async def exchange_list_rooms() -> str:
        """List meeting rooms from Exchange room lists and/or the local room catalog (with capacity, floor, equipment)."""
        return await run(backend.list_rooms)

    @mcp.tool(annotations=ann(READ, "Find free rooms"))
    async def exchange_find_available_rooms(
        start: Annotated[str, Field(description="YYYY-MM-DD HH:MM")],
        end: Annotated[str, Field(description="YYYY-MM-DD HH:MM")],
        min_capacity: Annotated[int, Field(ge=1, le=1000)] = 1,
    ) -> str:
        """Find rooms that are free for the whole window. Rooms with unknown capacity are kept and flagged."""
        def go():
            lo, hi = window(start, end, 1)
            return backend.find_available_rooms(lo, hi, min_capacity)
        return await run(go)

    @mcp.tool(annotations=ann(READ, "List meeting requests"))
    async def exchange_list_meeting_requests(
        include_past: bool = True,
        include_cancellations: bool = True,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> str:
        """List meeting requests and cancellations waiting in the inbox."""
        return await run(lambda: backend.list_meeting_requests(include_past=include_past,
                                                               include_cancellations=include_cancellations, limit=limit))

    if settings.read_only:
        return mcp

    # ================================================================ write tools
    @mcp.tool(annotations=ann(WRITE, "Mark read/unread"))
    async def exchange_mark_read(ids: IdList, is_read: bool = True) -> str:
        """Mark messages as read or unread."""
        return await run(lambda: backend.mark_read(_split_ids(ids), is_read))

    @mcp.tool(annotations=ann(WRITE, "Move messages"))
    async def exchange_move_messages(
        ids: IdList,
        folder: Annotated[str, Field(min_length=1, description="Target folder name or path, e.g. 'Inbox/Projects'.")],
        create_folder: Annotated[bool, Field(description="Create the folder (under Inbox, or along the path) if missing.")] = False,
    ) -> str:
        """Move messages to another folder."""
        return await run(lambda: backend.move_messages(_split_ids(ids), folder, create_folder))

    @mcp.tool(annotations=ann(DESTRUCTIVE, "Move messages to Deleted Items"))
    async def exchange_trash_messages(ids: IdList, dry_run: DryRun = True) -> str:
        """Move messages to Deleted Items (recoverable; not a permanent delete)."""
        return await run(lambda: backend.trash_messages(_split_ids(ids), dry_run))

    @mcp.tool(annotations=ann(WRITE, "Create event / meeting"))
    async def exchange_create_event(
        subject: Annotated[str, Field(min_length=1, max_length=255)],
        start: Annotated[str, Field(description="YYYY-MM-DD HH:MM (or YYYY-MM-DD when all_day).")],
        end: Annotated[str | None, Field(description="YYYY-MM-DD HH:MM; for all_day the last day (inclusive).")] = None,
        duration_minutes: Annotated[int | None, Field(ge=5, le=1440, description="Used when end is omitted (default 60).")] = None,
        all_day: bool = False,
        attendees: Annotated[list[str] | None, Field(max_length=200)] = None,
        optional_attendees: Annotated[list[str] | None, Field(max_length=200)] = None,
        room_email: Annotated[str | None, Field(description="Room to book (see exchange_find_available_rooms).")] = None,
        location: str | None = None,
        body: str = "",
        body_is_html: bool = False,
        reminder_minutes: Annotated[int | None, Field(ge=0, le=40320, description="null = no reminder.")] = 15,
        calendar: Annotated[str | None, Field(description="Sub-calendar name; default calendar when omitted.")] = None,
        busy_status: Literal["Free", "Tentative", "Busy", "OOF", "WorkingElsewhere"] = "Busy",
        dry_run: DryRun = True,
    ) -> str:
        """Create an appointment, or a meeting when attendees/room are given (invitations are sent)."""
        if not settings.allow_send and (attendees or optional_attendees or room_email):
            return _dump({"error": "Sending is disabled (EXCHANGE_ALLOW_SEND=false); only appointments without attendees."})
        return await run(lambda: backend.create_event(
            subject=subject, start=start, end=end, duration_minutes=duration_minutes, all_day=all_day,
            attendees=attendees, optional_attendees=optional_attendees, room_email=room_email, location=location,
            body=body, body_is_html=body_is_html, reminder_minutes=reminder_minutes, calendar=calendar,
            busy_status=busy_status, dry_run=dry_run))

    @mcp.tool(annotations=ann(WRITE, "Update event"))
    async def exchange_update_event(
        item_id: str | None = None,
        subject_contains: str | None = None,
        start_date: Annotated[str | None, Field(description="YYYY-MM-DD, used with end_date to find the event.")] = None,
        end_date: Annotated[str | None, Field(description="YYYY-MM-DD (inclusive).")] = None,
        calendar: str | None = None,
        new_subject: str | None = None,
        new_location: str | None = None,
        new_start: Annotated[str | None, Field(description="YYYY-MM-DD HH:MM; keeps duration if new_end omitted.")] = None,
        new_end: str | None = None,
        body: str | None = None,
        body_mode: Literal["replace", "append"] = "replace",
        send_updates: Annotated[bool, Field(description="Notify attendees (only when you are the organizer).")] = False,
        dry_run: DryRun = True,
    ) -> str:
        """Change exactly one event: subject, location, time or description. Select by item_id or by date range + subject."""
        if send_updates and not settings.allow_send:
            return _dump({"error": "Sending is disabled (EXCHANGE_ALLOW_SEND=false)."})
        return await run(lambda: backend.update_event(
            item_id=item_id, subject_contains=subject_contains, start_date=start_date, end_date=end_date,
            calendar=calendar, new_subject=new_subject, new_location=new_location, new_start=new_start,
            new_end=new_end, body=body, body_mode=body_mode, send_updates=send_updates, dry_run=dry_run))

    @mcp.tool(annotations=ann(DESTRUCTIVE, "Delete events"))
    async def exchange_delete_events(
        item_id: str | None = None,
        subject_contains: str | None = None,
        start_date: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
        end_date: Annotated[str | None, Field(description="YYYY-MM-DD (inclusive)")] = None,
        calendar: str | None = None,
        send_cancellations: Annotated[bool, Field(description="Send cancellations for meetings you organize.")] = False,
        max_delete: Annotated[int, Field(ge=1, le=200)] = 20,
        dry_run: DryRun = True,
    ) -> str:
        """Move events to Deleted Items. Select by item_id, or by date range (required) plus optional subject filter."""
        if send_cancellations and not settings.allow_send:
            return _dump({"error": "Sending is disabled (EXCHANGE_ALLOW_SEND=false)."})
        return await run(lambda: backend.delete_events(
            item_id=item_id, subject_contains=subject_contains, start_date=start_date, end_date=end_date,
            calendar=calendar, send_cancellations=send_cancellations, max_delete=max_delete, dry_run=dry_run))

    @mcp.tool(annotations=ann(DESTRUCTIVE, "Clean up meeting requests"))
    async def exchange_cleanup_meeting_requests(
        ids: Annotated[list[str] | None, Field(description="Specific requests; omit to target all past ones.")] = None,
        dry_run: DryRun = True,
    ) -> str:
        """Move meeting requests/cancellations to Deleted Items: the given ids, or every one whose meeting has started."""
        return await run(lambda: backend.cleanup_meeting_requests(ids=_split_ids(ids) if ids else None, dry_run=dry_run))

    if not settings.allow_send:
        return mcp

    # ================================================================ send tools
    @mcp.tool(annotations=ann(WRITE, "Send email / save draft"))
    async def exchange_send_email(
        to: Annotated[list[str], Field(min_length=1, max_length=100)],
        subject: Annotated[str, Field(min_length=1, max_length=255)],
        body: Annotated[str, Field(min_length=1)],
        cc: Annotated[list[str] | None, Field(max_length=100)] = None,
        bcc: Annotated[list[str] | None, Field(max_length=100)] = None,
        body_is_html: bool = False,
        attachments: Annotated[list[str] | None, Field(max_length=20, description="Local paths inside EXCHANGE_ATTACHMENT_DIRS.")] = None,
        inline_images: Annotated[dict[str, str] | None, Field(description="{content_id: local path}; reference as <img src=\"cid:ID\">.")] = None,
        signature: Annotated[str | None, Field(description="Outlook signature name (HTML body only).")] = None,
        send_at: Annotated[str | None, Field(description="YYYY-MM-DD HH:MM for delayed delivery by the server.")] = None,
        mode: Literal["send", "draft"] = "send",
        dry_run: DryRun = True,
    ) -> str:
        """Send an email or save it as a draft. Supports attachments, inline images, an Outlook signature and delayed delivery."""
        return await run(lambda: backend.compose(
            to=to, subject=subject, body=body, cc=cc, bcc=bcc, body_is_html=body_is_html, attachments=attachments,
            inline_images=inline_images, signature=signature, send_at=send_at, mode=mode, dry_run=dry_run))

    @mcp.tool(annotations=ann(WRITE, "Reply / forward"))
    async def exchange_reply_or_forward(
        item_id: Annotated[str, Field(min_length=1)],
        action: Literal["reply", "reply_all", "forward"],
        body: Annotated[str, Field(min_length=1)],
        to: Annotated[list[str] | None, Field(description="Required for forward; optional override for reply.")] = None,
        cc: list[str] | None = None,
        dry_run: DryRun = True,
    ) -> str:
        """Reply, reply-all or forward a message (the original is quoted by Exchange)."""
        return await run(lambda: backend.reply_or_forward(item_id=item_id, action=action, body=body, to=to, cc=cc,
                                                          dry_run=dry_run))

    @mcp.tool(annotations=ann(WRITE, "Respond to meeting"))
    async def exchange_respond_to_meeting(
        item_id: Annotated[str, Field(min_length=1, description="Meeting request or calendar item id.")],
        response: Literal["accept", "tentative", "decline"],
        message: str = "",
        dry_run: DryRun = True,
    ) -> str:
        """Accept, tentatively accept or decline a meeting invitation."""
        return await run(lambda: backend.respond_to_meeting(item_id=item_id, response=response, message=message,
                                                            dry_run=dry_run))

    return mcp
