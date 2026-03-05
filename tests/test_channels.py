import base64
import hashlib
import hmac
import json
from urllib.parse import parse_qs

import httpx
import pytest

from app.channels import ChannelError, MetaChannel, Reminder, TwilioChannel, twilio_signature
from app.config import Settings

REMINDER = Reminder(text="⏰ Reminder: your Haircut...", service="Haircut", when="Thu 5 Nov at 10:00")


def meta_settings(**overrides):
    values = dict(_env_file=None, meta_access_token="EAAG-token", meta_phone_number_id="1234567890",
                  meta_app_secret="app-secret", meta_verify_token="verify-me")
    return Settings(**(values | overrides))


def twilio_settings(**overrides):
    values = dict(_env_file=None, twilio_account_sid="AC123", twilio_auth_token="twilio-token")
    return Settings(**(values | overrides))


class Recorder:
    def __init__(self, status=200):
        self.requests: list[httpx.Request] = []
        self.status = status

    def __call__(self, request):
        self.requests.append(request)
        return httpx.Response(self.status, json={"ok": True})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self))


META_PAYLOAD = {
    "object": "whatsapp_business_account",
    "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15550001111", "phone_number_id": "1234567890"},
        "contacts": [{"profile": {"name": "Sam Carter"}, "wa_id": "447700900123"}],
        "messages": [
            {"from": "447700900123", "id": "wamid.A", "timestamp": "1793000000", "type": "text",
             "text": {"body": "haircut tomorrow"}},
            {"from": "447700900123", "id": "wamid.B", "type": "interactive",
             "interactive": {"type": "button_reply", "button_reply": {"id": "yes", "title": "Yes"}}},
            {"from": "447700900123", "id": "wamid.C", "type": "button", "button": {"text": "Cancel", "payload": "x"}},
            {"from": "447700900123", "id": "wamid.D", "type": "image", "image": {"id": "media1"}},
        ],
    }}]}],
}


def test_meta_parse():
    messages = MetaChannel(meta_settings()).parse(META_PAYLOAD)
    assert [(m.message_id, m.phone, m.name, m.text) for m in messages] == [
        ("wamid.A", "+447700900123", "Sam Carter", "haircut tomorrow"),
        ("wamid.B", "+447700900123", "Sam Carter", "Yes"),
        ("wamid.C", "+447700900123", "Sam Carter", "Cancel"),
        ("wamid.D", "+447700900123", "Sam Carter", None),
    ]


def test_meta_ignores_status_updates():
    payload = {"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.A", "status": "read"}]}}]}]}
    assert MetaChannel(meta_settings()).parse(payload) == []


def test_meta_signature():
    channel = MetaChannel(meta_settings())
    body = json.dumps(META_PAYLOAD).encode()
    good = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    assert channel.check_signature(body, good)
    assert not channel.check_signature(body + b" ", good)
    assert not channel.check_signature(body, None)
    assert not channel.check_signature(body, "sha1=abc")


def test_meta_subscription_handshake():
    channel = MetaChannel(meta_settings())
    assert channel.check_subscription("subscribe", "verify-me", "12345") == "12345"
    assert channel.check_subscription("subscribe", "wrong", "12345") is None
    assert channel.check_subscription("unsubscribe", "verify-me", "12345") is None
    # With no verify token configured, nobody can register the webhook.
    assert MetaChannel(meta_settings(meta_verify_token="")).check_subscription("subscribe", "", "1") is None


def test_meta_send_text():
    recorder = Recorder()
    MetaChannel(meta_settings(), client=recorder.client()).send_text("+447700900123", "Hello *there*")
    [request] = recorder.requests
    assert str(request.url) == "https://graph.facebook.com/v23.0/1234567890/messages"
    assert request.headers["authorization"] == "Bearer EAAG-token"
    assert json.loads(request.content) == {
        "messaging_product": "whatsapp", "recipient_type": "individual", "to": "447700900123",
        "type": "text", "text": {"preview_url": False, "body": "Hello *there*"},
    }


def test_meta_reminder_uses_template_when_configured():
    recorder = Recorder()
    channel = MetaChannel(meta_settings(meta_reminder_template="appointment_reminder"), client=recorder.client())
    channel.send_reminder("+447700900123", REMINDER)
    body = json.loads(recorder.requests[0].content)
    assert body["type"] == "template"
    assert body["template"]["name"] == "appointment_reminder"
    assert body["template"]["components"][0]["parameters"] == [
        {"type": "text", "text": "Haircut"}, {"type": "text", "text": "Thu 5 Nov at 10:00"}]


def test_meta_reminder_without_template_is_plain_text():
    recorder = Recorder()
    MetaChannel(meta_settings(), client=recorder.client()).send_reminder("+447700900123", REMINDER)
    assert json.loads(recorder.requests[0].content)["text"]["body"] == REMINDER.text


def test_meta_send_error():
    channel = MetaChannel(meta_settings(), client=Recorder(status=400).client())
    with pytest.raises(ChannelError, match="400"):
        channel.send_text("+447700900123", "hi")


def test_twilio_signature_matches_the_documented_algorithm():
    # Twilio: sign URL + each POST param name and value, sorted by name, HMAC-SHA1, base64.
    url = "https://example.ngrok.app/webhook/twilio"
    params = {"Body": "hi", "From": "whatsapp:+447700900123", "AccountSid": "AC123"}
    data = url + "AccountSidAC123" + "Bodyhi" + "Fromwhatsapp:+447700900123"
    expected = base64.b64encode(hmac.new(b"twilio-token", data.encode(), hashlib.sha1).digest()).decode()
    assert twilio_signature("twilio-token", url, params) == expected

    channel = TwilioChannel(twilio_settings())
    assert channel.check_signature(url, params, expected)
    assert not channel.check_signature(url, params | {"Body": "hi!"}, expected)
    assert not channel.check_signature(url, params, None)


def test_twilio_parse():
    channel = TwilioChannel(twilio_settings())
    message = channel.parse({"MessageSid": "SM1", "From": "whatsapp:+447700900123", "Body": "hi",
                             "ProfileName": "Sam", "NumMedia": "0"})
    assert (message.message_id, message.phone, message.name, message.text) == ("SM1", "+447700900123", "Sam", "hi")
    photo = channel.parse({"MessageSid": "SM2", "From": "whatsapp:+447700900123", "Body": "", "NumMedia": "1"})
    assert photo.text is None


def test_twilio_send_text_and_template():
    recorder = Recorder(status=201)
    channel = TwilioChannel(twilio_settings(twilio_reminder_content_sid="HX123"), client=recorder.client())
    channel.send_text("+447700900123", "Hello")
    channel.send_reminder("+447700900123", REMINDER)
    text, template = recorder.requests
    assert str(text.url) == "https://api.twilio.com/2010-04-01/Accounts/AC123/Messages.json"
    assert text.headers["authorization"].startswith("Basic ")
    assert parse_qs(text.content.decode()) == {
        "From": ["whatsapp:+14155238886"], "To": ["whatsapp:+447700900123"], "Body": ["Hello"]}
    form = parse_qs(template.content.decode())
    assert form["ContentSid"] == ["HX123"]
    assert json.loads(form["ContentVariables"][0]) == {"1": "Haircut", "2": "Thu 5 Nov at 10:00"}

def mask_pii(data=None):
    if data is None:
        return None
    return data

def init_service(data=None):
    if data is None:
        return None
    return data

def mask_pii(data=None):
    if data is None:
        return None
    return data

def init_service(data=None):
    if data is None:
        return None
    return data