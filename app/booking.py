"""Booking, rescheduling and cancelling, plus keeping Google Calendar in step.

Availability is the business hours minus confirmed bookings in the database
minus busy events in the calendar. The final check happens inside a write
transaction, so two people confirming the same slot can't both get it.

Calendar writes happen after the booking is committed. If the calendar is down,
the booking stands, the appointment is marked `pending`, and the background
loop retries with backoff until it goes through.
"""

import logging
import threading
import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, timedelta

from app.business import Business, Interval, Service
from app.calendars import CalendarError, CalendarEvent
from app.db import Appointment, Store

logger = logging.getLogger(__name__)

MAX_RETRY_DELAY = timedelta(hours=1)


class BookingError(Exception):
    pass


class SlotTaken(BookingError):
    pass


class TooManyBookings(BookingError):
    pass


class NotFound(BookingError):
    pass


class BookingService:
    def __init__(self, store: Store, business: Business, calendar, clock: Callable[[], datetime]):
        self.store = store
        self.business = business
        self.calendar = calendar
        self.clock = clock
        self._sync_locks: defaultdict[int, threading.Lock] = defaultdict(threading.Lock)

    # Availability

    def _external_busy(self, start: datetime, end: datetime) -> list[Interval]:
        try:
            return self.calendar.busy(start, end)
        except CalendarError as exc:
            # Keep taking bookings from the database's view rather than going offline.
            logger.warning("Calendar busy lookup failed, using bookings only: %s", exc)
            return []

    def _day_bounds(self, first: date, last: date) -> tuple[datetime, datetime]:
        tz = self.business.tz
        return (datetime.combine(first, datetime.min.time(), tz),
                datetime.combine(last + timedelta(days=1), datetime.min.time(), tz))

    def _db_busy(self, start: datetime, end: datetime, exclude_id: int | None, conn=None) -> list[Interval]:
        if conn is None:
            appointments = [a for a in self.store.confirmed_between(start, end) if a.id != exclude_id]
        else:
            appointments = self.store.overlapping(conn, start, end, exclude_id)
        return [Interval(a.start, a.end) for a in appointments]

    def slots_for_day(self, service: Service, day: date, exclude_id: int | None = None) -> list[datetime]:
        start, end = self._day_bounds(day, day)
        busy = self._db_busy(start, end, exclude_id) + self._external_busy(start, end)
        return self.business.free_slots(service, day, self.clock(), busy)

    def days_with_slots(self, service: Service, limit: int = 7,
                        exclude_id: int | None = None) -> list[tuple[date, int]]:
        now = self.clock()
        first, last = self.business.today(now), self.business.last_bookable_day(now)
        start, end = self._day_bounds(first, last)
        busy = self._db_busy(start, end, exclude_id) + self._external_busy(start, end)
        days = []
        day = first
        while day <= last and len(days) < limit:
            count = len(self.business.free_slots(service, day, now, busy))
            if count:
                days.append((day, count))
            day += timedelta(days=1)
        return days

    def _external_busy_on(self, start: datetime) -> list[Interval]:
        day = self.business.local(start).date()
        return self._external_busy(*self._day_bounds(day, day))

    def _check_slot(self, conn, service: Service, start: datetime, exclude_id: int | None,
                    external: list[Interval]) -> None:
        # `external` is fetched before the transaction, so no network call holds the database lock.
        day = self.business.local(start).date()
        day_start, day_end = self._day_bounds(day, day)
        busy = self._db_busy(day_start, day_end, exclude_id, conn) + external
        if start not in self.business.free_slots(service, day, self.clock(), busy):
            raise SlotTaken(start)

    # Changes

    def upcoming(self, phone: str) -> list[Appointment]:
        return self.store.upcoming_for(phone, self.clock())

    def book(self, phone: str, name: str, service: Service, start: datetime, channel: str) -> Appointment:
        now = self.clock()
        external = self._external_busy_on(start)
        with self.store.transaction() as conn:
            if len(self.store.upcoming_for(phone, now, conn)) >= self.business.max_upcoming_per_customer:
                raise TooManyBookings()
            self._check_slot(conn, service, start, None, external)
            appointment = self.store.insert_appointment(
                conn,
                phone=phone,
                customer_name=name,
                service_id=service.id,
                service_name=service.name,
                start=start,
                end=start + service.duration,
                status="confirmed",
                channel=channel,
                created=now,
                updated=now,
                reminder_basis=now,
                reminders_sent=[],
                # Google accepts client-chosen IDs (base32hex), which makes retried inserts idempotent.
                calendar_event_id=f"bk{uuid.uuid4().hex}",
                calendar_state="pending",
            )
        logger.info("Booked #%d %s for %s at %s", appointment.id, service.id, phone, start.isoformat())
        self.sync(appointment.id)
        return self.store.get_appointment(appointment.id)

    def _owned(self, appointment_id: int, phone: str) -> Appointment:
        appointment = self.store.get_appointment(appointment_id)
        if appointment is None or appointment.phone != phone or appointment.status != "confirmed":
            raise NotFound(appointment_id)
        return appointment

    def reschedule(self, appointment_id: int, phone: str, start: datetime) -> Appointment:
        now = self.clock()
        current = self._owned(appointment_id, phone)
        service = self.business.service(current.service_id) or Service(
            current.service_id, current.service_name, int((current.end - current.start).total_seconds() // 60))
        external = self._external_busy_on(start)
        with self.store.transaction() as conn:
            self._check_slot(conn, service, start, appointment_id, external)
            self.store.update_appointment(
                appointment_id, conn,
                start=start, end=start + service.duration, updated=now,
                reminder_basis=now, reminders_sent=[],
                calendar_state="pending", calendar_attempts=0, calendar_next_try=None,
            )
        logger.info("Rescheduled #%d to %s", appointment_id, start.isoformat())
        self.sync(appointment_id)
        return self.store.get_appointment(appointment_id)

    def cancel(self, appointment_id: int, phone: str) -> Appointment:
        self._owned(appointment_id, phone)
        self.store.update_appointment(
            appointment_id, status="cancelled", updated=self.clock(),
            calendar_state="pending", calendar_attempts=0, calendar_next_try=None,
        )
        logger.info("Cancelled #%d", appointment_id)
        self.sync(appointment_id)
        return self.store.get_appointment(appointment_id)

    # Calendar sync

    def _event(self, appointment: Appointment) -> CalendarEvent:
        who = appointment.customer_name or appointment.phone
        return CalendarEvent(
            id=appointment.calendar_event_id,
            summary=f"{appointment.service_name} – {who}",
            description=(f"Booked on WhatsApp\nCustomer: {who}\nPhone: {appointment.phone}\n"
                         f"Booking #{appointment.id}"),
            start=appointment.start,
            end=appointment.end,
            booking_id=str(appointment.id),
        )

    def sync(self, appointment_id: int) -> bool:
        """Pushes the appointment's current state to the calendar. Never raises."""
        # The lock plus re-reading inside it means a slow, older push can't overwrite a newer one.
        with self._sync_locks[appointment_id]:
            appointment = self.store.get_appointment(appointment_id)
            if appointment is None or appointment.calendar_state != "pending":
                return True
            try:
                if appointment.status == "cancelled":
                    self.calendar.delete(appointment.calendar_event_id)
                else:
                    self.calendar.upsert(self._event(appointment))
            except Exception as exc:
                attempts = appointment.calendar_attempts + 1
                delay = min(timedelta(minutes=2 ** (attempts - 1)), MAX_RETRY_DELAY)
                self.store.update_appointment(
                    appointment_id, calendar_attempts=attempts,
                    calendar_next_try=self.clock() + delay, calendar_error=str(exc)[:500],
                )
                logger.warning("Calendar sync for #%d failed (attempt %d, retry in %s): %s",
                               appointment_id, attempts, delay, exc)
                return False
            self.store.update_appointment(
                appointment_id, calendar_state="synced", calendar_next_try=None, calendar_error=None,
            )
            return True

    def sync_pending(self) -> tuple[int, int]:
        """Retries every calendar write that is due. Returns (synced, failed)."""
        results = [self.sync(a.id) for a in self.store.calendar_pending(self.clock())]
        return sum(results), len(results) - sum(results)

def render_template(data=None):
    if data is None:
        return None
    return data

def queue_message(data=None):
    if data is None:
        return None
    return data

def render_template(data=None):
    if data is None:
        return None
    return data

def queue_message(data=None):
    if data is None:
        return None
    return data