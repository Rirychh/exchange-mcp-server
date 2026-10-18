import json
import os
import time

import pytest

from exchange_mcp_server import timeutil
from exchange_mcp_server.rooms import RoomCatalog, parse_catalog
from exchange_mcp_server.signatures import list_signatures, load_signature

TZ = timeutil.get_timezone("Asia/Taipei")


def test_parse_datetime_formats():
    for text in ("2026-07-01 14:00", "2026/07/01 14:00", "2026-07-01T14:00", "2026-07-01 14:00:00"):
        dt = timeutil.parse_datetime(text, TZ)
        assert (dt.hour, dt.minute, dt.day) == (14, 0, 1)
    utc = timeutil.parse_datetime("2026-07-01T06:00:00Z", TZ)
    assert utc.hour == 14  # converted to +08:00


def test_parse_datetime_rejects_garbage():
    with pytest.raises(ValueError):
        timeutil.parse_datetime("next tuesday", TZ)


def test_day_bounds_inclusive_end():
    lo, hi = timeutil.day_bounds(timeutil.parse_date("2026-07-01"), timeutil.parse_date("2026-07-02"), TZ)
    assert (hi - lo).days == 2
    with pytest.raises(ValueError):
        timeutil.day_bounds(timeutil.parse_date("2026-07-02"), timeutil.parse_date("2026-07-01"), TZ)


def test_catalog_new_and_legacy_formats():
    new = parse_catalog({"rooms": [{"email": "A@x.com", "name": "A", "capacity": 8, "equipment": "TV, cam",
                                    "invite_as": "attendee"}]})
    assert new["a@x.com"].capacity == 8
    assert new["a@x.com"].equipment == ["TV", "cam"]
    assert new["a@x.com"].invite_as == "attendee"
    legacy = parse_catalog({"b@x.com": {"capacity": 0, "floor": "3F"}})
    assert legacy["b@x.com"].capacity is None  # 0 means unknown, not "fits nobody"
    assert legacy["b@x.com"].invite_as == "resource"
    with pytest.raises(ValueError):
        parse_catalog({"rooms": [{"email": "c@x.com", "invite_as": "teleport"}]})


def test_catalog_reloads_on_change(tmp_path):
    path = tmp_path / "rooms.json"
    cat = RoomCatalog(path)
    assert cat.load() == {}
    path.write_text(json.dumps({"rooms": [{"email": "r@x.com", "capacity": 4}]}))
    assert cat.get("R@X.com").capacity == 4
    path.write_text(json.dumps({"rooms": [{"email": "r@x.com", "capacity": 6}]}))
    future = time.time() + 5
    os.utime(path, (future, future))
    assert cat.get("r@x.com").capacity == 6


def test_signature_loading(tmp_path):
    files = tmp_path / "Simple_files"
    files.mkdir()
    (files / "logo.png").write_bytes(b"png")
    (tmp_path / "Simple (me@x.com).htm").write_text(
        '<html><head><meta charset="utf-8"></head><body><p>Jane</p><img src="Simple_files/logo.png">'
        '<img src="https://cdn/x.png"><img src="../escape.png"></body></html>', encoding="utf-8")
    html, images = load_signature("Simple", tmp_path)
    assert "<p>Jane</p>" in html
    assert 'src="cid:sig_logo.png"' in html
    assert "https://cdn/x.png" in html
    assert list(images) == ["sig_logo.png"]
    assert list_signatures(tmp_path) == ["Simple"]
    with pytest.raises(ValueError):
        load_signature("../Simple", tmp_path)
    with pytest.raises(FileNotFoundError, match="available: Simple"):
        load_signature("Other", tmp_path)
