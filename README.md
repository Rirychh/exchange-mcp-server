# exchange-mcp-server

A [Model Context Protocol](https://modelcontextprotocol.io) server that lets Claude (or any MCP client) work with a
Microsoft Exchange mailbox over **EWS**: read and search mail, file and clean up messages, read and edit the
calendar, check people's free/busy, find and book meeting rooms, and send email with attachments, signatures and
delayed delivery.

[繁體中文說明 → README.zh-TW.md](README.zh-TW.md)

> [!IMPORTANT]
> **Exchange Online (Microsoft 365):** Microsoft is retiring EWS in Exchange Online. Phased blocking started on
> **2026-10-01** for tenants that have not allow-listed their apps, and EWS is permanently shut down on
> **2027-04-01**. The OAuth modes below only work for tenants that still allow EWS.
> **On-premises Exchange Server is not affected**, and that is the main target of this project.
> A Microsoft Graph backend is the planned path for Exchange Online.

## Features

| Area | Tools |
|---|---|
| Mailbox | `exchange_whoami`, `exchange_list_folders`, `exchange_list_messages`, `exchange_search_messages`, `exchange_get_message`, `exchange_resolve_names` |
| Filing | `exchange_mark_read`, `exchange_move_messages`, `exchange_trash_messages` |
| Calendar | `exchange_list_calendars`, `exchange_list_events`, `exchange_create_event`, `exchange_update_event`, `exchange_delete_events` |
| Scheduling | `exchange_get_availability`, `exchange_list_rooms`, `exchange_find_available_rooms` |
| Invitations | `exchange_list_meeting_requests`, `exchange_cleanup_meeting_requests`, `exchange_respond_to_meeting` |
| Sending | `exchange_send_email` (send or save draft), `exchange_reply_or_forward` |

Design points:

- **Dry run by default.** Every tool that sends, deletes or edits takes `dry_run` (default `true`) and returns a
  preview first. The model has to call again with `dry_run=false`, which gives you a confirmation step.
- **Recoverable deletes.** Messages, events and meeting requests are moved to *Deleted Items*, never hard-deleted.
- **Free/busy without delegation.** Availability and room search use `GetUserAvailability`, so you do not need
  delegate access to colleagues' or rooms' calendars.
- **Rooms that Exchange does not describe.** A local catalog adds capacity, floor and equipment, and supports
  organisations whose meeting rooms are ordinary user mailboxes (`invite_as: "attendee"`).
- **Attachment sandbox.** Local files can only be attached from folders listed in `EXCHANGE_ATTACHMENT_DIRS`, so a
  prompt-injected email cannot make the model mail out arbitrary files.
- **Read-only and no-send modes** for cautious deployments.
- Works with `mcp` 1.x and 2.x, Python 3.10+.

## Install

```bash
pip install "exchange-mcp-server @ git+https://github.com/Rirychh/exchange-mcp-server"
# with Exchange Online delegated sign-in support:
pip install "exchange-mcp-server[oauth] @ git+https://github.com/Rirychh/exchange-mcp-server"
```

Or run without installing, using [uv](https://docs.astral.sh/uv/):

```bash
uvx --from git+https://github.com/Rirychh/exchange-mcp-server exchange-mcp-server check
```

## Configure

Copy [`.env.example`](.env.example) to one of these locations (first match wins) and fill it in:

1. the path in `EXCHANGE_MCP_ENV_FILE`
2. `./.env`
3. `~/.config/exchange-mcp/.env`
4. `~/exchange_mcp.env` (legacy)

Real environment variables always override the file. Then test it:

```bash
exchange-mcp-server check
```

### Settings

| Variable | Default | Description |
|---|---|---|
| `EXCHANGE_EMAIL` | — | Mailbox address (required). |
| `EXCHANGE_SERVER` | — | EWS URL, e.g. `https://mail.example.com/EWS/Exchange.asmx`. Defaults to Exchange Online for OAuth modes. |
| `EXCHANGE_AUTODISCOVER` | `false` | Use autodiscover instead of `EXCHANGE_SERVER`. |
| `EXCHANGE_AUTH` | `ntlm` | `ntlm`, `basic`, `oauth_app`, `oauth_delegated`. |
| `EXCHANGE_USERNAME` | email | Login name if it differs from the email (`DOMAIN\user` or UPN). |
| `EXCHANGE_PASSWORD` | — | Password for `ntlm` / `basic`. |
| `EXCHANGE_DOMAIN` | — | NTLM domain; with it the login becomes `DOMAIN\localpart`. |
| `EXCHANGE_VERIFY_SSL` | `true` | Set `false` only for a self-signed lab server. |
| `EXCHANGE_TIMEZONE` | host zone | IANA zone used to read and print times, e.g. `Asia/Taipei`. |
| `EXCHANGE_READ_ONLY` | `false` | Register only read tools. |
| `EXCHANGE_ALLOW_SEND` | `true` | `false` removes send/reply/respond tools and blocks invitations. |
| `EXCHANGE_ATTACHMENT_DIRS` | *(none)* | Comma-separated folders that may be attached. Empty = attachments off. |
| `EXCHANGE_MAX_ATTACHMENT_MB` | `20` | Total attachment size limit. |
| `EXCHANGE_SIGNATURE_DIR` | `%APPDATA%\Microsoft\Signatures` | Outlook signature folder. |
| `EXCHANGE_ROOM_SOURCE` | `both` | `roomlists` (Exchange room lists), `catalog` (local file) or `both`. |
| `EXCHANGE_ROOMS_FILE` | `~/.config/exchange-mcp/rooms.json` | Room catalog path. |
| `EXCHANGE_TENANT_ID` / `EXCHANGE_CLIENT_ID` / `EXCHANGE_CLIENT_SECRET` | — | OAuth app registration. |
| `EXCHANGE_TOKEN_CACHE` | `~/.config/exchange-mcp/token_cache.json` | MSAL cache for `oauth_delegated`. |

### Authentication modes

**On-premises Exchange (`ntlm` / `basic`)** — set `EXCHANGE_SERVER`, `EXCHANGE_EMAIL`, `EXCHANGE_PASSWORD` and usually
`EXCHANGE_DOMAIN`.

**Exchange Online, signed-in user (`oauth_delegated`)**

1. In Entra ID, register an app; under *Authentication* enable **Allow public client flows**.
2. Add the delegated API permission **Office 365 Exchange Online → EWS.AccessAsUser.All** and grant consent.
3. Set `EXCHANGE_AUTH=oauth_delegated`, `EXCHANGE_CLIENT_ID`, `EXCHANGE_TENANT_ID`, `EXCHANGE_EMAIL`.
4. Run `exchange-mcp-server login` once and follow the device-code prompt. Tokens refresh silently afterwards.

**Exchange Online, app-only (`oauth_app`)** — app registration with the application permission
`full_access_as_app` and a client secret; the server impersonates `EXCHANGE_EMAIL`. Scope it with an
[application access policy](https://learn.microsoft.com/exchange/permissions-exo/application-rbac).

## Use with Claude Desktop

Add to `claude_desktop_config.json` (see [`examples/`](examples)):

```json
{
  "mcpServers": {
    "exchange": {
      "command": "exchange-mcp-server",
      "env": { "EXCHANGE_MCP_ENV_FILE": "C:\\Users\\you\\.config\\exchange-mcp\\.env" }
    }
  }
}
```

With uv: `"command": "uvx", "args": ["--from", "git+https://github.com/Rirychh/exchange-mcp-server", "exchange-mcp-server"]`.

Claude Code: `claude mcp add exchange -- exchange-mcp-server`.

## Room catalog

EWS has no room capacity, and some companies create rooms as normal user accounts so they never appear in room
lists. Describe them in `rooms.json` ([example](rooms.example.json)):

```json
{
  "rooms": [
    { "email": "room-3a@example.com", "name": "Room 3A", "capacity": 8, "floor": "3F",
      "equipment": ["TV", "webcam"] },
    { "email": "boardroom@example.com", "name": "Boardroom", "capacity": 20, "invite_as": "attendee" }
  ]
}
```

- `invite_as: "resource"` (default) books the room as a resource; `"attendee"` invites it as a required attendee.
- Rooms without `capacity` are never filtered out by `min_capacity`; they are listed under `capacity_unknown`.
- The file is re-read automatically after you edit it.

## Example prompts

- "Which of my unread emails from this week need a reply?"
- "Find a room for 8 people tomorrow 14:00–15:30 and check whether Ana and Ben are free."
- "File every newsletter in my inbox into *Inbox/Newsletters* (preview first)."
- "Put an all-day 'Design review' on 3 Nov in my *Project X* calendar, no invitations."
- "Reply to the latest mail from facilities and attach `report.pdf` from my Reports folder."

## Security notes

- Keep the env file private (`chmod 600`); it holds your password or client secret.
- Mail content is untrusted input to the model. Keep `dry_run` previews, `EXCHANGE_ATTACHMENT_DIRS` and, if in
  doubt, `EXCHANGE_ALLOW_SEND=false` or `EXCHANGE_READ_ONLY=true`.
- The server talks only to your Exchange endpoint (and Microsoft login for OAuth).

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

Tests use mocks and need no Exchange server. Exchange access lives in `backend.py` (`EwsBackend`); tool definitions
in `server.py` only call backend methods, so another backend (e.g. Graph) can implement the same interface.

## License

[MIT](LICENSE)
