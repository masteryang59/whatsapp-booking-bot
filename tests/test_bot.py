from datetime import UTC, datetime, timedelta

from app.business import Interval
from tests.conftest import PHONE


def upcoming(core, phone=PHONE):
    return core.booking.upcoming(phone)


def test_greeting_shows_the_menu(say):
    reply = say("hi")
    assert "Hi Sam!" in reply
    assert "1. Book an appointment" in reply and "4. Cancel an appointment" in reply


def test_book_through_the_numbered_menus(say, core):
    assert "Which service" in say("1")
    reply = say("1")  # Haircut
    assert "Days with free times" in reply
    assert "1. Tue 3 Nov" in reply and "Mon" not in reply  # closed on Mondays
    reply = say("1")  # Tue 3 Nov
    assert "Free times on *Tue 3 Nov*" in reply and "1. 09:00" in reply
    reply = say("3")  # 10:00
    assert "Please confirm" in reply and "Tue 3 Nov at 10:00" in reply
    reply = say("yes")
    assert "✅ Booked! *Haircut* tomorrow (Tue 3 Nov) at 10:00" in reply
    assert "reminder 1 day and 2 hours before" in reply

    [appointment] = upcoming(core)
    assert appointment.service_id == "haircut"
    assert appointment.customer_name == "Sam Carter"
    assert appointment.start == datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
    assert appointment.end - appointment.start == timedelta(minutes=30)


def test_book_in_one_message(say, core, calendar):
    reply = say("Can I book a beard trim on friday at 2:30pm?")
    assert "Beard trim (15 min, £12)" in reply and "Fri 6 Nov at 14:30" in reply
    say("YES")
    [appointment] = upcoming(core)
    assert appointment.service_id == "beard"
    # The calendar event was written straight away.
    [event] = calendar.events.values()
    assert event.summary == "Beard trim – Sam Carter"
    assert event.start == appointment.start
    assert appointment.calendar_state == "synced"


def test_service_asked_when_missing_but_day_and_time_remembered(say):
    assert "Which service" in say("tomorrow at 11am please")
    reply = say("2")  # Haircut & beard
    assert "Please confirm" in reply and "Tue 3 Nov at 11:00" in reply


def test_time_only_then_day(say):
    reply = say("haircut at 4pm")
    assert "around 16:00" in reply
    reply = say("wednesday")
    assert "Please confirm" in reply and "Wed 4 Nov at 16:00" in reply


def test_taken_time_offers_the_closest_alternatives(say, core, calendar):
    tuesday_3pm = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
    calendar.busy_blocks.append(Interval(tuesday_3pm, tuesday_3pm + timedelta(hours=1)))
    reply = say("haircut tomorrow at 3pm")
    assert "15:00 on Tue 3 Nov isn't available" in reply
    assert "14:15" in reply and "14:30" in reply and "16:00" in reply
    reply = say("4")
    assert "Please confirm" in reply and "16:00" in reply  # 4th closest, sorted by time


def test_closed_day_explains_and_offers_days(say):
    reply = say("haircut on sunday")
    assert "We're closed on Sun 8 Nov" in reply
    assert "Days with free times" in reply


def test_past_and_far_future_days(say):
    assert "has already passed" in say("haircut on 2026-11-01 at 10:00")
    say("menu")
    assert "up to 14 days ahead" in say("haircut on 2026-12-15 at 10:00")


def test_typing_a_time_while_picking_from_the_list(say):
    say("haircut tomorrow")
    reply = say("14:45")
    assert "Please confirm" in reply and "14:45" in reply


def test_no_goes_back_to_the_times(say, core):
    say("haircut tomorrow at 10am")
    reply = say("no")
    assert "Free times on *Tue 3 Nov*" in reply
    assert upcoming(core) == []


def test_menu_or_cancel_word_aborts_a_flow(say, core):
    say("haircut tomorrow")
    reply = say("cancel")
    assert reply.startswith("OK, I've stopped that.")
    assert core.store.get_session(PHONE) is None


def test_unclear_answers_get_a_hint(say):
    say("1")
    assert "number of a service" in say("something")
    say("1")
    assert "number from the list" in say("whenever")
    say("1")
    assert "number from 1 to" in say("dunno")


def test_list_appointments(say):
    assert "no upcoming appointments" in say("my appointments")
    say("haircut tomorrow at 10am")
    say("yes")
    reply = say("2")
    assert "*Haircut* — Tue 3 Nov at 10:00" in reply


