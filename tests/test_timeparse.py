from datetime import date, time

import pytest

from app.timeparse import parse_choice, parse_day, parse_time, parse_when

TODAY = date(2026, 11, 2)  # a Monday


@pytest.mark.parametrize("text, expected", [
    ("3pm", time(15, 0)),
    ("3 PM", time(15, 0)),
    ("3:30pm", time(15, 30)),
    ("3.30 p.m.", time(15, 30)),
    ("11am", time(11, 0)),
    ("12pm", time(12, 0)),
    ("12am", time(0, 0)),
    ("15:45", time(15, 45)),
    ("09:00", time(9, 0)),
    ("07:30", time(7, 30)),
    ("at 3", time(15, 0)),
    ("at 10", time(10, 0)),
    ("4 o'clock", time(16, 0)),
    ("noon", time(12, 0)),
    ("around midday", time(12, 0)),
    ("haircut tomorrow at 2:15", time(14, 15)),
])
def test_parse_time(text, expected):
    assert parse_time(text)[0] == expected


@pytest.mark.parametrize("text", ["3", "on the 3rd", "3 nov", "13pm", "25:00", "at 3 nov", "haircut please"])
def test_parse_time_rejects_non_times(text):
    assert parse_time(text)[0] is None


@pytest.mark.parametrize("text, expected", [
    ("today", date(2026, 11, 2)),
    ("tomorrow", date(2026, 11, 3)),
    ("tmrw", date(2026, 11, 3)),
    ("day after tomorrow", date(2026, 11, 4)),
    ("friday", date(2026, 11, 6)),
    ("Fri", date(2026, 11, 6)),
    ("next friday", date(2026, 11, 6)),
    ("monday", date(2026, 11, 9)),       # a weekday name never means today...
    ("this monday", date(2026, 11, 2)),  # ...unless you say "this"
    ("3 nov", date(2026, 11, 3)),
    ("3rd of November", date(2026, 11, 3)),
    ("Nov 3", date(2026, 11, 3)),
    ("november 3rd, 2027", date(2027, 11, 3)),
    ("1 oct", date(2027, 10, 1)),        # already passed this year -> next year
    ("2026-11-20", date(2026, 11, 20)),
    ("03/11", date(2026, 11, 3)),        # DMY
    ("25/12/2026", date(2026, 12, 25)),
    ("the 5th", date(2026, 11, 5)),
    ("the 1st", date(2026, 12, 1)),      # already passed this month -> next month
])
def test_parse_day(text, expected):
    assert parse_day(text, TODAY) == expected


def test_parse_day_month_first():
    assert parse_day("11/03", TODAY, "MDY") == date(2026, 11, 3)


@pytest.mark.parametrize("text", ["31/02", "30 feb", "haircut", "3", "soon"])
def test_parse_day_rejects(text):
    assert parse_day(text, TODAY) is None


def test_parse_when_separates_day_and_time():
    when = parse_when("can I get a haircut on 3 nov at 3", TODAY)
    assert when.day == date(2026, 11, 3)
    assert when.time == time(15, 0)

    when = parse_when("friday 10:30am", TODAY)
    assert (when.day, when.time) == (date(2026, 11, 6), time(10, 30))

    # The time's digits aren't mistaken for a day of the month.
    when = parse_when("10:30", TODAY)
    assert (when.day, when.time) == (None, time(10, 30))


@pytest.mark.parametrize("text, count, expected", [
    ("2", 4, 1), (" 2. ", 4, 1), ("#3", 4, 2), ("option 1", 4, 0), ("4)", 4, 3),
    ("5", 4, None), ("0", 4, None), ("two", 4, None), ("2pm", 4, None),
])
def test_parse_choice(text, count, expected):
    assert parse_choice(text, count) == expected

def warm_cache(data=None):
    if data is None:
        return None
    return data

def send_notification(data=None):
    if data is None:
        return None
    return data

def warm_cache(data=None):
    if data is None:
        return None
    return data