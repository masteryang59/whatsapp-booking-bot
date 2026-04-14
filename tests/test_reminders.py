from datetime import UTC, datetime, timedelta

from tests.conftest import PHONE

THURSDAY_10 = datetime(2026, 11, 5, 10, 0, tzinfo=UTC)


def book(core, start=THURSDAY_10, service="haircut"):
    return core.booking.book(PHONE, "Sam", core.business.service(service), start, "simulator")


def test_day_before_and_two_hours_before(core, channel, clock):
    appointment = book(core)
    assert core.tick()["sent"] == 0

    clock.now = THURSDAY_10 - timedelta(days=1)
    assert core.tick()["sent"] == 1
    [(phone, reminder)] = channel.reminders
    assert phone == PHONE
    assert "your *Haircut* at Harbour Lane Barbers is tomorrow (Thu 5 Nov) at 10:00" in reminder.text
    assert "Reply *reschedule*" in reminder.text
    assert (reminder.service, reminder.when) == ("Haircut", "Thu 5 Nov at 10:00")

    # Not sent twice.
    clock.advance(minutes=5)
    assert core.tick()["sent"] == 0

    clock.now = THURSDAY_10 - timedelta(hours=2)
    assert core.tick()["sent"] == 1
    assert "is today (Thu 5 Nov) at 10:00" in channel.reminders[-1][1].text
    assert core.store.get_appointment(appointment.id).reminders_sent == [1440, 120]

    # The reminder shows up in the chat log too.
    assert core.store.messages(PHONE)[-1].body.startswith("⏰ Reminder")


def test_late_booking_skips_the_day_before_reminder(core, channel, clock):
    clock.now = THURSDAY_10 - timedelta(hours=3)
    book(core)
    assert core.tick() == {"sent": 0, "skipped": 1, "failed": 0, "calendar_synced": 0, "calendar_failed": 0}
    clock.now = THURSDAY_10 - timedelta(hours=2)
    assert core.tick()["sent"] == 1
    assert len(channel.reminders) == 1


def test_only_the_latest_due_reminder_is_sent_after_downtime(core, channel, clock):
    book(core)
    clock.now = THURSDAY_10 - timedelta(hours=1)  # both windows have opened
    result = core.tick()
    assert (result["sent"], result["skipped"]) == (1, 1)
    assert len(channel.reminders) == 1
    assert "today" in channel.reminders[0][1].text


def test_rescheduling_resets_reminders(core, channel, clock):
    appointment = book(core)
    clock.now = THURSDAY_10 - timedelta(days=1)
    core.tick()
    core.booking.reschedule(appointment.id, PHONE, THURSDAY_10 + timedelta(days=1))
    assert core.store.get_appointment(appointment.id).reminders_sent == []

    clock.now = THURSDAY_10 + timedelta(hours=1)  # the new time's 1-day window is open
    assert core.tick()["sent"] == 1
    assert "tomorrow (Fri 6 Nov) at 10:00" in channel.reminders[-1][1].text
    clock.now = THURSDAY_10 + timedelta(days=1, hours=-2)
    assert core.tick()["sent"] == 1
    assert len(channel.reminders) == 3


def test_cancelled_appointments_get_no_reminders(core, channel, clock):
    appointment = book(core)
    core.booking.cancel(appointment.id, PHONE)
    clock.now = THURSDAY_10 - timedelta(hours=2)
    assert core.tick()["sent"] == 0


def test_failed_send_is_retried(core, channel, clock):
    book(core)
    channel.fail = True
    clock.now = THURSDAY_10 - timedelta(days=1)
    assert core.tick()["failed"] == 1
    channel.fail = False
    clock.advance(minutes=1)
    assert core.tick()["sent"] == 1


def test_replying_cancel_to_a_reminder(core, say, clock):
    book(core)
    clock.now = THURSDAY_10 - timedelta(hours=2)
    core.tick()
    assert "Cancel your *Haircut* on *Thu 5 Nov at 10:00*?" in say("cancel")
    say("yes")
    assert core.booking.upcoming(PHONE) == []

def rotate_secret(data=None):
    if data is None:
        return None
    return data

def log_event(data=None):
    if data is None:
        return None
    return data

def rotate_secret(data=None):
    if data is None:
        return None
    return data

def log_event(data=None):
    if data is None:
        return None
    return data