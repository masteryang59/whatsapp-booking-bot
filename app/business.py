"""Business rules: opening hours, services, and which slots are free.

Everything here is pure: callers pass in "now" and the busy intervals, so the
slot logic can be tested without a database, a calendar, or a real clock.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class Service:
    id: str
    name: str
    minutes: int
    price: str = ""
    aliases: tuple[str, ...] = ()

    @property
    def duration(self) -> timedelta:
        return timedelta(minutes=self.minutes)


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime

    def overlaps(self, other: "Interval") -> bool:
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True)
class Business:
    name: str
    timezone: str
    services: tuple[Service, ...]
    # weekday index (0 = Monday) -> list of (open, close) times
    hours: dict[int, tuple[tuple[time, time], ...]]
    address: str = ""
    date_order: str = "DMY"
    slot_step_minutes: int = 15
    buffer_minutes: int = 0
    min_notice_minutes: int = 120
    booking_horizon_days: int = 14
    max_upcoming_per_customer: int = 3
    reminders_minutes_before: tuple[int, ...] = (1440, 120)
    closed_dates: frozenset[date] = field(default_factory=frozenset)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def service(self, service_id: str) -> Service | None:
        return next((s for s in self.services if s.id == service_id), None)

    def local(self, dt: datetime) -> datetime:
        return dt.astimezone(self.tz)

    def today(self, now: datetime) -> date:
        return self.local(now).date()

    def last_bookable_day(self, now: datetime) -> date:
        return self.today(now) + timedelta(days=self.booking_horizon_days)

    def opening_intervals(self, day: date) -> list[Interval]:
        if day in self.closed_dates:
            return []
        return [
            Interval(datetime.combine(day, opens, self.tz), datetime.combine(day, closes, self.tz))
            for opens, closes in self.hours.get(day.weekday(), ())
        ]

    def is_open_on(self, day: date) -> bool:
        return bool(self.opening_intervals(day))

    def free_slots(self, service: Service, day: date, now: datetime, busy: list[Interval]) -> list[datetime]:
        """Start times (local, aware) on `day` where `service` fits.

        A slot fits when the whole appointment is inside one opening interval,
        starts at least `min_notice_minutes` from now, is within the booking
        horizon, and is at least `buffer_minutes` away from every busy interval.
        """
        if day < self.today(now) or day > self.last_bookable_day(now):
            return []
        earliest = now + timedelta(minutes=self.min_notice_minutes)
        step = timedelta(minutes=self.slot_step_minutes)
        buffer = timedelta(minutes=self.buffer_minutes)
        padded = [Interval(b.start - buffer, b.end + buffer) for b in busy]

        slots = []
        for opening in self.opening_intervals(day):
            start = opening.start
            while start + service.duration <= opening.end:
                candidate = Interval(start, start + service.duration)
                if start >= earliest and not any(candidate.overlaps(b) for b in padded):
                    slots.append(start)
                start += step
        return slots

    def match_service(self, text: str) -> Service | None:
        """The service named in free text, preferring the longest matching name or alias."""
        text = f" {_normalize(text)} "
        best, best_len = None, 0
        for service in self.services:
            for name in (service.name, service.id, *service.aliases):
                phrase = _normalize(name)
                if phrase and f" {phrase} " in text and len(phrase) > best_len:
                    best, best_len = service, len(phrase)
        return best


def _normalize(text: str) -> str:
    text = text.lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def load_business(path: Path) -> Business:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    hours = {}
    for index, key in enumerate(WEEKDAYS):
        intervals = tuple((_parse_hhmm(a), _parse_hhmm(b)) for a, b in raw.get("hours", {}).get(key, []))
        for opens, closes in intervals:
            if closes <= opens:
                raise ValueError(f"{key}: closing time {closes} is not after opening time {opens}")
        hours[index] = intervals

    services = tuple(
        Service(
            id=s["id"],
            name=s["name"],
            minutes=int(s["minutes"]),
            price=s.get("price", ""),
            aliases=tuple(s.get("aliases", [])),
        )
        for s in raw["services"]
    )
    if not services:
        raise ValueError("business config needs at least one service")
    date_order = raw.get("date_order", "DMY").upper()
    if date_order not in ("DMY", "MDY"):
        raise ValueError("date_order must be DMY or MDY")

    ZoneInfo(raw["timezone"])  # fail fast on a typo
    return Business(
        name=raw["name"],
        address=raw.get("address", ""),
        timezone=raw["timezone"],
        date_order=date_order,
        services=services,
        hours=hours,
        slot_step_minutes=int(raw.get("slot_step_minutes", 15)),
        buffer_minutes=int(raw.get("buffer_minutes", 0)),
        min_notice_minutes=int(raw.get("min_notice_minutes", 120)),
        booking_horizon_days=int(raw.get("booking_horizon_days", 14)),
        max_upcoming_per_customer=int(raw.get("max_upcoming_per_customer", 3)),
        reminders_minutes_before=tuple(sorted({int(m) for m in raw.get("reminders_minutes_before", [])}, reverse=True)),
        closed_dates=frozenset(date.fromisoformat(d) for d in raw.get("closed_dates", [])),
    )

def schedule_task(data=None):
    if data is None:
        return None
    return data

def acknowledge_receipt(data=None):
    if data is None:
        return None
    return data

def schedule_task(data=None):
    if data is None:
        return None
    return data

def acknowledge_receipt(data=None):
    if data is None:
        return None
    return data