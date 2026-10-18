import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from exchangelib.items import MeetingRequest

from exchange_mcp_server import timeutil
from exchange_mcp_server.backend import BackendError, EwsBackend
from exchange_mcp_server.config import Settings

TZ = timeutil.get_timezone("Asia/Taipei")


@pytest.fixture
def backend(tmp_path):
    s = Settings(email="me@x.com", server="https://x/EWS/Exchange.asmx", password="p", timezone="Asia/Taipei",
                 rooms_file=tmp_path / "rooms.json", room_source="catalog", attachment_dirs=[tmp_path / "ok"])
    b = EwsBackend(s)
    b._account = MagicMock(primary_smtp_address="me@x.com")
    return b


def ev(start, end, busy="Busy", subject=None):
    return SimpleNamespace(start=start, end=end, busy_type=busy,
                           details=SimpleNamespace(subject=subject) if subject else None)


def test_free_busy_ignores_free_and_out_of_window(backend):
    lo = timeutil.parse_datetime("2026-07-01 10:00", TZ)
    hi = lo + timedelta(hours=1)
    views = [
        SimpleNamespace(calendar_events=[ev(lo, hi, "Free")]),
        SimpleNamespace(calendar_events=[ev(lo - timedelta(hours=2), lo), ev(lo, hi, "Tentative", "Sync")]),
        RuntimeError("no permission"),
    ]
    backend._account.protocol.get_free_busy_info.return_value = iter(views)
    fb = backend.free_busy(["a@x.com", "b@x.com", "c@x.com"], lo, hi)
    assert fb["a@x.com"]["status"] == "free"
    assert fb["b@x.com"]["status"] == "busy"
    assert fb["b@x.com"]["busy"][0]["subject"] == "Sync"
    assert len(fb["b@x.com"]["busy"]) == 1  # the event ending exactly at window start is not a conflict
    assert fb["c@x.com"]["status"] == "unknown"


def test_find_available_rooms_classifies(backend, tmp_path):
    (tmp_path / "rooms.json").write_text(json.dumps({"rooms": [
        {"email": "big@x.com", "capacity": 20}, {"email": "small@x.com", "capacity": 4},
        {"email": "mid@x.com", "capacity": 8}, {"email": "nocap@x.com"},
    ]}))
    lo = timeutil.parse_datetime("2026-07-01 10:00", TZ)
    hi = lo + timedelta(hours=1)
    backend.free_busy = MagicMock(return_value={
        "big@x.com": {"status": "free", "busy": []},
        "mid@x.com": {"status": "free", "busy": []},
        "nocap@x.com": {"status": "unknown", "busy": [], "error": "denied"},
    })
    res = backend.find_available_rooms(lo, hi, min_capacity=6)
    assert [r["email"] for r in res["available"]] == ["mid@x.com", "big@x.com"]  # smallest fit first
    assert res["too_small"] == ["small@x.com"]
    assert res["unknown"][0]["email"] == "nocap@x.com"
    assert "nocap@x.com" in res["capacity_unknown"]


def test_trash_dry_run_does_not_touch(backend):
    item = MagicMock(subject="Hello")
    backend._account.fetch.return_value = [item]
    out = backend.trash_messages(["id1"], dry_run=True)
    assert out["would_trash"] == [{"id": "id1", "subject": "Hello"}]
    item.move_to_trash.assert_not_called()
    backend._account.fetch.return_value = [item]
    backend.trash_messages(["id1"], dry_run=False)
    item.move_to_trash.assert_called_once()


def test_delete_events_moves_to_trash_not_hard_delete(backend):
    item = MagicMock(subject="Standup", id="e1", is_all_day=False, required_attendees=[], optional_attendees=[])
    item.organizer.email_address = "me@x.com"
    backend._account.fetch.return_value = [item]
    backend.delete_events(item_id="e1", subject_contains=None, start_date=None, end_date=None, calendar=None,
                          send_cancellations=False, max_delete=5, dry_run=False)
    item.move_to_trash.assert_called_once_with(send_meeting_cancellations="SendToNone")
    item.delete.assert_not_called()


