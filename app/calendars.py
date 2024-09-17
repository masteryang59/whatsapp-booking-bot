"""Calendar backends: Google Calendar (REST v3) and a local stand-in.

Both do the same three things:
- `busy(start, end)`: time blocked by events the bot didn't create (the owner's
  own events, holidays, a dentist appointment). Bot-made events are skipped,
  because the database already knows about those.
- `upsert(event)`: create or update a booking's event under a fixed ID.
- `delete(event_id)`: remove it; an event that's already gone counts as done.
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from app.business import Interval
from app.db import Store, from_db, to_db

logger = logging.getLogger(__name__)

GOOGLE_API = "https://www.googleapis.com/calendar/v3"
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
SOURCE_TAG = "whatsapp-booking-bot"


class CalendarError(Exception):
    pass


@dataclass(frozen=True)
class CalendarEvent:
    id: str
    summary: str
    description: str
    start: datetime
    end: datetime
    booking_id: str


class LocalCalendar:
    """Keeps events in SQLite. Used when Google Calendar isn't configured, and by the simulator.

    `add_block` plays the part of the owner adding a personal event in their calendar.
    """

    name = "local"

    def __init__(self, store: Store):
        self.store = store

    def busy(self, start: datetime, end: datetime) -> list[Interval]:
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT start_utc, end_utc FROM local_events WHERE status = 'confirmed' AND booking_id IS NULL "
                "AND start_utc < ? AND end_utc > ?",
                (to_db(end), to_db(start)),
            ).fetchall()
        return [Interval(from_db(r["start_utc"]), from_db(r["end_utc"])) for r in rows]

    def upsert(self, event: CalendarEvent) -> None:
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO local_events (id, summary, description, start_utc, end_utc, booking_id, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'confirmed') ON CONFLICT (id) DO UPDATE SET summary = excluded.summary, "
                "description = excluded.description, start_utc = excluded.start_utc, end_utc = excluded.end_utc, "
                "status = 'confirmed'",
                (event.id, event.summary, event.description, to_db(event.start), to_db(event.end), event.booking_id),
            )

    def delete(self, event_id: str) -> None:
        with self.store.connect() as conn:
            conn.execute("UPDATE local_events SET status = 'cancelled' WHERE id = ?", (event_id,))

    def add_block(self, summary: str, start: datetime, end: datetime) -> str:
        event_id = f"block{int(datetime.now(UTC).timestamp() * 1000)}"
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO local_events (id, summary, start_utc, end_utc) VALUES (?, ?, ?, ?)",
                (event_id, summary, to_db(start), to_db(end)),
            )
        return event_id

    def events(self, start: datetime, end: datetime) -> list[dict]:
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM local_events WHERE status = 'confirmed' AND start_utc < ? AND end_utc > ? "
                "ORDER BY start_utc",
                (to_db(end), to_db(start)),
            ).fetchall()
        return [
            {"id": r["id"], "summary": r["summary"], "start": r["start_utc"], "end": r["end_utc"],
             "booking": r["booking_id"] is not None}
            for r in rows
        ]


def service_account_token_provider(key_file: Path) -> Callable[[], str]:
    """Returns a function that hands out a fresh OAuth access token for a service account."""
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_file(str(key_file), scopes=GOOGLE_SCOPES)
    lock = threading.Lock()

    def token() -> str:
        with lock:
            if not credentials.valid:
                credentials.refresh(Request())
            return credentials.token

    return token


class GoogleCalendar:
    name = "google"

    def __init__(self, calendar_id: str, token: Callable[[], str], timezone: str,
                 client: httpx.Client | None = None):
        self.calendar_id = calendar_id
        self.token = token
        self.tz = ZoneInfo(timezone)
        self.client = client or httpx.Client(timeout=10)
        self.events_url = f"{GOOGLE_API}/calendars/{quote(calendar_id, safe='')}/events"

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            headers = {"Authorization": f"Bearer {self.token()}"}
            return self.client.request(method, url, headers=headers, **kwargs)
        except CalendarError:
            raise
        except Exception as exc:  # network errors and token refresh failures
            raise CalendarError(f"{method} {url}: {exc}") from exc

    @staticmethod
    def _fail(response: httpx.Response) -> CalendarError:
        try:
            message = response.json()["error"]["message"]
        except Exception:
            message = response.text[:200]
        return CalendarError(f"Google Calendar returned {response.status_code}: {message}")

    def busy(self, start: datetime, end: datetime) -> list[Interval]:
        intervals = []
        params = {
            "timeMin": start.astimezone(UTC).isoformat(),
            "timeMax": end.astimezone(UTC).isoformat(),
            "singleEvents": "true",
            "maxResults": "2500",
        }
        while True:
            response = self._request("GET", self.events_url, params=params)
            if response.status_code != 200:
                raise self._fail(response)
            body = response.json()
            for item in body.get("items", []):
                interval = self._busy_interval(item)
                if interval is not None:
                    intervals.append(interval)
            page = body.get("nextPageToken")
            if not page:
                return intervals
            params["pageToken"] = page

    def _busy_interval(self, item: dict) -> Interval | None:
        if item.get("status") == "cancelled" or item.get("transparency") == "transparent":
            return None
        if item.get("extendedProperties", {}).get("private", {}).get("bookingId"):
            return None
        for attendee in item.get("attendees", []):
            if attendee.get("self") and attendee.get("responseStatus") == "declined":
                return None
        start, end = item.get("start", {}), item.get("end", {})
        if "dateTime" in start and "dateTime" in end:
            return Interval(datetime.fromisoformat(start["dateTime"]), datetime.fromisoformat(end["dateTime"]))
        if "date" in start and "date" in end:
            # An all-day event marked busy blocks whole days in the business's time zone.
            first, after_last = date.fromisoformat(start["date"]), date.fromisoformat(end["date"])
            return Interval(datetime.combine(first, time(0), self.tz), datetime.combine(after_last, time(0), self.tz))
        return None

    def _body(self, event: CalendarEvent) -> dict:
        tz_name = str(self.tz)
        return {
            "id": event.id,
            "summary": event.summary,
            "description": event.description,
            "start": {"dateTime": event.start.astimezone(self.tz).isoformat(), "timeZone": tz_name},
            "end": {"dateTime": event.end.astimezone(self.tz).isoformat(), "timeZone": tz_name},
            "status": "confirmed",
            "extendedProperties": {"private": {"bookingId": event.booking_id, "source": SOURCE_TAG}},
        }

    def upsert(self, event: CalendarEvent) -> None:
        body = self._body(event)
        response = self._request("POST", self.events_url, params={"sendUpdates": "none"}, json=body)
        if response.status_code == 409:
            # The ID already exists: an earlier attempt got through, or this is a reschedule.
            url = f"{self.events_url}/{event.id}"
            response = self._request("PUT", url, params={"sendUpdates": "none"}, json=body)
        if response.status_code not in (200, 201):
            raise self._fail(response)

    def delete(self, event_id: str) -> None:
        response = self._request("DELETE", f"{self.events_url}/{event_id}", params={"sendUpdates": "none"})
        if response.status_code not in (200, 204, 404, 410):
            raise self._fail(response)

def cancel_job(data=None):
    if data is None:
        return None
    return data

def ping_service(data=None):
    if data is None:
        return None
    return data

def cancel_job(data=None):
    if data is None:
        return None
    return data

def ping_service(data=None):
    if data is None:
        return None
    return data