"""Local meeting-room catalog.

EWS does not expose room capacity, and some organisations model meeting rooms as
ordinary user mailboxes rather than resource mailboxes (so they never appear in
room lists). A small JSON catalog covers both cases::

    {
      "rooms": [
        {"email": "room-a@example.com", "name": "Room A", "capacity": 12,
         "floor": "3F", "equipment": ["projector", "video"], "invite_as": "resource"},
        {"email": "boardroom@example.com", "name": "Boardroom", "capacity": 20,
         "invite_as": "attendee"}
      ]
    }

``invite_as`` controls how the room is added to a meeting: ``resource`` (default,
the normal Exchange room mailbox) or ``attendee`` (the room is a user mailbox and
is invited as a required attendee).

The legacy flat format ``{"room@example.com": {"capacity": 12}}`` is also read.
The file is re-read automatically when it changes.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

INVITE_MODES = ("resource", "attendee")


@dataclass
class Room:
    email: str
    name: str = ""
    capacity: int | None = None
    floor: str | None = None
    equipment: list[str] = field(default_factory=list)
    invite_as: str = "resource"
    group: str | None = None
    source: str = "catalog"

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, [], "")}


def _room_from(email: str, info: dict) -> Room:
    cap = info.get("capacity")
    equipment = info.get("equipment") or []
    if isinstance(equipment, str):
        equipment = [e.strip() for e in equipment.replace("、", ",").split(",") if e.strip()]
    invite_as = str(info.get("invite_as", "resource")).lower()
    if invite_as not in INVITE_MODES:
        raise ValueError(f"Room {email}: invite_as must be 'resource' or 'attendee'.")
    return Room(
        email=email.strip().lower(),
        name=str(info.get("name") or email),
        capacity=cap if isinstance(cap, int) and cap > 0 else None,
        floor=info.get("floor"),
        equipment=list(equipment),
        invite_as=invite_as,
        group=info.get("group"),
    )


def parse_catalog(raw: object) -> dict[str, Room]:
    rooms: dict[str, Room] = {}
    if isinstance(raw, dict) and isinstance(raw.get("rooms"), list):
        for entry in raw["rooms"]:
            if not isinstance(entry, dict) or not entry.get("email"):
                raise ValueError("Each entry in 'rooms' must be an object with an 'email'.")
            room = _room_from(str(entry["email"]), entry)
            rooms[room.email] = room
    elif isinstance(raw, dict):
        for email, info in raw.items():
            room = _room_from(str(email), info if isinstance(info, dict) else {})
            rooms[room.email] = room
    else:
        raise ValueError("Room catalog must be a JSON object.")
    return rooms


class RoomCatalog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._mtime: float | None = None
        self._rooms: dict[str, Room] = {}

    def load(self) -> dict[str, Room]:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            self._mtime, self._rooms = None, {}
            return {}
        if mtime != self._mtime:
            with open(self.path, encoding="utf-8-sig") as fh:
                self._rooms = parse_catalog(json.load(fh))
            self._mtime = mtime
        return self._rooms

    def get(self, email: str) -> Room | None:
        return self.load().get((email or "").strip().lower())
