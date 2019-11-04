import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Annotated
from urllib.parse import parse_qsl

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, StringConstraints

from app.calendars import LocalCalendar
from app.channels import Incoming
from app.config import BASE_DIR, Settings
from app.core import BookingBot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

STATIC_DIR = BASE_DIR / "app" / "static"
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'

Phone = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\+[1-9]\d{6,14}$")]


class SimMessage(BaseModel):
    phone: Phone
    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)] = ""
    text: Annotated[str, StringConstraints(max_length=1000)]


# Simulator times without a UTC offset are read in the business's time zone.
class SimBlock(BaseModel):
    start: datetime
    end: datetime
    summary: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] = "Busy"


class SimReminders(BaseModel):
    # Run the reminder check as if it were this time (simulator "time travel"). Empty means now.
    at: datetime | None = None


def create_app(settings: Settings | None = None, core: BookingBot | None = None) -> FastAPI:
    settings = settings or Settings()
    core = core or BookingBot(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if settings.reminder_interval_seconds > 0:
            task = asyncio.create_task(background_loop())
        logger.info("Channels: %s; calendar: %s", ", ".join(core.channels) or "none", core.calendar.name)
        yield
        if task:
            task.cancel()

    async def background_loop():
        while True:
            try:
                result = await run_in_threadpool(core.tick)
                if any(result.values()):
                    logger.info("Background pass: %s", result)
            except Exception:
                logger.exception("Background pass failed")
            await asyncio.sleep(settings.reminder_interval_seconds)

    app = FastAPI(title="WhatsApp Booking Bot", lifespan=lifespan)
    app.state.core = core

    @app.get("/api/health")
    def health():
        return {"status": "ok", "channels": list(core.channels), "calendar": core.calendar.name}

    # Meta WhatsApp Cloud API

    def meta():
        channel = core.channels.get("meta")
        if channel is None:
            raise HTTPException(404, "The Meta channel is not configured")
        return channel

    @app.get("/webhook/meta")
    def meta_verify(mode: str | None = Query(None, alias="hub.mode"),
                    token: str | None = Query(None, alias="hub.verify_token"),
                    challenge: str | None = Query(None, alias="hub.challenge")):
        answer = meta().check_subscription(mode, token, challenge)
        if answer is None:
            raise HTTPException(403, "Verification failed")
        return PlainTextResponse(answer)

    @app.post("/webhook/meta")
    async def meta_webhook(request: Request, background: BackgroundTasks):
        channel = meta()
        body = await request.body()
        if not channel.check_signature(body, request.headers.get("x-hub-signature-256")):
            raise HTTPException(401, "Bad signature")
        try:
            payload = json.loads(body)
        except ValueError:
            raise HTTPException(400, "Body is not JSON")
        # Answer Meta straight away; it retries slow webhooks. The bot runs after the response.
        for message in channel.parse(payload):
            background.add_task(core.handle, message)
        return {"status": "ok"}

    # Twilio WhatsApp

    @app.post("/webhook/twilio")
    async def twilio_webhook(request: Request, background: BackgroundTasks):
        channel = core.channels.get("twilio")
        if channel is None:
            raise HTTPException(404, "The Twilio channel is not configured")
        params = dict(parse_qsl((await request.body()).decode(), keep_blank_values=True))
        url = str(request.url)
        if settings.public_base_url:
            url = settings.public_base_url.rstrip("/") + request.url.path
            if request.url.query:
                url += "?" + request.url.query
        if not channel.check_signature(url, params, request.headers.get("x-twilio-signature")):
            raise HTTPException(403, "Bad signature")
        background.add_task(core.handle, channel.parse(params))
        # Replies go out through the REST API, so the TwiML response is empty.
        return Response(EMPTY_TWIML, media_type="application/xml")

    # Browser simulator

    def business_time(value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=core.business.tz)

    if "simulator" in core.channels:
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def simulator_page():
            return FileResponse(STATIC_DIR / "index.html")

        @app.post("/api/sim/messages")
        def sim_send(msg: SimMessage):
            incoming = Incoming("simulator", uuid.uuid4().hex, msg.phone, msg.name, msg.text)
            return {"replies": core.handle(incoming)}

        @app.get("/api/sim/messages")
        def sim_messages(phone: str, after: int = 0):
            return [
                {"id": m.id, "direction": m.direction, "body": m.body, "at": m.created.isoformat()}
                for m in core.store.messages(phone=phone, after_id=after)
            ]

        @app.get("/api/sim/state")
        def sim_state():
            now = core.clock()
            business = core.business
            appointments = core.store.list_appointments(now - timedelta(days=1))
            events = []
            if isinstance(core.calendar, LocalCalendar):
                events = core.calendar.events(now - timedelta(days=1), now + timedelta(days=60))
            return {
                "business": {
                    "name": business.name,
                    "timezone": business.timezone,
                    "services": [{"name": s.name, "minutes": s.minutes, "price": s.price} for s in business.services],
                    "reminders": list(business.reminders_minutes_before),
                },
                "now": now.isoformat(),
                "calendar": core.calendar.name,
                "appointments": [
                    {"id": a.id, "phone": a.phone, "name": a.customer_name, "service": a.service_name,
                     "start": a.start.isoformat(), "end": a.end.isoformat(), "status": a.status,
                     "channel": a.channel, "calendar_state": a.calendar_state, "calendar_error": a.calendar_error,
                     "reminders_sent": a.reminders_sent}
                    for a in appointments
                ],
                "events": events,
            }

        @app.post("/api/sim/blocks")
        def sim_block(block: SimBlock):
            if not isinstance(core.calendar, LocalCalendar):
                raise HTTPException(400, "Add busy time in Google Calendar itself")
            start, end = business_time(block.start), business_time(block.end)
            if end <= start:
                raise HTTPException(422, "The end must be after the start")
            return {"id": core.calendar.add_block(block.summary, start, end)}

        @app.post("/api/sim/reminders")
        def sim_reminders(req: SimReminders):
            return core.tick(business_time(req.at) if req.at else None)

    else:
        @app.get("/", include_in_schema=False)
        def root():
            return {"status": "ok", "docs": "/docs"}

    return app

def export_csv(data=None):
    if data is None:
        return None
    return data

def create_session(data=None):
    if data is None:
        return None
    return data