def test_reschedule_single_appointment_in_one_message(say, core, calendar):
    say("haircut tomorrow at 10am")
    say("yes")
    reply = say("can I move it to thursday 6pm?")
    assert "Move your *Haircut* from Tue 3 Nov at 10:00 to *Thu 5 Nov at 18:00*" in reply
    reply = say("yes")
    assert "now on *Thu 5 Nov at 18:00*" in reply
    [appointment] = upcoming(core)
    assert appointment.start == datetime(2026, 11, 5, 18, 0, tzinfo=UTC)
    assert calendar.events[appointment.calendar_event_id].start == appointment.start


def test_reschedule_can_overlap_its_own_old_time(say, core):
    say("haircut tomorrow at 10am")
    say("yes")
    say("reschedule")
    reply = say("tomorrow 10:15")  # overlaps the current 10:00-10:30 booking, which is being moved
    assert "Move your *Haircut*" in reply
    say("yes")
    assert upcoming(core)[0].start.strftime("%H:%M") == "10:15"


def test_reschedule_picks_the_named_service(say):
    say("haircut tomorrow at 10am")
    say("yes")
    say("beard trim wednesday at 9:00")
    say("yes")
    reply = say("move my beard trim to friday at 11")
    assert "Move your *Beard trim* from Wed 4 Nov at 09:00 to *Fri 6 Nov at 11:00*" in reply


def test_cancel_with_several_appointments(say, core, calendar):
    say("haircut tomorrow at 10am")
    say("yes")
    say("beard trim wednesday at 9:00")
    say("yes")
    reply = say("4")
    assert "Which appointment would you like to cancel?" in reply
    assert "2. Beard trim — Wed 4 Nov at 09:00" in reply
    assert "Cancel your *Beard trim*" in say("2")
    assert "is cancelled" in say("yes")
    [left] = upcoming(core)
    assert left.service_id == "haircut"
    assert len(calendar.deleted) == 1


def test_cancel_then_keep(say, core):
    say("haircut tomorrow at 10am")
    say("yes")
    say("cancel")
    assert "kept your appointment" in say("no")
    assert len(upcoming(core)) == 1


def test_other_customers_dont_see_each_others_bookings(say, core):
    say("haircut tomorrow at 10am")
    say("yes")
    assert "no upcoming appointments" in say("my appointments", phone="+447700900999")
    # And the slot is gone for them.
    assert "10:00 on Tue 3 Nov isn't available" in say("haircut tomorrow at 10am", phone="+447700900999")


def test_upcoming_limit(say, core):
    for day in ("tuesday", "wednesday", "thursday"):
        say(f"haircut {day} at 10am")
        assert "Booked" in say("yes")
    say("haircut friday at 10am")
    assert "already have 3 upcoming appointments" in say("yes")
    assert len(upcoming(core)) == 3


def test_slot_taken_between_question_and_yes(say, core):
    say("haircut tomorrow at 10am")
    say("haircut tomorrow at 10am", phone="+447700900999")
    say("yes", phone="+447700900999")
    reply = say("yes")
    assert "someone just took that time" in reply
    assert "Free times on *Tue 3 Nov*" in reply
    assert upcoming(core) == []


def test_session_times_out(say, core, clock):
    say("haircut tomorrow")
    clock.advance(minutes=45)
    reply = say("3")
    assert "timed out" in reply
    assert upcoming(core) == []


def test_new_request_after_timeout_is_handled_normally(say, clock):
    say("haircut tomorrow")
    clock.advance(minutes=45)
    assert "Please confirm" in say("beard trim tomorrow at 11am")


def test_state_survives_a_restart(say, settings, clock, calendar, channel):
    from app.channels import Incoming
    from app.core import BookingBot

    say("haircut tomorrow at 10am")
    restarted = BookingBot(settings, clock=clock, calendar=calendar, channels={"simulator": channel})
    replies = restarted.handle(Incoming("simulator", "after-restart", PHONE, "Sam", "yes"))
    assert "Booked" in replies[0]


def test_unknown_text_and_thanks(say):
    assert "didn't catch that" in say("what's the meaning of life")
    assert "You're welcome" in say("thanks!")

def encrypt_field(data=None):
    if data is None:
        return None
    return data

def build_query(data=None):
    if data is None:
        return None
    return data

def encrypt_field(data=None):
    if data is None:
        return None
    return data

def build_query(data=None):
    if data is None:
        return None
    return data