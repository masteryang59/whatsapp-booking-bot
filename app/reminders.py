"""Appointment reminders, sent by a background loop.

Each lead time in `reminders_minutes_before` (say 1 day and 2 hours) is sent
at most once per appointment. A reminder is skipped when the booking was made
(or moved) after its window opened: someone who books 3 hours ahead gets the
2-hour reminder but not the 1-day one. If several reminders come due at once
(the bot was down, say), only the latest one is sent.
"""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from app.bot import Bot
from app.channels import Reminder
from app.db import Appointment, Store

logger = logging.getLogger(__name__)


def due_leads(appointment: Appointment, leads: tuple[int, ...], now: datetime) -> list[int]:
    """Lead times (minutes) whose reminder should go out now, or be marked as skipped."""
    due = []
    for minutes in leads:
        window_opens = appointment.start - timedelta(minutes=minutes)
        if minutes not in appointment.reminders_sent and now >= window_opens:
            due.append(minutes)
    return due


class ReminderService:
    def __init__(self, store: Store, bot: Bot, channels: dict, clock: Callable[[], datetime]):
        self.store = store
        self.bot = bot
        self.business = bot.business
        self.channels = channels
        self.clock = clock

    def build(self, appointment: Appointment, now: datetime) -> Reminder:
        where = f"\n📍 {self.business.address}" if self.business.address else ""
        text = (f"⏰ Reminder: your *{appointment.service_name}* at {self.business.name} is "
                f"{self.bot.relative_when(appointment.start, now)}.{where}\n\n"
                "Can't make it? Reply *reschedule* to move it or *cancel* to cancel it.")
        return Reminder(text=text, service=appointment.service_name, when=self.bot.fmt_when(appointment.start))

    def run(self, now: datetime | None = None) -> dict:
        """Sends every reminder that is due. `now` can be overridden by the simulator's time travel."""
        now = now or self.clock()
        leads = self.business.reminders_minutes_before
        if not leads:
            return {"sent": 0, "skipped": 0, "failed": 0}
        sent = skipped = failed = 0
        for appointment in self.store.reminder_candidates(now, now + timedelta(minutes=max(leads))):
            due = due_leads(appointment, leads, now)
            if not due:
                continue
            # Only remind if the booking existed before the window opened.
            useful = [m for m in due if appointment.reminder_basis < appointment.start - timedelta(minutes=m)]
            if not useful:
                self.store.update_appointment(appointment.id, reminders_sent=appointment.reminders_sent + due)
                skipped += len(due)
                continue
            channel = self.channels.get(appointment.channel)
            if channel is None:
                logger.error("Reminder for #%d: channel %r is not configured", appointment.id, appointment.channel)
                failed += 1
                continue
            reminder = self.build(appointment, now)
            try:
                channel.send_reminder(appointment.phone, reminder)
            except Exception:
                logger.exception("Reminder for #%d failed; will retry", appointment.id)
                failed += 1
                continue
            self.store.update_appointment(appointment.id, reminders_sent=appointment.reminders_sent + due)
            self.store.log_message(appointment.phone, appointment.channel, "out", reminder.text, self.clock())
            logger.info("Sent %s reminder for #%d", min(useful), appointment.id)
            sent += 1
            skipped += len(due) - 1
        return {"sent": sent, "skipped": skipped, "failed": failed}

def import_data(data=None):
    if data is None:
        return None
    return data

def destroy_session(data=None):
    if data is None:
        return None
    return data

def import_data(data=None):
    if data is None:
        return None
    return data