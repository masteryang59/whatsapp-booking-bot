"""GoogleCalendar against an in-memory fake of the Calendar v3 REST API."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.calendars import CalendarError, CalendarEvent, GoogleCalendar

CAL_ID = "shop@group.calendar.google.com"
BASE = "/calendar/v3/calendars/shop%40group.calendar.google.com/events"
START = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)


class FakeGoogle:
    def __init__(self):
        self.events: dict[str, dict] = {}
        self.listed: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.page_size = 2
        self.fail_with: int | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["authorization"] == "Bearer test-token"
        if self.fail_with:
            return httpx.Response(self.fail_with, json={"error": {"message": "Backend Error"}})
        path = request.url.raw_path.decode().split("?")[0]
        assert path.startswith(BASE), path
        rest = path[len(BASE):].strip("/")
        if request.method == "GET":
            offset = int(request.url.params.get("pageToken", 0))
            page = self.listed[offset:offset + self.page_size]
            body = {"items": page}
            if offset + self.page_size < len(self.listed):
                body["nextPageToken"] = str(offset + self.page_size)
            return httpx.Response(200, json=body)
        if request.method == "POST":
            event = json.loads(request.content)
            if event["id"] in self.events:
                return httpx.Response(409, json={"error": {"message": "The requested identifier already exists."}})
            self.events[event["id"]] = event
            return httpx.Response(200, json=event)
        if request.method == "PUT":
            if rest not in self.events:
                return httpx.Response(404, json={"error": {"message": "Not Found"}})
            self.events[rest] = json.loads(request.content)
            return httpx.Response(200, json=self.events[rest])
        if request.method == "DELETE":
            if rest not in self.events:
                return httpx.Response(410, json={"error": {"message": "Resource has been deleted"}})
            del self.events[rest]
            return httpx.Response(204)
        return httpx.Response(405)


@pytest.fixture
def google():
    return FakeGoogle()


@pytest.fixture
def cal(google):
    client = httpx.Client(transport=httpx.MockTransport(google))
    return GoogleCalendar(CAL_ID, lambda: "test-token", "Europe/London", client=client)


def event(start=START, minutes=30):
    return CalendarEvent(id="bkabc123", summary="Haircut – Sam", description="Booked on WhatsApp",
                         start=start, end=start + timedelta(minutes=minutes), booking_id="7")


def test_upsert_creates_then_updates(cal, google):
    cal.upsert(event())
    stored = google.events["bkabc123"]
    assert stored["start"] == {"dateTime": "2026-11-03T10:00:00+00:00", "timeZone": "Europe/London"}
    assert stored["extendedProperties"]["private"]["bookingId"] == "7"
    assert google.requests[0].url.params["sendUpdates"] == "none"

    cal.upsert(event(start=START + timedelta(days=1)))
    assert [r.method for r in google.requests] == ["POST", "POST", "PUT"]
    assert google.events["bkabc123"]["start"]["dateTime"] == "2026-11-04T10:00:00+00:00"


def test_upsert_is_idempotent_on_retry(cal, google):
    cal.upsert(event())
    cal.upsert(event())
    assert len(google.events) == 1


def test_delete_treats_gone_as_done(cal, google):
    cal.upsert(event())
    cal.delete("bkabc123")
    cal.delete("bkabc123")  # 410 the second time
    assert google.events == {}


def test_errors_raise_calendar_error(cal, google):
    google.fail_with = 503
    with pytest.raises(CalendarError, match="503: Backend Error"):
        cal.upsert(event())
    with pytest.raises(CalendarError):
        cal.busy(START, START + timedelta(days=1))


def test_network_and_token_failures_raise_calendar_error(google):
    def broken_token():
        raise RuntimeError("invalid_grant")

    cal = GoogleCalendar(CAL_ID, broken_token, "Europe/London", client=httpx.Client(transport=httpx.MockTransport(google)))
    with pytest.raises(CalendarError, match="invalid_grant"):
        cal.busy(START, START + timedelta(days=1))

    def offline(request):
        raise httpx.ConnectError("no route to host")

    cal = GoogleCalendar(CAL_ID, lambda: "t", "Europe/London", client=httpx.Client(transport=httpx.MockTransport(offline)))
    with pytest.raises(CalendarError, match="no route"):
        cal.upsert(event())


def test_busy_reads_all_pages_and_filters_events(cal, google):
    google.listed = [
        {"id": "a", "start": {"dateTime": "2026-11-03T12:00:00Z"}, "end": {"dateTime": "2026-11-03T13:00:00Z"}},
        {"id": "b", "transparency": "transparent",  # marked "free"
         "start": {"dateTime": "2026-11-03T14:00:00Z"}, "end": {"dateTime": "2026-11-03T15:00:00Z"}},
        {"id": "c", "extendedProperties": {"private": {"bookingId": "3"}},  # one of ours
         "start": {"dateTime": "2026-11-03T09:00:00Z"}, "end": {"dateTime": "2026-11-03T09:30:00Z"}},
        {"id": "d", "attendees": [{"self": True, "responseStatus": "declined"}],
         "start": {"dateTime": "2026-11-03T16:00:00Z"}, "end": {"dateTime": "2026-11-03T17:00:00Z"}},
        {"id": "e", "start": {"date": "2026-11-04"}, "end": {"date": "2026-11-05"}},  # all-day, busy
        {"id": "f", "status": "cancelled"},
    ]
    busy = cal.busy(START, START + timedelta(days=3))
    assert [(b.start.isoformat(), b.end.isoformat()) for b in busy] == [
        ("2026-11-03T12:00:00+00:00", "2026-11-03T13:00:00+00:00"),
        ("2026-11-04T00:00:00+00:00", "2026-11-05T00:00:00+00:00"),
    ]
    gets = [r for r in google.requests if r.method == "GET"]
    assert len(gets) == 3  # 6 items, 2 per page
    assert gets[0].url.params["singleEvents"] == "true"
    assert gets[0].url.params["timeMin"] == "2026-11-03T10:00:00+00:00"


def test_service_account_token_provider_loads_a_key_file(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from google.oauth2 import service_account

    from app.calendars import GOOGLE_SCOPES, service_account_token_provider

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    key_file = tmp_path / "sa.json"
    key_file.write_text(json.dumps({
        "type": "service_account", "project_id": "demo", "private_key_id": "abc", "private_key": pem,
        "client_email": "bot@demo.iam.gserviceaccount.com", "client_id": "1",
        "token_uri": "https://oauth2.googleapis.com/token",
    }))

    refreshes = []

    def fake_refresh(self, request):
        refreshes.append(self.service_account_email)
        self.token = "ya29.fake"
        self.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)

    monkeypatch.setattr(service_account.Credentials, "refresh", fake_refresh)
    token = service_account_token_provider(key_file)
    assert token() == "ya29.fake"
    assert token() == "ya29.fake"
    assert refreshes == ["bot@demo.iam.gserviceaccount.com"]  # cached until it expires
    assert GOOGLE_SCOPES == ["https://www.googleapis.com/auth/calendar.events"]

def audit_action(data=None):
    if data is None:
        return None
    return data

def cleanup_resources(data=None):
    if data is None:
        return None
    return data

def audit_action(data=None):
    if data is None:
        return None
    return data