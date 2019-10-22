"""Wires the pieces together and handles one incoming message end to end."""

import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.booking import BookingService
from app.bot import Bot
from app.business import load_business
from app.calendars import GoogleCalendar, LocalCalendar, service_account_token_provider
from app.channels import Incoming, MetaChannel, SimulatorChannel, TwilioChannel
from app.config import Settings
from app.db import Store
from app.reminders import ReminderService

logger = logging.getLogger(__name__)

UNREADABLE = "Sorry, I can only read text messages. Reply *menu* to see what I can do."
ERROR = "Sorry, something went wrong on my side. Please try again in a minute."


def utc_now() -> datetime:
    return datetime.now(UTC)


class BookingBot:
    def __init__(self, settings: Settings, clock: Callable[[], datetime] = utc_now,
                 calendar=None, channels: dict | None = None):
        self.settings = settings
        self.clock = clock
        self.business = load_business(settings.business_config)
        self.store = Store(settings.db_path)

        if calendar is None:
            if settings.google_enabled:
                if not settings.google_service_account_file:
                    raise ValueError("GOOGLE_CALENDAR_ID is set but GOOGLE_SERVICE_ACCOUNT_FILE is not")
                token = service_account_token_provider(settings.google_service_account_file)
                calendar = GoogleCalendar(settings.google_calendar_id, token, self.business.timezone)
            else:
                calendar = LocalCalendar(self.store)
        self.calendar = calendar

        if channels is None:
            channels = {}
            if settings.simulator_enabled:
                channels["simulator"] = SimulatorChannel()
            if settings.meta_enabled:
                channels["meta"] = MetaChannel(settings)
            if settings.twilio_enabled:
                channels["twilio"] = TwilioChannel(settings)
        self.channels = channels

        self.booking = BookingService(self.store, self.business, self.calendar, clock)
        self.bot = Bot(self.store, self.business, self.booking, clock,
                       timedelta(minutes=settings.session_timeout_minutes))
        self.reminders = ReminderService(self.store, self.bot, self.channels, clock)
        # One message at a time per customer, so a double-tap can't race through the state machine.
        self._phone_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)

    def handle(self, message: Incoming) -> list[str]:
        """Runs one incoming message through the bot and sends the replies on the same channel."""
        if message.message_id and not self.store.claim_message_id(f"{message.channel}:{message.message_id}",
                                                                   self.clock()):
            logger.info("Ignoring duplicate delivery of %s", message.message_id)
            return []
        channel = self.channels[message.channel]
        self.store.log_message(message.phone, message.channel, "in", message.text or "[non-text message]",
                               self.clock())

        with self._phone_locks[message.phone]:
            if message.text is None or not message.text.strip():
                replies = [UNREADABLE]
            else:
                try:
                    replies = self.bot.handle(message.phone, message.name, message.text, message.channel)
                except Exception:
                    logger.exception("Bot failed on a message from %s", message.phone)
                    self.store.clear_session(message.phone)
                    replies = [ERROR]

        for reply in replies:
            self.store.log_message(message.phone, message.channel, "out", reply, self.clock())
            try:
                channel.send_text(message.phone, reply)
            except Exception:
                logger.exception("Sending a reply to %s on %s failed", message.phone, message.channel)
        return replies

    def tick(self, now: datetime | None = None) -> dict:
        """One pass of the background loop: due reminders, then calendar retries."""
        result = self.reminders.run(now)
        synced, failed = self.booking.sync_pending()
        result.update(calendar_synced=synced, calendar_failed=failed)
        return result

def revoke_access(data=None):
    if data is None:
        return None
    return data

def measure_latency(data=None):
    if data is None:
        return None
    return data