"""EWS backend.

All Exchange access goes through :class:`EwsBackend`. Tools in ``server.py`` only
validate input, call these methods and serialise the returned dictionaries, so a
different backend (e.g. Microsoft Graph) can implement the same methods later.
"""

from __future__ import annotations

import html
import threading
from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path
from typing import Any

from exchangelib import (
    Account,
    Attendee,
    CalendarItem,
    EWSDate,
    FileAttachment,
    Folder,
    HTMLBody,
    Mailbox,
    Message,
    Q,
)
from exchangelib.items import MeetingCancellation, MeetingRequest
from exchangelib.properties import ItemId

from . import timeutil
from .auth import build_configuration
from .config import Settings
from .rooms import Room, RoomCatalog
from .signatures import load_signature

WELL_KNOWN_FOLDERS = {
    "inbox": "inbox",
    "sent": "sent",
    "sent items": "sent",
    "drafts": "drafts",
    "deleted": "trash",
    "deleted items": "trash",
    "trash": "trash",
    "junk": "junk",
    "junk email": "junk",
    "outbox": "outbox",
}

MEETING_REQUEST_CLASS = "IPM.Schedule.Meeting.Request"
MEETING_CANCEL_CLASS = "IPM.Schedule.Meeting.Canceled"
SUMMARY_FIELDS = ("id", "subject", "sender", "datetime_received", "is_read", "has_attachments")
FREE_BUSY_CHUNK = 50


class BackendError(RuntimeError):
    """An error with a message that is safe and useful to show to the model."""


def _mailbox_str(mb) -> str | None:
    if mb is None:
        return None
    name = getattr(mb, "name", None)
    addr = getattr(mb, "email_address", None)
    if name and addr and name != addr:
        return f"{name} <{addr}>"
    return addr or name


def _split_ids(ids: Iterable[str] | str) -> list[str]:
    if isinstance(ids, str):
        ids = ids.replace("\n", ",").split(",")
    return [i.strip() for i in ids if i and i.strip()]