def test_delete_events_requires_selector(backend):
    with pytest.raises(BackendError, match="start_date and end_date"):
        backend.delete_events(item_id=None, subject_contains="x", start_date=None, end_date=None, calendar=None,
                              send_cancellations=False, max_delete=5, dry_run=True)


def test_meeting_requests_past_filter(backend):
    now = timeutil.now(TZ)
    past = SimpleNamespace(id="p", subject="old", start=now - timedelta(days=2), organizer=None, sender=None)
    future = SimpleNamespace(id="f", subject="new", start=now + timedelta(days=2), organizer=None, sender=None)
    qs = MagicMock()
    qs.order_by.return_value = [past, future]
    backend._account.inbox.filter.return_value = qs
    ids = [r["id"] for r in backend.list_meeting_requests(include_past=False, include_cancellations=False, limit=10)]
    assert ids == ["f"]
    ids = [r["id"] for r in backend.list_meeting_requests(include_past=True, include_cancellations=False, limit=10)]
    assert ids == ["p", "f"]


def test_cleanup_rejects_non_meeting_ids(backend):
    backend._account.fetch.return_value = [MagicMock(spec=["subject"])]
    with pytest.raises(BackendError, match="not a meeting"):
        backend.cleanup_meeting_requests(ids=["x"], dry_run=True)
    req = MagicMock(spec=MeetingRequest)
    req.subject, req.id, req.start = "Plan", "m1", None
    backend._account.fetch.return_value = [req]
    assert backend.cleanup_meeting_requests(ids=["m1"], dry_run=True)["would_trash"][0]["id"] == "m1"


def test_attachments_need_allowlist(backend, tmp_path):
    ok = tmp_path / "ok"
    ok.mkdir()
    (ok / "a.txt").write_text("hi")
    (tmp_path / "secret.txt").write_text("no")
    preview = backend.compose(to=["y@x.com"], subject="s", body="b", attachments=[str(ok / "a.txt")])
    assert preview["dry_run"] and preview["preview"]["attachments"][0]["name"] == "a.txt"
    with pytest.raises(BackendError, match="outside"):
        backend.compose(to=["y@x.com"], subject="s", body="b", attachments=[str(tmp_path / "secret.txt")])
    with pytest.raises(BackendError, match="outside"):
        backend.compose(to=["y@x.com"], subject="s", body="b", attachments=[str(ok / ".." / "secret.txt")])
    backend.settings.attachment_dirs = []
    with pytest.raises(BackendError, match="disabled"):
        backend.compose(to=["y@x.com"], subject="s", body="b", attachments=[str(ok / "a.txt")])


def test_send_at_in_past_rejected(backend):
    with pytest.raises(BackendError, match="past"):
        backend.compose(to=["y@x.com"], subject="s", body="b", send_at="2001-01-01 09:00")


def test_create_event_dry_run_room_as_attendee(backend, tmp_path):
    (tmp_path / "rooms.json").write_text(json.dumps({"rooms": [
        {"email": "board@x.com", "name": "Boardroom", "capacity": 12, "invite_as": "attendee"}]}))
    backend._account.calendar.view.return_value = []
    backend._account.calendar.name = "Calendar"
    out = backend.create_event(subject="Review", start="2026-07-01 10:00", duration_minutes=30,
                               attendees=["a@x.com"], room_email="board@x.com")
    p = out["preview"]
    assert out["dry_run"] is True
    assert p["attendees"] == ["a@x.com", "board@x.com"]
    assert p["location"] == "Boardroom"
    assert p["end"] == "2026-07-01 10:30"


def test_create_all_day_event_preview(backend):
    backend._account.calendar.name = "Calendar"
    out = backend.create_event(subject="Milestone", start="2026-07-01", end="2026-07-03", all_day=True)
    assert out["preview"]["start"] == "2026-07-01"
    assert out["preview"]["end"] == "2026-07-03"
    assert out["preview"]["sends_invitations"] is False
