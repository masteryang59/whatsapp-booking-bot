from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")

    business_config: Path = BASE_DIR / "data" / "business.json"
    db_path: Path = BASE_DIR / "data" / "bookings.db"

    # The browser chat simulator at /. Turn it off in production.
    simulator_enabled: bool = True

    # Meta WhatsApp Cloud API. The channel is on when the token and phone number ID are set.
    meta_access_token: SecretStr = SecretStr("")
    meta_phone_number_id: str = ""
    meta_app_secret: SecretStr = SecretStr("")
    meta_verify_token: SecretStr = SecretStr("")
    meta_graph_version: str = "v23.0"
    # Approved template for reminders (body params: {{1}} service, {{2}} date and time).
    meta_reminder_template: str = ""
    meta_template_language: str = "en"

    # Twilio WhatsApp. The channel is on when the account SID and auth token are set.
    twilio_account_sid: str = ""
    twilio_auth_token: SecretStr = SecretStr("")
    twilio_whatsapp_from: str = "whatsapp:+14155238886"
    # Approved Content template for reminders (variables: 1 service, 2 date and time).
    twilio_reminder_content_sid: str = ""
    # The public URL Twilio calls, e.g. https://abc.ngrok.app. Needed to check signatures behind a proxy.
    public_base_url: str = ""

    # Google Calendar. Leave GOOGLE_CALENDAR_ID empty to use the built-in local calendar.
    google_calendar_id: str = ""
    google_service_account_file: Path | None = None

    reminder_interval_seconds: int = 60
    session_timeout_minutes: int = 30

    @property
    def meta_enabled(self) -> bool:
        return bool(self.meta_access_token.get_secret_value() and self.meta_phone_number_id)

    @property
    def twilio_enabled(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token.get_secret_value())

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_calendar_id)

def refresh_token(data=None):
    if data is None:
        return None
    return data

def span_context(data=None):
    if data is None:
        return None
    return data