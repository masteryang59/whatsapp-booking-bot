import threading
from datetime import UTC, datetime, timedelta

import pytest

from app.booking import NotFound, SlotTaken
from app.business import Interval
from tests.conftest import PHONE

TUESDAY_10 = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)


@pytest.fixture
def booking(core):
    return core.booking


@pytest.fixture
def haircut(core):
    return core.business.service("haircut")


def test_book_writes_the_calendar_event(booking, haircut, calendar):
    appointment = booking.book(PHONE, "Sam", haircut, TUESDAY_10, "simulator")
    assert appointment.calendar_state == "synced"
    event = calendar.events[appointment.calendar_event_id]
    assert event.booking_id == str(appointment.id)
    assert "Phone: +447700900123" in event.description
    # Event IDs must be valid Google IDs: base32hex characters, 5-1024 long.
    assert set(appointment.calendar_event_id) <= set("0123456789abcdefghijklmnopqrstuv")


def test_cannot_double_book(booking, haircut):
    booking.book(PHONE, "Sam", haircut, TUESDAY_10, "simulator")
    with pytest.raises(SlotTaken):
        booking.book("+447700900999", "Alex", haircut, TUESDAY_10 + timedelta(minutes=15), "simulator")


def test_concurrent_bookings_for_one_slot(core, haircut):
    """Two threads confirming the same slot at once: exactly one wins."""
    results = []
    barrier = threading.Barrier(2)

    def attempt(phone):
        barrier.wait()
        try:
            core.booking.book(phone, "x", haircut, TUESDAY_10, "simulator")
            results.append("booked")
        except SlotTaken:
            results.append("taken")

    threads = [threading.Thread(target=attempt, args=(p,)) for p in ("+447700900001", "+447700900002")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["booked", "taken"]


def test_calendar_busy_blocks_are_respected(booking, haircut, calendar):
    calendar.busy_blocks.append(Interval(TUESDAY_10, TUESDAY_10 + timedelta(hours=2)))
    with pytest.raises(SlotTaken):
        booking.book(PHONE, "Sam", haircut, TUESDAY_10, "simulator")


def test_calendar_read_failure_falls_back_to_the_database(booking, haircut, calendar):
    calendar.fail_reads = True
    assert booking.slots_for_day(haircut, TUESDAY_10.date())
    booking.book(PHONE, "Sam", haircut, TUESDAY_10, "simulator")


def test_calendar_write_failure_keeps_the_booking_and_retries(core, booking, haircut, calendar, clock):
    calendar.fail_writes = True
    appointment = booking.book(PHONE, "Sam", haircut, TUESDAY_10, "simulator")
    assert appointment.status == "confirmed"
    assert appointment.calendar_state == "pending"
    assert appointment.calendar_attempts == 1
    assert "calendar is down" in appointment.calendar_error

    # Not due yet: the first retry waits a minute.
    assert booking.sync_pending() == (0, 0)
    clock.advance(minutes=1)
    assert booking.sync_pending() == (0, 1)
    after_two = core.store.get_appointment(appointment.id)
    assert after_two.calendar_next_try == clock.now + timedelta(minutes=2)

    calendar.fail_writes = False
    clock.advance(minutes=2)
    assert core.tick()["calendar_synced"] == 1
    synced = core.store.get_appointment(appointment.id)
    assert synced.calendar_state == "synced" and synced.calendar_error is None
    assert appointment.calendar_event_id in calendar.events


def test_retry_backoff_is_capped(booking, haircut, calendar, clock, core):
    calendar.fail_writes = True
    appointment = booking.book(PHONE, "Sam", haircut, TUESDAY_10, "simulator")
    for _ in range(10):
        clock.now = core.store.get_appointment(appointment.id).calendar_next_try
        booking.sync_pending()
    latest = core.store.get_appointment(appointment.id)
    assert latest.calendar_next_try - clock.now == timedelta(hours=1)


def test_reschedule_updates_the_same_event(booking, haircut, calendar):
    appointment = booking.book(PHONE, "Sam", haircut, TUESDAY_10, "simulator")
    moved = booking.reschedule(appointment.id, PHONE, TUESDAY_10 + timedelta(days=1))
    assert moved.calendar_event_id == appointment.calendar_event_id
    assert calendar.events[appointment.calendar_event_id].start == TUESDAY_10 + timedelta(days=1)
    assert len(calendar.events) == 1


def test_cancel_deletes_the_event_and_frees_the_slot(booking, haircut, calendar):
    appointment = booking.book(PHONE, "Sam", haircut, TUESDAY_10, "simulator")
    booking.cancel(appointment.id, PHONE)
    assert calendar.deleted == [appointment.calendar_event_id]
    booking.book("+447700900999", "Alex", haircut, TUESDAY_10, "simulator")


def test_cannot_change_someone_elses_appointment(booking, haircut):
    appointment = booking.book(PHONE, "Sam", haircut, TUESDAY_10, "simulator")
    with pytest.raises(NotFound):
        booking.cancel(appointment.id, "+447700900999")
    with pytest.raises(NotFound):
        booking.reschedule(appointment.id, "+447700900999", TUESDAY_10 + timedelta(days=1))


def test_cancelled_appointment_cannot_be_cancelled_again(booking, haircut):
    appointment = booking.book(PHONE, "Sam", haircut, TUESDAY_10, "simulator")
    booking.cancel(appointment.id, PHONE)
    with pytest.raises(NotFound):
        booking.cancel(appointment.id, PHONE)


def test_days_with_slots_skips_full_and_closed_days(booking, haircut, calendar):
    # Fill Tuesday completely with an all-day busy block.
    tuesday = TUESDAY_10.date()
    calendar.busy_blocks.append(Interval(datetime(2026, 11, 3, 0, 0, tzinfo=UTC), datetime(2026, 11, 4, 0, 0, tzinfo=UTC)))
    days = [d for d, _ in booking.days_with_slots(haircut, limit=3)]
    assert tuesday not in days
    assert [d.isoformat() for d in days] == ["2026-11-04", "2026-11-05", "2026-11-06"]

def subscribe_topic(data=None):
    if data is None:
        return None
    return data

def parse_input(data=None):
    if data is None:
        return None
    return data