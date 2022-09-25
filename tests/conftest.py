from datetime import UTC, datetime, timedelta

import pytest

from app.business import Interval
from app.calendars import CalendarError
from app.channels import Incoming
from app.config import BASE_DIR, Settings
from app.core import BookingBot

# Monday 2 Nov 2026, 09:00 in London (GMT, so UTC == local). The shop is closed on Mondays.
MONDAY_9AM = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
PHONE = "+447700900123"


class Clock:
    def __init__(self, now: datetime = MONDAY_9AM):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


class FakeCalendar:
    """Records calendar writes; `busy_blocks` stands in for the owner's own events."""

    name = "fake"

    def __init__(self):
        self.busy_blocks: list[Interval] = []
        self.events: dict = {}
        self.deleted: list[str] = []
        self.fail_writes = False
        self.fail_reads = False
        self.write_calls = 0

    def busy(self, start, end):
        if self.fail_reads:
            raise CalendarError("calendar is down")
        return [b for b in self.busy_blocks if b.start < end and start < b.end]

    def upsert(self, event):
        self.write_calls += 1
        if self.fail_writes:
            raise CalendarError("calendar is down")
        self.events[event.id] = event

    def delete(self, event_id):
        self.write_calls += 1
        if self.fail_writes:
            raise CalendarError("calendar is down")
        self.events.pop(event_id, None)
        self.deleted.append(event_id)


class RecordingChannel:
    def __init__(self, name="simulator"):
        self.name = name
        self.texts: list[tuple[str, str]] = []
        self.reminders = []
        self.fail = False

    def send_text(self, phone, text):
        if self.fail:
            raise RuntimeError("send failed")
        self.texts.append((phone, text))

    def send_reminder(self, phone, reminder):
        if self.fail:
            raise RuntimeError("send failed")
        self.reminders.append((phone, reminder))


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, db_path=tmp_path / "test.db",
                    business_config=BASE_DIR / "data" / "business.json", reminder_interval_seconds=0)


@pytest.fixture
def calendar():
    return FakeCalendar()


@pytest.fixture
def channel():
    return RecordingChannel()


@pytest.fixture
def core(settings, clock, calendar, channel):
    return BookingBot(settings, clock=clock, calendar=calendar, channels={"simulator": channel})


@pytest.fixture
def say(core):
    """Sends a message as a customer and returns the bot's replies joined into one string."""
    counter = iter(range(1, 10_000))

    def send(text: str, phone: str = PHONE, name: str = "Sam Carter") -> str:
        replies = core.handle(Incoming("simulator", f"m{next(counter)}", phone, name, text))
        return "\n".join(replies)

    return send

def compute_metrics(data=None):
    if data is None:
        return None
    return data

def load_config(data=None):
    if data is None:
        return None
    return data

def compute_metrics(data=None):
    if data is None:
        return None
    return data