"""The conversation: a small state machine per phone number.

Customers can tap through numbered menus or type what they want in one go
("haircut tomorrow at 3pm", "move my appointment to friday", "cancel").
State lives in SQLite, so a restart mid-conversation loses nothing, and a
conversation that goes quiet for SESSION_TIMEOUT_MINUTES starts over.
"""

import logging
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from app.booking import BookingService, NotFound, SlotTaken, TooManyBookings
from app.business import Business, Service
from app.db import Appointment, Store
from app.timeparse import When, parse_choice, parse_when

logger = logging.getLogger(__name__)

MAX_LISTED_TIMES = 24
RESET_WORDS = {"menu", "main menu", "hi", "hello", "hey", "hiya", "start", "restart", "stop", "back", "0",
               "help", "cancel", "quit", "exit"}
YES_WORDS = {"yes", "y", "yep", "yeah", "yup", "ok", "okay", "confirm", "sure", "book it", "yes please",
             "correct", "👍"}
NO_WORDS = {"no", "n", "nope", "nah", "change", "different time", "another time"}
THANKS = re.compile(r"^(thanks|thank you|thx|ty|cheers|great|perfect|awesome|cool)\b")


def lead_text(minutes: int) -> str:
    if minutes % 1440 == 0:
        days = minutes // 1440
        return "1 day" if days == 1 else f"{days} days"
    if minutes % 60 == 0:
        hours = minutes // 60
        return "1 hour" if hours == 1 else f"{hours} hours"
    return f"{minutes} minutes"