class EwsBackend:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.tz = timeutil.get_timezone(settings.timezone)
        self.rooms = RoomCatalog(settings.rooms_file)
        self._lock = threading.Lock()
        self._account: Account | None = None
        self._deferred_registered = False
        if not settings.verify_ssl:
            from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter

            BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter

    # ------------------------------------------------------------------ account
    @property
    def account(self) -> Account:
        with self._lock:
            if self._account is None:
                from exchangelib import DELEGATE

                config, credentials, access_type = build_configuration(self.settings)
                if config is None:
                    self._account = Account(
                        primary_smtp_address=self.settings.email,
                        credentials=credentials,
                        autodiscover=True,
                        access_type=access_type or DELEGATE,
                    )
                else:
                    self._account = Account(
                        primary_smtp_address=self.settings.email,
                        config=config,
                        autodiscover=False,
                        access_type=access_type,
                    )
            return self._account

    def reset(self) -> None:
        with self._lock:
            self._account = None

    # ------------------------------------------------------------------ helpers
    def _fetch(self, ids: list[str]) -> list[Any]:
        """Fetch items by id; returns items or Exception objects in input order."""
        if not ids:
            return []
        return list(self.account.fetch(ids=[ItemId(id=i) for i in ids]))

    def _fetch_one(self, item_id: str):
        results = self._fetch([item_id])
        item = results[0] if results else None
        if item is None or isinstance(item, Exception):
            raise BackendError(f"Item not found: {item_id} ({item})")
        return item

    def folder(self, name: str | None, *, create: bool = False) -> Folder:
        """Resolve a folder by well-known name, path (``Inbox/Projects``) or unique display name."""
        account = self.account
        key = (name or "inbox").strip()
        attr = WELL_KNOWN_FOLDERS.get(key.lower())
        if attr:
            return getattr(account, attr)
        if "/" in key:
            parts = [p for p in key.split("/") if p]
            first = WELL_KNOWN_FOLDERS.get(parts[0].lower())
            node = getattr(account, first) if first else account.msg_folder_root
            rest = parts[1:] if first else parts
            for part in rest:
                match = next((c for c in node.children if (c.name or "").lower() == part.lower()), None)
                if match is None:
                    if not create:
                        raise BackendError(f"Folder not found: {key}")
                    match = Folder(parent=node, name=part)
                    match.save()
                node = match
            return node
        matches = [f for f in account.msg_folder_root.walk() if (f.name or "").lower() == key.lower()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            paths = ", ".join(self._folder_path(f) for f in matches)
            raise BackendError(f"Folder name {key!r} is ambiguous; use a path. Candidates: {paths}")
        if not create:
            raise BackendError(f"Folder not found: {key}. Use exchange_list_folders to see names.")
        folder = Folder(parent=account.inbox, name=key)
        folder.save()
        return folder

    def _folder_path(self, folder) -> str:
        root = self.account.msg_folder_root
        parts = []
        node = folder
        while node is not None and node.id != root.id:
            parts.append(node.name or "")
            node = getattr(node, "parent", None)
        return "/".join(reversed(parts))

    def _summary(self, item) -> dict[str, Any]:
        return {
            "id": item.id,
            "subject": getattr(item, "subject", None),
            "from": _mailbox_str(getattr(item, "sender", None)),
            "received": timeutil.fmt(getattr(item, "datetime_received", None), self.tz),
            "is_read": getattr(item, "is_read", None),
            "has_attachments": getattr(item, "has_attachments", None),
        }

    # ------------------------------------------------------------------ mailbox
    def whoami(self) -> dict[str, Any]:
        account = self.account
        return {
            "email": account.primary_smtp_address,
            "server": self.settings.server or "(autodiscover)",
            "auth": self.settings.auth,
            "timezone": str(self.tz.key if hasattr(self.tz, "key") else self.tz),
            "inbox_total": account.inbox.total_count,
            "inbox_unread": account.inbox.unread_count,
            "read_only": self.settings.read_only,
            "config_file": str(self.settings.env_file) if self.settings.env_file else None,
        }

    def list_folders(self) -> list[dict[str, Any]]:
        out = []
        for f in self.account.msg_folder_root.walk():
            if getattr(f, "folder_class", None) not in (None, "IPF.Note"):
                continue
            out.append({
                "path": self._folder_path(f),
                "total": getattr(f, "total_count", None),
                "unread": getattr(f, "unread_count", None),
            })
        return sorted(out, key=lambda r: r["path"].lower())

    def list_messages(self, folder: str, limit: int, offset: int = 0, unread_only: bool = False) -> list[dict]:
        qs = self.folder(folder).all().order_by("-datetime_received").only(*SUMMARY_FIELDS)
        if unread_only:
            qs = qs.filter(is_read=False)
        return [self._summary(i) for i in qs[offset : offset + limit]]

    def search_messages(
        self,
        *,
        folder: str,
        query: str = "",
        subject: str = "",
        sender: str = "",
        days_back: int = 30,
        limit: int = 20,
        scan_limit: int = 2000,
    ) -> dict[str, Any]:
        qs = self.folder(folder).all()
        if days_back:
            qs = qs.filter(datetime_received__gte=timeutil.now(self.tz) - timedelta(days=days_back))
        if subject:
            qs = qs.filter(subject__icontains=subject)
        if query:
            qs = qs.filter(Q(subject__icontains=query) | Q(body__icontains=query))
        qs = qs.order_by("-datetime_received").only(*SUMMARY_FIELDS)
        needle = sender.lower().strip()
        hits, scanned = [], 0
        for item in qs[:scan_limit] if needle else qs[:limit]:
            scanned += 1
            if needle:
                mb = getattr(item, "sender", None)
                hay = f"{getattr(mb, 'name', '') or ''} {getattr(mb, 'email_address', '') or ''}".lower()
                if needle not in hay:
                    continue
            hits.append(self._summary(item))
            if len(hits) >= limit:
                break
        return {"count": len(hits), "scanned": scanned, "messages": hits,
                "truncated_scan": bool(needle) and scanned >= scan_limit}

    def get_message(self, item_id: str, max_chars: int = 20000) -> dict[str, Any]:
        item = self._fetch_one(item_id)
        body = getattr(item, "text_body", None) or getattr(item, "body", None) or ""
        body = str(body)
        attachments = [
            {
                "name": getattr(a, "name", None),
                "size": getattr(a, "size", None),
                "content_type": getattr(a, "content_type", None),
                "is_inline": getattr(a, "is_inline", None),
            }
            for a in (getattr(item, "attachments", None) or [])
        ]
        return {
            **self._summary(item),
            "to": [_mailbox_str(r) for r in (getattr(item, "to_recipients", None) or [])],
            "cc": [_mailbox_str(r) for r in (getattr(item, "cc_recipients", None) or [])],
            "item_class": getattr(item, "item_class", None),
            "attachments": attachments,
            "body": body[:max_chars],
            "body_truncated": len(body) > max_chars,
        }

    def mark_read(self, ids: list[str], is_read: bool) -> dict[str, Any]:
        done, failed = [], []
        for iid, item in zip(ids, self._fetch(ids), strict=False):
            if item is None or isinstance(item, Exception):
                failed.append({"id": iid, "error": str(item)})
                continue
            item.is_read = is_read
            item.save(update_fields=["is_read"])
            done.append(item.subject)
        return {"updated": done, "failed": failed}

    def move_messages(self, ids: list[str], folder: str, create: bool) -> dict[str, Any]:
        target = self.folder(folder, create=create)
        moved, failed = [], []
        for iid, item in zip(ids, self._fetch(ids), strict=False):
            if item is None or isinstance(item, Exception):
                failed.append({"id": iid, "error": str(item)})
                continue
            try:
                item.move(target)
                moved.append(item.subject)
            except Exception as exc:  # noqa: BLE001
                failed.append({"id": iid, "subject": item.subject, "error": str(exc)})
        return {"folder": self._folder_path(target), "moved": moved, "failed": failed}

    def trash_messages(self, ids: list[str], dry_run: bool) -> dict[str, Any]:
        items = self._fetch(ids)
        preview, failed = [], []
        for iid, item in zip(ids, items, strict=False):
            if item is None or isinstance(item, Exception):
                failed.append({"id": iid, "error": str(item)})
            else:
                preview.append({"id": iid, "subject": item.subject})
        if dry_run:
            return {"dry_run": True, "would_trash": preview, "failed": failed}
        trashed = []
        for iid, item in zip(ids, items, strict=False):
            if item is None or isinstance(item, Exception):
                continue
            try:
                item.move_to_trash()
                trashed.append(item.subject)
            except Exception as exc:  # noqa: BLE001
                failed.append({"id": iid, "error": str(exc)})
        return {"dry_run": False, "trashed": trashed, "failed": failed}

    # ------------------------------------------------------------------ sending
    def _check_local_file(self, path: str) -> Path:
        p = Path(path).expanduser().resolve()
        allowed = self.settings.attachment_dirs
        if not allowed:
            raise BackendError(
                "Local file attachments are disabled. Set EXCHANGE_ATTACHMENT_DIRS to the folders "
                "that may be attached (comma-separated)."
            )
        if not any(p == d or d in p.parents for d in allowed):
            raise BackendError(f"{p} is outside EXCHANGE_ATTACHMENT_DIRS.")
        if not p.is_file():
            raise BackendError(f"File not found: {p}")
        return p

    def _register_deferred(self) -> None:
        if self._deferred_registered:
            return
        from exchangelib import ExtendedProperty

        class DeferredDeliveryTime(ExtendedProperty):
            property_tag = 0x3FEF  # PidTagDeferredDeliveryTime
            property_type = "SystemTime"

        try:
            Message.register("deferred_delivery_time", DeferredDeliveryTime)
        except ValueError:
            pass  # already registered in this process
        self._deferred_registered = True

    def compose(
        self,
        *,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        body_is_html: bool = False,
        attachments: list[str] | None = None,
        inline_images: dict[str, str] | None = None,
        signature: str | None = None,
        send_at: str | None = None,
        mode: str = "send",
        dry_run: bool = True,
    ) -> dict[str, Any]:
        errors: list[str] = []
        files: list[Path] = []
        inline: dict[str, Path] = {}
        for path in attachments or []:
            try:
                files.append(self._check_local_file(path))
            except BackendError as exc:
                errors.append(str(exc))
        for cid, path in (inline_images or {}).items():
            try:
                inline[cid] = self._check_local_file(path)
            except BackendError as exc:
                errors.append(str(exc))
        if signature:
            if not body_is_html:
                errors.append("signature requires body_is_html=true.")
            else:
                try:
                    sig_html, sig_images = load_signature(signature, self.settings.signature_dir)
                    body = body + "<br>" + sig_html
                    inline.update({cid: Path(p) for cid, p in sig_images.items()})
                except (FileNotFoundError, ValueError) as exc:
                    errors.append(str(exc))
        total = sum(p.stat().st_size for p in files if p.is_file()) + sum(
            p.stat().st_size for p in inline.values() if p.is_file()
        )
        limit = self.settings.max_attachment_mb * 1024 * 1024
        if total > limit:
            errors.append(f"Attachments total {total / 1048576:.1f} MB > limit {self.settings.max_attachment_mb} MB.")
        send_at_dt = None
        if send_at:
            if mode != "send":
                errors.append("send_at only applies when mode='send'.")
            else:
                send_at_dt = timeutil.parse_datetime(send_at, self.tz)
                if send_at_dt <= timeutil.now(self.tz) + timedelta(minutes=1):
                    errors.append(f"send_at {send_at} is in the past or less than a minute away.")

        preview = {
            "mode": mode,
            "from": self.settings.email,
            "to": to,
            "cc": cc or [],
            "bcc": bcc or [],
            "subject": subject,
            "body_chars": len(body),
            "body_is_html": body_is_html,
            "attachments": [{"name": p.name, "bytes": p.stat().st_size} for p in files if p.is_file()],
            "inline_images": sorted(inline),
            "send_at": timeutil.fmt(send_at_dt, self.tz) if send_at_dt else ("now" if mode == "send" else None),
        }
        if errors:
            raise BackendError("; ".join(errors) + f" | preview={preview}")
        if dry_run:
            return {"dry_run": True, "preview": preview}

        account = self.account
        msg = Message(
            account=account,
            folder=account.drafts if mode == "draft" else account.sent,
            subject=subject,
            body=HTMLBody(body) if body_is_html else body,
            to_recipients=[Mailbox(email_address=e) for e in to],
            cc_recipients=[Mailbox(email_address=e) for e in (cc or [])] or None,
            bcc_recipients=[Mailbox(email_address=e) for e in (bcc or [])] or None,
        )
        if send_at_dt is not None:
            from exchangelib import UTC

            self._register_deferred()
            msg.deferred_delivery_time = send_at_dt.astimezone(UTC)
        for p in files:
            msg.attach(FileAttachment(name=p.name, content=p.read_bytes()))
        for cid, p in inline.items():
            msg.attach(FileAttachment(name=p.name, content=p.read_bytes(), is_inline=True, content_id=cid))
        if mode == "draft":
            msg.save()
            return {"dry_run": False, "saved_draft": True, "id": msg.id, "preview": preview}
        msg.send_and_save()
        return {"dry_run": False, "sent": True, "scheduled": send_at_dt is not None, "preview": preview}

    def reply_or_forward(
        self,
        *,
        item_id: str,
        action: str,
        body: str,
        to: list[str] | None = None,
        cc: list[str] | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        item = self._fetch_one(item_id)
        subj = item.subject or ""
        prefix = {"reply": "RE: ", "reply_all": "RE: ", "forward": "FW: "}[action]
        new_subject = subj if subj.upper().startswith(prefix.strip().upper()) else prefix + subj
        if action == "forward" and not to:
            raise BackendError("forward requires at least one 'to' recipient.")
        if action == "reply":
            recipients = to or [getattr(item.sender, "email_address", None)]
        elif action == "reply_all":
            recipients = ["(original sender and all recipients)"]
        else:
            recipients = to
        preview = {"action": action, "subject": new_subject, "to": recipients, "cc": cc or [], "body_chars": len(body)}
        if dry_run:
            return {"dry_run": True, "preview": preview}
        if action == "reply":
            item.reply(subject=new_subject, body=body,
                       to_recipients=[Mailbox(email_address=e) for e in to] if to else None,
                       cc_recipients=[Mailbox(email_address=e) for e in cc] if cc else None)
        elif action == "reply_all":
            item.reply_all(subject=new_subject, body=body)
        else:
            item.forward(subject=new_subject, body=body, to_recipients=[Mailbox(email_address=e) for e in to],
                         cc_recipients=[Mailbox(email_address=e) for e in cc] if cc else None)
        return {"dry_run": False, "sent": True, "preview": preview}

    # ------------------------------------------------------------------ directory
    def resolve_names(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        results = self.account.protocol.resolve_names(names=[query], return_full_contact_data=False)
        out = []
        for r in list(results)[:limit]:
            if isinstance(r, Exception):
                continue
            mb = r[0] if isinstance(r, tuple) else r
            out.append({"name": getattr(mb, "name", None), "email": getattr(mb, "email_address", None),
                        "type": getattr(mb, "mailbox_type", None)})
        return out

    # ------------------------------------------------------------------ calendar
    def calendar_folder(self, name: str | None):
        cal = self.account.calendar
        if not name:
            return cal
        for child in cal.children:
            if (child.name or "").lower() == name.lower():
                return child
        for f in self.account.root.walk():
            if getattr(f, "folder_class", "") == "IPF.Appointment" and (f.name or "").lower() == name.lower():
                return f
        raise BackendError(f"Calendar {name!r} not found. Use exchange_list_calendars.")

    def list_calendars(self) -> list[dict[str, Any]]:
        out = []
        for f in self.account.root.walk():
            if getattr(f, "folder_class", None) == "IPF.Appointment":
                out.append({"name": f.name, "total": getattr(f, "total_count", None)})
        return out

    def _event(self, ev) -> dict[str, Any]:
        organizer = getattr(getattr(ev, "organizer", None), "email_address", None)
        return {
            "id": ev.id,
            "subject": getattr(ev, "subject", None),
            "start": timeutil.fmt(getattr(ev, "start", None), self.tz),
            "end": timeutil.fmt(getattr(ev, "end", None), self.tz),
            "all_day": getattr(ev, "is_all_day", None),
            "location": getattr(ev, "location", None),
            "organizer": organizer,
            "is_organizer": bool(organizer) and organizer.lower() == self.settings.email.lower(),
            "attendees": len(getattr(ev, "required_attendees", None) or [])
            + len(getattr(ev, "optional_attendees", None) or []),
            "is_recurring": getattr(ev, "is_recurring", None),
        }

    def list_events(self, start, end, calendar: str | None = None, subject: str = "") -> list[dict[str, Any]]:
        items = self.calendar_folder(calendar).view(start=start, end=end).order_by("start")
        needle = subject.lower().strip()
        return [self._event(e) for e in items if not needle or needle in (e.subject or "").lower()]

    def free_busy(self, emails: list[str], start, end) -> dict[str, dict[str, Any]]:
        """Return ``{email: {"status": free|busy|unknown, "busy": [...], "error": ...}}`` using GetUserAvailability.

        Free/busy works without delegate access to the other mailboxes.
        """
        result: dict[str, dict[str, Any]] = {}
        protocol = self.account.protocol
        for i in range(0, len(emails), FREE_BUSY_CHUNK):
            chunk = emails[i : i + FREE_BUSY_CHUNK]
            try:
                views = list(protocol.get_free_busy_info(
                    accounts=[(e, "Required", False) for e in chunk], start=start, end=end, requested_view="Detailed",
                ))
            except Exception as exc:  # noqa: BLE001
                for e in chunk:
                    result[e] = {"status": "unknown", "busy": [], "error": str(exc)}
                continue
            for email, view in zip(chunk, views, strict=False):
                if isinstance(view, Exception):
                    result[email] = {"status": "unknown", "busy": [], "error": str(view)}
                    continue
                busy = []
                for ev in getattr(view, "calendar_events", None) or []:
                    if getattr(ev, "busy_type", None) in ("Free", "WorkingElsewhere"):
                        continue
                    if ev.end <= start or ev.start >= end:
                        continue
                    details = getattr(ev, "details", None)
                    busy.append({
                        "start": timeutil.fmt(ev.start, self.tz),
                        "end": timeutil.fmt(ev.end, self.tz),
                        "busy_type": ev.busy_type,
                        "subject": getattr(details, "subject", None) if details else None,
                    })
                result[email] = {"status": "busy" if busy else "free", "busy": busy}
        return result

    def _find_events(self, *, item_id: str | None, subject: str | None, start_date: str | None,
                     end_date: str | None, calendar: str | None, limit: int) -> list:
        if item_id:
            return [self._fetch_one(item_id)]
        if not (start_date and end_date):
            raise BackendError("Provide item_id, or both start_date and end_date (optionally with subject_contains).")
        lo, hi = timeutil.day_bounds(timeutil.parse_date(start_date), timeutil.parse_date(end_date), self.tz)
        needle = (subject or "").lower()
        items = [e for e in self.calendar_folder(calendar).view(start=lo, end=hi).order_by("start")
                 if not needle or needle in (e.subject or "").lower()]
        return items[: limit + 1]

    def create_event(
        self,
        *,
        subject: str,
        start: str,
        end: str | None = None,
        duration_minutes: int | None = None,
        all_day: bool = False,
        attendees: list[str] | None = None,
        optional_attendees: list[str] | None = None,
        room_email: str | None = None,
        location: str | None = None,
        body: str = "",
        body_is_html: bool = False,
        reminder_minutes: int | None = 15,
        calendar: str | None = None,
        busy_status: str = "Busy",
        dry_run: bool = True,
    ) -> dict[str, Any]:
        if all_day:
            d0 = timeutil.parse_date(start[:10])
            d1 = timeutil.parse_date(end[:10]) if end else d0
            if d1 < d0:
                raise BackendError("end must not be before start.")
            ev_start, ev_end = EWSDate.from_date(d0), EWSDate.from_date(d1 + timedelta(days=1))
        else:
            ev_start = timeutil.parse_datetime(start, self.tz)
            if end:
                ev_end = timeutil.parse_datetime(end, self.tz)
            else:
                ev_end = ev_start + timedelta(minutes=duration_minutes or 60)
            if ev_end <= ev_start:
                raise BackendError("end must be after start.")

        required = [Attendee(mailbox=Mailbox(email_address=e)) for e in attendees or []]
        optional = [Attendee(mailbox=Mailbox(email_address=e)) for e in optional_attendees or []]
        resources: list[Attendee] = []
        room: Room | None = None
        if room_email:
            room = self.rooms.get(room_email) or Room(email=room_email.lower(), source="adhoc")
            target = required if room.invite_as == "attendee" else resources
            target.append(Attendee(mailbox=Mailbox(email_address=room.email)))
            if not location:
                location = room.name if room.name and room.name != room.email else room.email
        is_meeting = bool(required or optional or resources)
        folder = self.calendar_folder(calendar)
        preview = {
            "subject": subject,
            "start": timeutil.fmt(ev_start, self.tz),
            "end": timeutil.fmt(ev_end, self.tz) if not all_day else timeutil.fmt(ev_end - timedelta(days=1)),
            "all_day": all_day,
            "calendar": folder.name,
            "attendees": [a.mailbox.email_address for a in required],
            "optional_attendees": [a.mailbox.email_address for a in optional],
            "room": room.to_dict() if room else None,
            "location": location,
            "sends_invitations": is_meeting,
        }
        if dry_run:
            conflicts = []
            if not all_day:
                try:
                    conflicts = [self._event(e)["subject"] for e in self.account.calendar.view(start=ev_start, end=ev_end)]
                except Exception:  # noqa: BLE001, S110 - preview only
                    pass
            return {"dry_run": True, "preview": preview, "my_conflicts": conflicts}
        item = CalendarItem(
            account=self.account,
            folder=folder,
            subject=subject,
            start=ev_start,
            end=ev_end,
            is_all_day=all_day,
            body=HTMLBody(body) if body_is_html else body,
            location=location,
            required_attendees=required or None,
            optional_attendees=optional or None,
            resources=resources or None,
            legacy_free_busy_status=busy_status,
            reminder_is_set=reminder_minutes is not None,
            reminder_minutes_before_start=reminder_minutes or 0,
        )
        item.save(send_meeting_invitations="SendToAllAndSaveCopy" if is_meeting else "SendToNone")
        return {"dry_run": False, "created": True, "id": item.id, "preview": preview}

    def update_event(
        self,
        *,
        item_id: str | None,
        subject_contains: str | None,
        start_date: str | None,
        end_date: str | None,
        calendar: str | None,
        new_subject: str | None,
        new_location: str | None,
        new_start: str | None,
        new_end: str | None,
        body: str | None,
        body_mode: str,
        send_updates: bool,
        dry_run: bool,
    ) -> dict[str, Any]:
        items = self._find_events(item_id=item_id, subject=subject_contains, start_date=start_date,
                                  end_date=end_date, calendar=calendar, limit=20)
        if len(items) != 1:
            return {"error": f"Exactly one event must match; found {len(items)}. Narrow the filter or use item_id.",
                    "matched": [self._event(i) for i in items]}
        ev = items[0]
        info = self._event(ev)
        warnings = []
        if send_updates and not info["is_organizer"]:
            warnings.append("You are not the organizer; only your copy changes and nobody is notified.")
        changes: dict[str, Any] = {}
        if new_subject is not None:
            changes["subject"] = new_subject
        if new_location is not None:
            changes["location"] = new_location
        if new_start is not None:
            changes["start"] = timeutil.parse_datetime(new_start, self.tz)
        if new_end is not None:
            changes["end"] = timeutil.parse_datetime(new_end, self.tz)
        elif new_start is not None and ev.start and ev.end and not ev.is_all_day:
            changes["end"] = changes["start"] + (ev.end - ev.start)
        if "start" in changes and "end" in changes and changes["end"] <= changes["start"]:
            raise BackendError("new_end must be after new_start.")
        if body is not None:
            current = getattr(ev, "body", None) or ""
            if isinstance(current, HTMLBody):
                block = '<div style="white-space:pre-wrap">' + html.escape(body) + "</div>"
                if body_mode == "append":
                    s = str(current)
                    pos = s.lower().rfind("</body>")
                    changes["body"] = HTMLBody(s[:pos] + "<br>" + block + s[pos:] if pos >= 0 else s + "<br>" + block)
                else:
                    changes["body"] = HTMLBody(block)
            else:
                changes["body"] = (str(current) + "\n\n" + body) if (body_mode == "append" and current) else body
        if not changes:
            raise BackendError("Nothing to change: give new_subject, new_location, new_start/new_end or body.")
        shown = {k: (timeutil.fmt(v, self.tz) if k in ("start", "end") else
                     (f"{body_mode} {len(body or '')} chars" if k == "body" else v)) for k, v in changes.items()}
        if dry_run:
            return {"dry_run": True, "target": info, "changes": shown, "warnings": warnings}
        for k, v in changes.items():
            setattr(ev, k, v)
        notify = "SendToAllAndSaveCopy" if (send_updates and info["is_organizer"]) else "SendToNone"
        ev.save(update_fields=list(changes), send_meeting_invitations=notify)
        return {"dry_run": False, "updated": True, "target": info, "changes": shown, "notified": notify != "SendToNone",
                "warnings": warnings}

    def delete_events(
        self,
        *,
        item_id: str | None,
        subject_contains: str | None,
        start_date: str | None,
        end_date: str | None,
        calendar: str | None,
        send_cancellations: bool,
        max_delete: int,
        dry_run: bool,
    ) -> dict[str, Any]:
        items = self._find_events(item_id=item_id, subject=subject_contains, start_date=start_date,
                                  end_date=end_date, calendar=calendar, limit=max_delete)
        if len(items) > max_delete:
            return {"error": f"More than max_delete={max_delete} events match; narrow the filter or raise max_delete.",
                    "sample": [self._event(i) for i in items[:10]]}
        preview = [self._event(i) for i in items]
        if dry_run:
            return {"dry_run": True, "would_delete": preview,
                    "note": "Events go to Deleted Items (recoverable)."}
        deleted, errors = 0, []
        for item, info in zip(items, preview, strict=False):
            notify = "SendToAllAndSaveCopy" if (send_cancellations and info["is_organizer"] and info["attendees"]) \
                else "SendToNone"
            try:
                item.move_to_trash(send_meeting_cancellations=notify)
                deleted += 1
            except Exception as exc:  # noqa: BLE001
                errors.append({"subject": info["subject"], "error": str(exc)})
        return {"dry_run": False, "deleted": deleted, "items": preview, "errors": errors}

    # ------------------------------------------------------------------ meeting requests
    def _request_start(self, item):
        start = getattr(item, "start", None)
        if start is None:
            return None
        if hasattr(start, "tzinfo") and start.tzinfo is None:
            start = start.replace(tzinfo=self.tz)
        return start

    def list_meeting_requests(self, *, include_past: bool, include_cancellations: bool, limit: int) -> list[dict]:
        inbox = self.account.inbox
        classes = [MEETING_REQUEST_CLASS] + ([MEETING_CANCEL_CLASS] if include_cancellations else [])
        now = timeutil.now(self.tz)
        out = []
        for cls in classes:
            for item in inbox.filter(item_class=cls).order_by("-datetime_received")[:limit]:
                start = self._request_start(item)
                past = bool(start is not None and start < now)
                if past and not include_past:
                    continue
                out.append({
                    "id": item.id,
                    "kind": "cancellation" if cls == MEETING_CANCEL_CLASS else "request",
                    "subject": item.subject,
                    "start": timeutil.fmt(start, self.tz),
                    "organizer": _mailbox_str(getattr(item, "organizer", None) or getattr(item, "sender", None)),
                    "is_past": past,
                })
        return out[:limit]

    def cleanup_meeting_requests(self, *, ids: list[str] | None, dry_run: bool) -> dict[str, Any]:
        """Move past meeting requests/cancellations (or the given ids) to Deleted Items."""
        if ids:
            targets = []
            for iid, item in zip(ids, self._fetch(ids), strict=False):
                if item is None or isinstance(item, Exception):
                    continue
                if not isinstance(item, (MeetingRequest, MeetingCancellation)):
                    raise BackendError(f"{iid} is not a meeting request/cancellation.")
                targets.append(item)
        else:
            now = timeutil.now(self.tz)
            targets = []
            for cls in (MEETING_REQUEST_CLASS, MEETING_CANCEL_CLASS):
                for item in self.account.inbox.filter(item_class=cls):
                    start = self._request_start(item)
                    if start is not None and start < now:
                        targets.append(item)
        preview = [{"id": i.id, "subject": i.subject, "start": timeutil.fmt(self._request_start(i), self.tz)}
                   for i in targets]
        if dry_run:
            return {"dry_run": True, "would_trash": preview}
        errors = []
        for item in targets:
            try:
                item.move_to_trash()
            except Exception as exc:  # noqa: BLE001
                errors.append({"subject": item.subject, "error": str(exc)})
        return {"dry_run": False, "trashed": len(targets) - len(errors), "errors": errors}

    def respond_to_meeting(self, *, item_id: str, response: str, message: str, dry_run: bool) -> dict[str, Any]:
        item = self._fetch_one(item_id)
        if not isinstance(item, (MeetingRequest, CalendarItem)):
            raise BackendError("Item is not a meeting request or calendar item.")
        preview = {"subject": item.subject, "start": timeutil.fmt(self._request_start(item), self.tz),
                   "response": response, "message_chars": len(message)}
        if dry_run:
            return {"dry_run": True, "preview": preview}
        kwargs = {"body": message} if message else {}
        {"accept": item.accept, "tentative": item.tentatively_accept, "decline": item.decline}[response](**kwargs)
        return {"dry_run": False, "responded": True, "preview": preview}

    # ------------------------------------------------------------------ rooms
    def list_rooms(self) -> dict[str, Any]:
        source = self.settings.room_source
        rooms: dict[str, Room] = {}
        warnings: list[str] = []
        if source in ("roomlists", "both"):
            protocol = self.account.protocol
            get_lists = getattr(protocol, "get_roomlists", None) or getattr(protocol, "get_room_lists", None)
            try:
                for rl in get_lists():
                    for r in protocol.get_rooms(rl.email_address):
                        email = (r.email_address or "").lower()
                        rooms[email] = Room(email=email, name=r.name or email, group=rl.name, source="roomlist")
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"Room list lookup failed: {exc}")
        if source in ("catalog", "both"):
            try:
                for email, meta in self.rooms.load().items():
                    base = rooms.get(email)
                    if base:
                        meta = Room(**{**meta.__dict__, "group": meta.group or base.group,
                                       "name": meta.name if meta.name != meta.email else base.name,
                                       "source": "roomlist+catalog"})
                    rooms[email] = meta
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"Room catalog {self.settings.rooms_file} unreadable: {exc}")
        ordered = sorted(rooms.values(), key=lambda r: (r.capacity is None, -(r.capacity or 0), r.name))
        return {"rooms": [r.to_dict() for r in ordered], "warnings": warnings,
                "catalog_file": str(self.settings.rooms_file)}

    def find_available_rooms(self, start, end, min_capacity: int = 1) -> dict[str, Any]:
        listing = self.list_rooms()
        rooms: list[dict[str, Any]] = listing["rooms"]
        too_small = [r for r in rooms if r.get("capacity") is not None and r["capacity"] < min_capacity]
        candidates = [r for r in rooms if r not in too_small]
        fb = self.free_busy([r["email"] for r in candidates], start, end) if candidates else {}
        available, busy, unknown = [], [], []
        for r in candidates:
            state = fb.get(r["email"], {"status": "unknown", "error": "no data"})
            if state["status"] == "free":
                available.append(r)
            elif state["status"] == "busy":
                busy.append({**r, "busy": state["busy"]})
            else:
                unknown.append({**r, "error": state.get("error")})
        # Smallest room that fits first; unknown capacity last.
        available.sort(key=lambda r: (r.get("capacity") is None, r.get("capacity") or 0))
        return {
            "window": f"{timeutil.fmt(start, self.tz)} - {timeutil.fmt(end, self.tz)}",
            "min_capacity": min_capacity,
            "available": available,
            "busy": busy,
            "unknown": unknown,
            "too_small": [r["email"] for r in too_small],
            "capacity_unknown": [r["email"] for r in candidates if r.get("capacity") is None],
            "warnings": listing["warnings"],
        }
