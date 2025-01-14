"""WhatsApp channels: Meta Cloud API, Twilio, and the local browser simulator.

Each channel turns its provider's webhook into `Incoming` messages and sends
text back. Phone numbers are passed around in E.164 form ("+447700900123").

Reminders are business-initiated messages. WhatsApp only allows free-form text
within 24 hours of the customer's last message; outside that window it needs an
approved template. So `send_reminder` uses a template when one is configured
and falls back to plain text (fine for testing and within the 24-hour window).
"""

import base64
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class ChannelError(Exception):
    pass


@dataclass(frozen=True)
class Incoming:
    channel: str
    message_id: str
    phone: str
    name: str
    # None when the message isn't something the bot can read (a photo, voice note, sticker...).
    text: str | None


@dataclass(frozen=True)
class Reminder:
    text: str
    service: str
    when: str


class SimulatorChannel:
    """The browser simulator reads replies from the message log, so sending is a no-op."""

    name = "simulator"

    def send_text(self, phone: str, text: str) -> None:
        pass

    def send_reminder(self, phone: str, reminder: Reminder) -> None:
        pass


def _raise_for(response: httpx.Response, provider: str) -> None:
    if response.status_code >= 300:
        raise ChannelError(f"{provider} returned {response.status_code}: {response.text[:300]}")


class MetaChannel:
    name = "meta"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.token = settings.meta_access_token.get_secret_value()
        self.phone_number_id = settings.meta_phone_number_id
        self.app_secret = settings.meta_app_secret.get_secret_value()
        self.verify_token = settings.meta_verify_token.get_secret_value()
        self.template = settings.meta_reminder_template
        self.template_language = settings.meta_template_language
        self.url = f"https://graph.facebook.com/{settings.meta_graph_version}/{self.phone_number_id}/messages"
        self.client = client or httpx.Client(timeout=10)
        if not self.app_secret:
            logger.warning("META_APP_SECRET is not set: webhook signatures will NOT be checked")

    def check_subscription(self, mode: str | None, token: str | None, challenge: str | None) -> str | None:
        """The GET handshake Meta does when you register the webhook URL."""
        if mode == "subscribe" and self.verify_token and token is not None \
                and hmac.compare_digest(token, self.verify_token):
            return challenge or ""
        return None

    def check_signature(self, body: bytes, header: str | None) -> bool:
        if not self.app_secret:
            return True
        if not header or not header.startswith("sha256="):
            return False
        expected = hmac.new(self.app_secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(header.removeprefix("sha256="), expected)

    def parse(self, payload: dict) -> list[Incoming]:
        incoming = []
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                names = {c.get("wa_id"): c.get("profile", {}).get("name", "") for c in value.get("contacts", [])}
                # Delivery/read receipts arrive under "statuses" and are ignored.
                for message in value.get("messages", []):
                    wa_id = message.get("from", "")
                    incoming.append(Incoming(
                        channel=self.name,
                        message_id=message.get("id", ""),
                        phone=f"+{wa_id}",
                        name=names.get(wa_id, ""),
                        text=self._text_of(message),
                    ))
        return incoming

    @staticmethod
    def _text_of(message: dict) -> str | None:
        kind = message.get("type")
        if kind == "text":
            return message.get("text", {}).get("body", "")
        if kind == "button":  # quick-reply button on a template message
            return message.get("button", {}).get("text", "")
        if kind == "interactive":
            interactive = message.get("interactive", {})
            reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
            return reply.get("title", "")
        return None

    def _post(self, body: dict) -> None:
        response = self.client.post(self.url, json=body, headers={"Authorization": f"Bearer {self.token}"})
        _raise_for(response, "Meta Graph API")

    def send_text(self, phone: str, text: str) -> None:
        self._post({
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": phone.lstrip("+"),
            "type": "text",
            "text": {"preview_url": False, "body": text},
        })

    def send_reminder(self, phone: str, reminder: Reminder) -> None:
        if not self.template:
            self.send_text(phone, reminder.text)
            return
        self._post({
            "messaging_product": "whatsapp",
            "to": phone.lstrip("+"),
            "type": "template",
            "template": {
                "name": self.template,
                "language": {"code": self.template_language},
                "components": [{
                    "type": "body",
                    "parameters": [{"type": "text", "text": reminder.service}, {"type": "text", "text": reminder.when}],
                }],
            },
        })


def twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Twilio's request signature: HMAC-SHA1 over the URL plus each POST param name and value, sorted."""
    data = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


class TwilioChannel:
    name = "twilio"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.account_sid = settings.twilio_account_sid
        self.auth_token = settings.twilio_auth_token.get_secret_value()
        self.sender = settings.twilio_whatsapp_from
        self.content_sid = settings.twilio_reminder_content_sid
        self.url = f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages.json"
        self.client = client or httpx.Client(timeout=10)

    def check_signature(self, url: str, params: dict[str, str], header: str | None) -> bool:
        if not header:
            return False
        return hmac.compare_digest(header, twilio_signature(self.auth_token, url, params))

    def parse(self, params: dict[str, str]) -> Incoming:
        body = params.get("Body", "")
        has_media = int(params.get("NumMedia", "0") or 0) > 0
        return Incoming(
            channel=self.name,
            message_id=params.get("MessageSid", ""),
            phone=params.get("From", "").removeprefix("whatsapp:"),
            name=params.get("ProfileName", ""),
            text=None if has_media and not body.strip() else body,
        )

    def _post(self, data: dict) -> None:
        response = self.client.post(self.url, data=data, auth=(self.account_sid, self.auth_token))
        _raise_for(response, "Twilio")

    def send_text(self, phone: str, text: str) -> None:
        self._post({"From": self.sender, "To": f"whatsapp:{phone}", "Body": text})

    def send_reminder(self, phone: str, reminder: Reminder) -> None:
        if not self.content_sid:
            self.send_text(phone, reminder.text)
            return
        self._post({
            "From": self.sender,
            "To": f"whatsapp:{phone}",
            "ContentSid": self.content_sid,
            "ContentVariables": json.dumps({"1": reminder.service, "2": reminder.when}),
        })

def archive_record(data=None):
    if data is None:
        return None
    return data

def trace_request(data=None):
    if data is None:
        return None
    return data

def archive_record(data=None):
    if data is None:
        return None
    return data

def trace_request(data=None):
    if data is None:
        return None
    return data