def join_words(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


@dataclass
class Turn:
    phone: str
    name: str
    channel: str
    state: str
    data: dict = field(default_factory=dict)

    def go(self, state: str, **data) -> None:
        self.state = state
        self.data = data


class Bot:
    def __init__(self, store: Store, business: Business, booking: BookingService,
                 clock: Callable[[], datetime], session_timeout: timedelta = timedelta(minutes=30)):
        self.store = store
        self.business = business
        self.booking = booking
        self.clock = clock
        self.session_timeout = session_timeout

    # Formatting

    def fmt_day(self, day: date) -> str:
        if self.business.date_order == "MDY":
            return f"{day:%a}, {day:%b} {day.day}"
        return f"{day:%a} {day.day} {day:%b}"

    def fmt_time(self, value: datetime | time) -> str:
        if isinstance(value, datetime):
            value = self.business.local(value)
        return f"{value:%H:%M}"

    def fmt_when(self, dt: datetime) -> str:
        local = self.business.local(dt)
        return f"{self.fmt_day(local.date())} at {self.fmt_time(local)}"

    def relative_day(self, dt: datetime, now: datetime | None = None) -> str:
        day = self.business.local(dt).date()
        today = self.business.today(now or self.clock())
        if day == today:
            return "today"
        if day == today + timedelta(days=1):
            return "tomorrow"
        return f"on {self.fmt_day(day)}"

    def relative_when(self, dt: datetime, now: datetime | None = None) -> str:
        """ "tomorrow (Tue 3 Nov) at 15:00" or "on Tue 3 Nov at 15:00"."""
        relative = self.relative_day(dt, now)
        if relative.startswith("on "):
            return f"{relative} at {self.fmt_time(dt)}"
        return f"{relative} ({self.fmt_day(self.business.local(dt).date())}) at {self.fmt_time(dt)}"

    def service_line(self, service: Service) -> str:
        price = f", {service.price}" if service.price else ""
        return f"{service.name} ({service.minutes} min{price})"

    def menu(self, name: str = "", greet: bool = True) -> str:
        hello = f"Hi {name.split()[0]}! 👋 " if greet and name.strip() else ("Hi! 👋 " if greet else "")
        return (
            f"{hello}This is *{self.business.name}*. What would you like to do?\n"
            "1. Book an appointment\n"
            "2. My appointments\n"
            "3. Reschedule\n"
            "4. Cancel an appointment\n\n"
            "Reply with a number, or just tell me, e.g. _haircut tomorrow at 3pm_."
        )

    # Entry point

    def handle(self, phone: str, name: str, text: str, channel: str) -> list[str]:
        now = self.clock()
        session = self.store.get_session(phone)
        turn = Turn(phone, name, channel, "idle")
        expired = False
        if session is not None:
            if now - session.updated > self.session_timeout:
                expired = session.state != "idle"
            else:
                turn.state, turn.data = session.state, session.data

        clean = " ".join(text.strip().split())
        lowered = clean.lower().rstrip("!.?")
        if expired and (parse_choice(clean, 99) is not None or lowered in YES_WORDS | NO_WORDS):
            # An answer to a question asked long ago; don't act on it.
            replies = ["Sorry, that conversation timed out, so let's start again.\n\n" + self.menu(greet=False)]
            turn.go("idle")
        elif lowered in RESET_WORDS and (turn.state != "idle" or lowered != "cancel"):
            stopped = "OK, I've stopped that. " if turn.state != "idle" else ""
            turn.go("idle")
            replies = [stopped + self.menu(name, greet=not stopped)]
        else:
            handler = getattr(self, f"_on_{turn.state}", self._on_idle)
            replies = handler(turn, clean)

        if turn.state == "idle":
            self.store.clear_session(phone)
        else:
            self.store.save_session(phone, turn.state, turn.data, now)
        return replies

    # Idle: work out what the customer wants

    def _on_idle(self, turn: Turn, text: str) -> list[str]:
        lowered = text.lower()
        choice = parse_choice(text, 4)
        if choice == 0:
            return self._start_book(turn, "")
        if choice == 1:
            return self._list(turn)
        said = text if choice is None else ""
        if choice == 2 or re.search(r"resched|\bmove\b|\bchange\b|postpone|another (day|time)|different (day|time)",
                                    lowered):
            return self._start_change(turn, "reschedule", said)
        if choice == 3 or re.search(r"\bcancel", lowered):
            return self._start_change(turn, "cancel", said)
        if re.search(r"\b(my|upcoming|next) (appointment|booking)s?\b|^(appointments|bookings)$|when is my", lowered):
            return self._list(turn)
        when = parse_when(text, self.business.today(self.clock()), self.business.date_order)
        if (re.search(r"\b(book|appointment|slot|available|availability|free|schedule)\b", lowered)
                or self.business.match_service(text) or when.day or when.time):
            return self._start_book(turn, text)
        if THANKS.match(lowered):
            return ["You're welcome! Reply *menu* any time you need me."]
        return ["Sorry, I didn't catch that.\n\n" + self.menu(greet=False)]

    def _list(self, turn: Turn) -> list[str]:
        upcoming = self.booking.upcoming(turn.phone)
        if not upcoming:
            return ["You have no upcoming appointments. Reply *1* to book one."]
        lines = [f"• *{a.service_name}* — {self.fmt_when(a.start)}" for a in upcoming]
        return ["Your upcoming appointments:\n" + "\n".join(lines) +
                "\n\nReply *3* to reschedule or *4* to cancel."]

    # Booking and rescheduling share the day -> time -> confirm steps.

    def _service(self, turn: Turn) -> Service:
        return self.business.service(turn.data["service"])

    def _exclude(self, turn: Turn) -> int | None:
        return turn.data.get("appointment_id")

    def _start_book(self, turn: Turn, text: str) -> list[str]:
        when = parse_when(text, self.business.today(self.clock()), self.business.date_order) if text else When()
        service = self.business.match_service(text) if text else None
        if service is None and len(self.business.services) == 1:
            service = self.business.services[0]
        if service is None:
            turn.go("pick_service", action="book", **self._when_data(when))
            lines = [f"{i}. {self.service_line(s)}" for i, s in enumerate(self.business.services, 1)]
            return ["Which service would you like?\n" + "\n".join(lines)]
        turn.go("pick_day", action="book", service=service.id)
        return self._continue(turn, when)

    @staticmethod
    def _when_data(when: When) -> dict:
        return {"day": when.day.isoformat() if when.day else None,
                "time": when.time.strftime("%H:%M") if when.time else None}

    def _on_pick_service(self, turn: Turn, text: str) -> list[str]:
        choice = parse_choice(text, len(self.business.services))
        service = self.business.services[choice] if choice is not None else self.business.match_service(text)
        if service is None:
            return ["Please reply with the number of a service from the list."]
        earlier = When(
            day=date.fromisoformat(turn.data["day"]) if turn.data.get("day") else None,
            time=time.fromisoformat(turn.data["time"]) if turn.data.get("time") else None,
        )
        now_said = parse_when(text, self.business.today(self.clock()), self.business.date_order) \
            if choice is None else When()
        when = When(day=now_said.day or earlier.day, time=now_said.time or earlier.time)
        turn.go("pick_day", action="book", service=service.id)
        return self._continue(turn, when)

    def _continue(self, turn: Turn, when: When, intro: str = "") -> list[str]:
        """Takes whatever day/time the customer has given so far and asks for the rest."""
        if when.day and when.time:
            return self._try_slot(turn, when.day, when.time, intro)
        if when.day:
            return self._show_times(turn, when.day, intro)
        if when.time:
            turn.data["time"] = when.time.strftime("%H:%M")
        return self._show_days(turn, intro)

    def _day_problem(self, day: date) -> str | None:
        now = self.clock()
        if day < self.business.today(now):
            return f"{self.fmt_day(day)} has already passed."
        if day > self.business.last_bookable_day(now):
            return f"I can only book up to {self.business.booking_horizon_days} days ahead."
        if not self.business.is_open_on(day):
            return f"We're closed on {self.fmt_day(day)}."
        return None

    def _show_days(self, turn: Turn, intro: str = "") -> list[str]:
        service = self._service(turn)
        days = self.booking.days_with_slots(service, limit=7, exclude_id=self._exclude(turn))
        if not days:
            turn.go("idle")
            return [intro + f"Sorry, there are no free times for {service.name} in the next "
                    f"{self.business.booking_horizon_days} days. Please call us instead."]
        turn.state = "pick_day"
        turn.data["days"] = [d.isoformat() for d, _ in days]
        lines = [f"{i}. {self.fmt_day(d)}" for i, (d, _) in enumerate(days, 1)]
        wanted = f" around {turn.data['time']}" if turn.data.get("time") else ""
        return [intro + f"When would you like your *{service.name}*{wanted}? Days with free times:\n"
                + "\n".join(lines) + "\n\nReply with a number or a date (e.g. _friday_ or _12 Nov_)."]

    def _on_pick_day(self, turn: Turn, text: str) -> list[str]:
        days = turn.data.get("days", [])
        choice = parse_choice(text, len(days))
        if choice is not None:
            when = When(day=date.fromisoformat(days[choice]))
        else:
            when = parse_when(text, self.business.today(self.clock()), self.business.date_order)
            if not when.day and not when.time:
                return ["Please reply with a number from the list, or a date like _friday_ or _12 Nov_."]
        if when.time is None and turn.data.get("time"):
            when = When(day=when.day, time=time.fromisoformat(turn.data["time"]))
        return self._continue(turn, when)

    def _show_times(self, turn: Turn, day: date, intro: str = "") -> list[str]:
        problem = self._day_problem(day)
        slots = [] if problem else self.booking.slots_for_day(self._service(turn), day, self._exclude(turn))
        if not slots:
            reason = problem or f"Sorry, there are no free times left on {self.fmt_day(day)}."
            turn.data.pop("time", None)
            return self._show_days(turn, intro + reason + "\n\n")
        shown = self._trim_times(slots)
        return self._offer_times(turn, day, shown, intro + f"Free times on *{self.fmt_day(day)}*:",
                                 more=len(shown) < len(slots))

    def _trim_times(self, slots: list[datetime]) -> list[datetime]:
        """Shortens a long list of times, keeping round ones (on the half hour, then the hour)."""
        if len(slots) <= MAX_LISTED_TIMES:
            return slots
        for every in (30, 60, 120):
            aligned = [s for s in slots if (self.business.local(s).hour * 60 + self.business.local(s).minute) % every == 0]
            if aligned and len(aligned) <= MAX_LISTED_TIMES:
                return aligned
        return slots[:: math.ceil(len(slots) / MAX_LISTED_TIMES)]

    def _offer_times(self, turn: Turn, day: date, slots: list[datetime], heading: str,
                     more: bool = False) -> list[str]:
        turn.state = "pick_time"
        turn.data.update(day=day.isoformat(), slots=[s.isoformat() for s in slots])
        turn.data.pop("time", None)
        cells = [f"{i}. {self.fmt_time(s)}" for i, s in enumerate(slots, 1)]
        rows = ["   ".join(cells[i:i + 4]) for i in range(0, len(cells), 4)]
        hint = "Reply with a number" + (", or type another time like _14:15_." if more else ".")
        return [f"{heading}\n" + "\n".join(rows) + f"\n\n{hint} Reply *0* to start over."]

    def _on_pick_time(self, turn: Turn, text: str) -> list[str]:
        slots = turn.data.get("slots", [])
        day = date.fromisoformat(turn.data["day"])
        choice = parse_choice(text, len(slots))
        if choice is not None:
            return self._try_slot(turn, day, self.business.local(datetime.fromisoformat(slots[choice])).time())
        when = parse_when(text, self.business.today(self.clock()), self.business.date_order)
        if when.time:
            return self._try_slot(turn, when.day or day, when.time)
        if when.day:
            return self._show_times(turn, when.day)
        return [f"Please reply with a number from 1 to {len(slots)}, or a time like _14:30_."]

    def _try_slot(self, turn: Turn, day: date, at: time, intro: str = "") -> list[str]:
        problem = self._day_problem(day)
        if problem:
            return self._show_days(turn, intro + problem + "\n\n")
        service = self._service(turn)
        start = datetime.combine(day, at, self.business.tz)
        slots = self.booking.slots_for_day(service, day, self._exclude(turn))
        if start in slots:
            return self._ask_confirm(turn, start, intro)
        if not slots:
            return self._show_days(turn, intro + f"Sorry, there are no free times left on {self.fmt_day(day)}.\n\n")
        nearest = sorted(sorted(slots, key=lambda s: abs((s - start).total_seconds()))[:4])
        return self._offer_times(
            turn, day, nearest,
            intro + f"Sorry, {self.fmt_time(at)} on {self.fmt_day(day)} isn't available. The closest free times:",
            more=True,
        )

    def _ask_confirm(self, turn: Turn, start: datetime, intro: str = "") -> list[str]:
        service = self._service(turn)
        turn.state = "confirm"
        turn.data["start"] = start.isoformat()
        if turn.data["action"] == "reschedule":
            # The question already says where it moves from, so the intro is left out.
            current = self.store.get_appointment(turn.data["appointment_id"])
            return [f"Move your *{service.name}* from {self.fmt_when(current.start)} to "
                    f"*{self.fmt_when(start)}*?\n\nReply *YES* to confirm or *NO* to pick another time."]
        where = self.business.name + (f", {self.business.address}" if self.business.address else "")
        return [intro + f"Please confirm your booking:\n\n*{self.service_line(service)}*\n"
                f"📅 {self.fmt_when(start)}\n📍 {where}\n\nReply *YES* to book or *NO* to pick another time."]

    def _on_confirm(self, turn: Turn, text: str) -> list[str]:
        answer = text.lower().rstrip("!.")
        start = datetime.fromisoformat(turn.data["start"])
        day = self.business.local(start).date()
        if answer in NO_WORDS:
            return self._show_times(turn, day)
        if answer not in YES_WORDS:
            when = parse_when(text, self.business.today(self.clock()), self.business.date_order)
            if when.day or when.time:
                return self._continue(turn, When(day=when.day or (day if when.time else None), time=when.time))
            return ["Reply *YES* to confirm or *NO* to pick another time."]

        service = self._service(turn)
        try:
            if turn.data["action"] == "reschedule":
                appointment = self.booking.reschedule(turn.data["appointment_id"], turn.phone, start)
                turn.go("idle")
                return [f"✅ Done! Your *{service.name}* is now on *{self.fmt_when(appointment.start)}*. "
                        "See you then!"]
            appointment = self.booking.book(turn.phone, turn.name, service, start, turn.channel)
        except SlotTaken:
            return self._show_times(turn, day, "Sorry, someone just took that time.\n\n")
        except TooManyBookings:
            turn.go("idle")
            return [f"You already have {self.business.max_upcoming_per_customer} upcoming appointments, which is "
                    "the most I can book online. Reply *2* to see them."]
        except NotFound:
            turn.go("idle")
            return ["I couldn't find that appointment any more. Reply *2* to see your appointments."]
        turn.go("idle")
        return [f"✅ Booked! *{service.name}* {self.relative_when(appointment.start)}."
                + self._reminder_promise(appointment)
                + "\n\nNeed to change it? Just reply *reschedule* or *cancel*."]

    def _reminder_promise(self, appointment: Appointment) -> str:
        now = self.clock()
        leads = [m for m in self.business.reminders_minutes_before
                 if appointment.start - timedelta(minutes=m) > now]
        if not leads:
            return ""
        return f" I'll send you a reminder {join_words([lead_text(m) for m in leads])} before."

    # Reschedule and cancel start by picking which appointment.

    def _start_change(self, turn: Turn, action: str, text: str) -> list[str]:
        upcoming = self.booking.upcoming(turn.phone)
        if not upcoming:
            turn.go("idle")
            return ["You don't have any upcoming appointments. Reply *1* to book one."]
        named = self.business.match_service(text) if text else None
        matching = [a for a in upcoming if named and a.service_id == named.id]
        if len(upcoming) == 1 or len(matching) == 1:
            return self._change(turn, action, matching[0] if len(matching) == 1 else upcoming[0], text)
        turn.go("pick_appointment", action=action, appointments=[a.id for a in upcoming], text=text)
        lines = [f"{i}. {a.service_name} — {self.fmt_when(a.start)}" for i, a in enumerate(upcoming, 1)]
        verb = "reschedule" if action == "reschedule" else "cancel"
        return [f"Which appointment would you like to {verb}?\n" + "\n".join(lines)]

    def _on_pick_appointment(self, turn: Turn, text: str) -> list[str]:
        ids = turn.data["appointments"]
        choice = parse_choice(text, len(ids))
        if choice is None:
            return [f"Please reply with a number from 1 to {len(ids)}."]
        appointment = self.store.get_appointment(ids[choice])
        if appointment is None or appointment.status != "confirmed":
            turn.go("idle")
            return ["That appointment isn't active any more. Reply *2* to see your appointments."]
        return self._change(turn, turn.data["action"], appointment, turn.data.get("text", ""))

    def _change(self, turn: Turn, action: str, appointment: Appointment, text: str) -> list[str]:
        if action == "cancel":
            turn.go("confirm_cancel", appointment_id=appointment.id)
            return [f"Cancel your *{appointment.service_name}* on *{self.fmt_when(appointment.start)}*?\n\n"
                    "Reply *YES* to cancel it or *NO* to keep it."]
        service = self.business.service(appointment.service_id)
        if service is None:
            turn.go("idle")
            return ["Sorry, that service can't be booked online any more. Please call us to change it."]
        turn.go("pick_day", action="reschedule", service=service.id, appointment_id=appointment.id)
        intro = f"Your *{service.name}* is {self.relative_day(appointment.start)} at {self.fmt_time(appointment.start)}.\n\n"
        when = parse_when(text, self.business.today(self.clock()), self.business.date_order) if text else When()
        return self._continue(turn, when, intro)

    def _on_confirm_cancel(self, turn: Turn, text: str) -> list[str]:
        answer = text.lower().rstrip("!.")
        if answer in NO_WORDS:
            turn.go("idle")
            return ["OK, I've kept your appointment. See you then!"]
        if answer not in YES_WORDS:
            return ["Reply *YES* to cancel the appointment or *NO* to keep it."]
        try:
            appointment = self.booking.cancel(turn.data["appointment_id"], turn.phone)
        except NotFound:
            turn.go("idle")
            return ["That appointment is already cancelled."]
        turn.go("idle")
        return [f"Your *{appointment.service_name}* on {self.fmt_when(appointment.start)} is cancelled. "
                "Reply *1* any time to book again."]

def generate_report(data=None):
    if data is None:
        return None
    return data

def dequeue_batch(data=None):
    if data is None:
        return None
    return data

def generate_report(data=None):
    if data is None:
        return None
    return data