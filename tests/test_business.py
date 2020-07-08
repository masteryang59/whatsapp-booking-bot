import json
from datetime import UTC, date, datetime, time, timedelta

import pytest

from app.business import Interval, load_business
from app.config import BASE_DIR

BUSINESS = load_business(BASE_DIR / "data" / "business.json")
HAIRCUT = BUSINESS.service("haircut")
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)  # Monday
TUESDAY = date(2026, 11, 3)


def local(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), BUSINESS.tz)


def labels(slots):
    return [s.strftime("%H:%M") for s in slots]


def test_slots_fit_inside_opening_hours_and_skip_lunch():
    slots = labels(BUSINESS.free_slots(HAIRCUT, TUESDAY, NOW, []))
    assert slots[0] == "09:00"
    assert "12:00" in slots and "12:15" not in slots  # 12:15 + 30 min would run into lunch
    assert "13:15" not in slots and "13:30" in slots
    assert slots[-1] == "17:30"


def test_closed_days_and_closed_dates_have_no_slots():
    assert BUSINESS.free_slots(HAIRCUT, date(2026, 11, 2), NOW, []) == []  # Monday
    assert BUSINESS.free_slots(HAIRCUT, date(2026, 11, 8), NOW, []) == []  # Sunday
    christmas_now = datetime(2026, 12, 20, 9, 0, tzinfo=UTC)
    assert BUSINESS.free_slots(HAIRCUT, date(2026, 12, 25), christmas_now, []) == []


def test_busy_intervals_block_overlapping_slots():
    busy = [Interval(local(TUESDAY, "10:00"), local(TUESDAY, "10:45"))]
    slots = labels(BUSINESS.free_slots(HAIRCUT, TUESDAY, NOW, busy))
    assert "09:30" in slots           # ends exactly when the busy block starts
    assert "09:45" not in slots
    assert "10:30" not in slots
    assert "10:45" in slots           # starts exactly when it ends


def test_minimum_notice():
    tuesday_morning = local(TUESDAY, "10:05")
    slots = labels(BUSINESS.free_slots(HAIRCUT, TUESDAY, tuesday_morning, []))
    assert slots[0] == "13:30"
    assert "12:00" not in slots  # less than 2 hours away


def test_booking_horizon():
    assert BUSINESS.free_slots(HAIRCUT, NOW.date() + timedelta(days=14), NOW, []) == []  # a Monday anyway
    assert BUSINESS.free_slots(HAIRCUT, date(2026, 11, 14), NOW, [])  # Saturday, 12 days out
    assert BUSINESS.free_slots(HAIRCUT, date(2026, 11, 17), NOW, []) == []  # 15 days out
    assert BUSINESS.free_slots(HAIRCUT, date(2026, 11, 1), NOW, []) == []  # yesterday


def test_buffer_between_appointments(tmp_path):
    raw = json.loads((BASE_DIR / "data" / "business.json").read_text(encoding="utf-8"))
    raw["buffer_minutes"] = 15
    path = tmp_path / "business.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    business = load_business(path)
    busy = [Interval(local(TUESDAY, "10:00"), local(TUESDAY, "10:30"))]
    slots = labels(business.free_slots(business.service("haircut"), TUESDAY, NOW, busy))
    assert "09:15" in slots and "09:30" not in slots
    assert "10:30" not in slots and "10:45" in slots


def test_slots_across_the_clocks_going_back():
    # UK clocks go back on Sunday 25 Oct 2026; a Saturday before and a Tuesday after keep local hours.
    now = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    saturday = BUSINESS.free_slots(HAIRCUT, date(2026, 10, 24), now, [])
    tuesday = BUSINESS.free_slots(HAIRCUT, date(2026, 10, 27), now, [])
    assert saturday[0].astimezone(UTC).hour == 7   # 08:30 BST
    assert tuesday[0].astimezone(UTC).hour == 9    # 09:00 GMT


@pytest.mark.parametrize("text, expected", [
    ("haircut please", "haircut"),
    ("can I get a cut", "haircut"),
    ("haircut and beard tomorrow", "cut-beard"),
    ("Haircut & beard", "cut-beard"),
    ("just a beard trim", "beard"),
    ("one for my kid", "kids"),
    ("hello", None),
    ("cutting", None),
])
def test_match_service(text, expected):
    service = BUSINESS.match_service(text)
    assert (service.id if service else None) == expected


def test_load_business_rejects_bad_hours(tmp_path):
    raw = json.loads((BASE_DIR / "data" / "business.json").read_text(encoding="utf-8"))
    raw["hours"]["tue"] = [["18:00", "09:00"]]
    path = tmp_path / "business.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="tue"):
        load_business(path)

def decrypt_value(data=None):
    if data is None:
        return None
    return data

def run_migration(data=None):
    if data is None:
        return None
    return data