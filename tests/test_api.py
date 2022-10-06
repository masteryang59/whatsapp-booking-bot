import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlencode

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.channels import MetaChannel, SimulatorChannel, TwilioChannel, twilio_signature
from app.core import BookingBot
from app.main import create_app
from tests.conftest import PHONE


class Outbox:
    """Captures what would be sent to Meta and Twilio."""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.out"}]})

    def meta_texts(self):
        return [json.loads(r.content)["text"]["body"] for r in self.requests if "graph.facebook.com" in str(r.url)]

    def twilio_texts(self):
        return [parse_qs(r.content.decode())["Body"][0] for r in self.requests if "api.twilio.com" in str(r.url)]


@pytest.fixture
def outbox():
    return Outbox()


@pytest.fixture
def api_settings(settings):
    return settings.model_copy(update=dict(
        meta_access_token=SecretStr("EAAG"),
        meta_phone_number_id="1234567890",
        meta_app_secret=SecretStr("app-secret"),
        meta_verify_token=SecretStr("verify-me"),
        twilio_account_sid="AC123",
        twilio_auth_token=SecretStr("twilio-token"),
        public_base_url="https://bot.example.com",
    ))


@pytest.fixture
def client(api_settings, clock, calendar, outbox):
    http = httpx.Client(transport=httpx.MockTransport(outbox))
    channels = {
        "simulator": SimulatorChannel(),
        "meta": MetaChannel(api_settings, client=http),
        "twilio": TwilioChannel(api_settings, client=http),
    }
    core = BookingBot(api_settings, clock=clock, calendar=calendar, channels=channels)
    with TestClient(create_app(api_settings, core)) as test_client:
        yield test_client


def meta_post(client, payload, secret=b"app-secret"):
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return client.post("/webhook/meta", content=body,
                       headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature})


def meta_message(text, message_id="wamid.1"):
    return {"entry": [{"changes": [{"value": {
        "contacts": [{"profile": {"name": "Sam Carter"}, "wa_id": "447700900123"}],
        "messages": [{"from": "447700900123", "id": message_id, "type": "text", "text": {"body": text}}],
    }}]}]}


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok", "channels": ["simulator", "meta", "twilio"],
                                                "calendar": "fake"}


def test_simulator_conversation(client):
    reply = client.post("/api/sim/messages", json={"phone": PHONE, "name": "Sam", "text": "haircut tomorrow at 10am"})
    assert "Please confirm" in reply.json()["replies"][0]
    client.post("/api/sim/messages", json={"phone": PHONE, "name": "Sam", "text": "yes"})

    log = client.get("/api/sim/messages", params={"phone": PHONE}).json()
    assert [m["direction"] for m in log] == ["in", "out", "in", "out"]
    assert log[-1]["body"].startswith("✅ Booked!")
    assert client.get("/api/sim/messages", params={"phone": PHONE, "after": log[-1]["id"]}).json() == []

    state = client.get("/api/sim/state").json()
    [appointment] = state["appointments"]
    assert appointment["service"] == "Haircut" and appointment["calendar_state"] == "synced"
    assert state["business"]["timezone"] == "Europe/London"


def test_simulator_rejects_bad_phone(client):
    response = client.post("/api/sim/messages", json={"phone": "07700 900123", "text": "hi"})
    assert response.status_code == 422


def test_simulator_reminder_time_travel(client):
    client.post("/api/sim/messages", json={"phone": PHONE, "text": "haircut thursday at 10am"})
    client.post("/api/sim/messages", json={"phone": PHONE, "text": "yes"})
    # A naive time is read in the business's time zone (London).
    result = client.post("/api/sim/reminders", json={"at": "2026-11-04T10:30:00"}).json()
    assert result["sent"] == 1
    assert client.get("/api/sim/messages", params={"phone": PHONE}).json()[-1]["body"].startswith("⏰ Reminder")


def test_simulator_blocks_need_the_local_calendar(client):
    response = client.post("/api/sim/blocks", json={"start": "2026-11-03T10:00", "end": "2026-11-03T11:00"})
    assert response.status_code == 400


