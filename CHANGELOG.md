# Changelog

## 0.1.0 — 2026-10-18

First public release, rebuilt from an internal single-file server.

- Package layout with `exchange-mcp-server` CLI (`serve`, `check`, `login`).
- Configuration from environment / `.env` (legacy `~/exchange_mcp.env` still read).
- Auth: NTLM, Basic, OAuth app-only (impersonation), OAuth delegated (MSAL device code with token cache).
- 22 tools across mail, calendar, availability, rooms, invitations and sending.
- `dry_run` defaults to `true` for every tool that sends, edits or deletes.
- Deletes move items to Deleted Items (previous version hard-deleted calendar events).
- Free/busy via GetUserAvailability (no delegate access required); `Free` slots no longer count as conflicts.
- Fixed meeting-request listing that dropped upcoming requests and compared naive/aware datetimes.
- Room catalog with capacity/floor/equipment and `invite_as` for rooms that are user mailboxes.
- Attachments restricted to `EXCHANGE_ATTACHMENT_DIRS`.
- New: reply/forward, respond to meetings, drafts, mark read, list folders/calendars, resolve names,
  sub-calendars and all-day events.
- Compatible with `mcp` 1.x and 2.x.