def test_simulator_block_with_local_calendar(settings, clock):
    core = BookingBot(settings, clock=clock, channels={"simulator": SimulatorChannel()})
    with TestClient(create_app(settings, core)) as client:
        response = client.post("/api/sim/blocks",
                               json={"start": "2026-11-03T09:00", "end": "2026-11-03T12:30", "summary": "Dentist"})
        assert response.status_code == 200
        reply = client.post("/api/sim/messages", json={"phone": PHONE, "text": "haircut tomorrow at 10am"}).json()
        assert "isn't available" in reply["replies"][0]
        events = client.get("/api/sim/state").json()["events"]
        assert [e["summary"] for e in events] == ["Dentist"]


def test_meta_verification_handshake(client):
    ok = client.get("/webhook/meta", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                             "hub.challenge": "1158201444"})
    assert ok.status_code == 200 and ok.text == "1158201444"
    bad = client.get("/webhook/meta", params={"hub.mode": "subscribe", "hub.verify_token": "nope",
                                              "hub.challenge": "1"})
    assert bad.status_code == 403


def test_meta_webhook_round_trip(client, outbox):
    assert meta_post(client, meta_message("hi")).status_code == 200
    [reply] = outbox.meta_texts()
    assert reply.startswith("Hi Sam! 👋")
    assert json.loads(outbox.requests[0].content)["to"] == "447700900123"


def test_meta_webhook_rejects_bad_signature(client, outbox):
    response = meta_post(client, meta_message("hi"), secret=b"wrong")
    assert response.status_code == 401
    assert outbox.requests == []


def test_meta_retries_are_processed_once(client, outbox):
    meta_post(client, meta_message("hi", "wamid.same"))
    meta_post(client, meta_message("hi", "wamid.same"))
    assert len(outbox.meta_texts()) == 1


def test_meta_non_text_message(client, outbox):
    payload = meta_message("x")
    payload["entry"][0]["changes"][0]["value"]["messages"][0] = {
        "from": "447700900123", "id": "wamid.voice", "type": "audio", "audio": {"id": "m1"}}
    meta_post(client, payload)
    assert "only read text" in outbox.meta_texts()[0]


def test_twilio_webhook_round_trip(client, outbox):
    params = {"MessageSid": "SM1", "From": "whatsapp:+447700900123", "To": "whatsapp:+14155238886",
              "Body": "my appointments", "ProfileName": "Sam", "NumMedia": "0"}
    # Signed with the public URL, as Twilio sees it, not the internal test URL.
    signature = twilio_signature("twilio-token", "https://bot.example.com/webhook/twilio", params)
    response = client.post("/webhook/twilio", content=urlencode(params),
                           headers={"Content-Type": "application/x-www-form-urlencoded",
                                    "X-Twilio-Signature": signature})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert "<Response></Response>" in response.text
    assert "no upcoming appointments" in outbox.twilio_texts()[0]


def test_twilio_webhook_rejects_bad_signature(client, outbox):
    response = client.post("/webhook/twilio", content="Body=hi&From=whatsapp%3A%2B447700900123",
                           headers={"Content-Type": "application/x-www-form-urlencoded",
                                    "X-Twilio-Signature": "bogus"})
    assert response.status_code == 403
    assert outbox.requests == []


def test_reminder_goes_out_on_the_booking_channel(client, outbox, clock):
    meta_post(client, meta_message("haircut thursday at 10am", "wamid.1"))
    meta_post(client, meta_message("yes", "wamid.2"))
    clock.advance(days=2, hours=1)  # Wednesday 10:00, one day before
    assert client.post("/api/sim/reminders", json={}).json()["sent"] == 1
    assert outbox.meta_texts()[-1].startswith("⏰ Reminder: your *Haircut*")


def test_unconfigured_channels_404(settings, clock):
    core = BookingBot(settings, clock=clock)
    with TestClient(create_app(settings, core)) as client:
        assert client.post("/webhook/meta", content=b"{}").status_code == 404
        assert client.post("/webhook/twilio", content=b"").status_code == 404

def emit_event(data=None):
    if data is None:
        return None
    return data

def save_state(data=None):
    if data is None:
        return None
    return data

def emit_event(data=None):
    if data is None:
        return None
    return data