"""Captures docs/screenshot.png from a real run of the simulator.

Starts the app on a throwaway database, adds a busy block to the local
calendar, chats through a booking in headless Chromium, books a second
appointment for another customer, runs the reminder check as of two hours
before the booking, and takes the screenshot.

Needs Playwright, which isn't in requirements-dev.txt:
    pip install playwright && playwright install chromium
    python scripts/make_screenshot.py
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
OUT = ROOT / "docs" / "screenshot.png"


def call(path: str, payload: dict) -> dict:
    request = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def wait_for_server() -> None:
    for _ in range(100):
        try:
            urllib.request.urlopen(BASE + "/api/health")
            return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("server did not start")


def next_open_day(business: dict, after: date) -> date:
    names = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    day = after + timedelta(days=1)
    while not business["hours"][names[day.weekday()]] or day.isoformat() in business["closed_dates"]:
        day += timedelta(days=1)
    return day


def main() -> None:
    business = json.loads((ROOT / "data" / "business.json").read_text(encoding="utf-8"))
    today = datetime.now(ZoneInfo(business["timezone"])).date()
    day1 = next_open_day(business, today)

    workdir = Path(tempfile.mkdtemp())
    env = os.environ | {
        "DB_PATH": str(workdir / "demo.db"),
        "REMINDER_INTERVAL_SECONDS": "0",
        "GOOGLE_CALENDAR_ID": "",
        "META_ACCESS_TOKEN": "",
        "TWILIO_ACCOUNT_SID": "",
    }
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory", "--port", str(PORT)],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_server()
        # The owner has a dentist appointment, so the bot must work around it.
        call("/api/sim/blocks", {"start": f"{day1}T13:30", "end": f"{day1}T15:00", "summary": "Dentist"})
        # Another customer already booked.
        call("/api/sim/messages", {"phone": "+447700900456", "name": "Priya Shah",
                                   "text": f"haircut and beard {day1:%d %b} at 10:30"})
        call("/api/sim/messages", {"phone": "+447700900456", "name": "Priya Shah", "text": "yes"})

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1360, "height": 860}, device_scale_factor=2,
                                    color_scheme="light")
            page.goto(BASE)
            page.wait_for_selector(".chat-title:not(:text('…'))")

            def send(text: str) -> None:
                count = page.locator(".bubble").count()
                page.fill("#text", text)
                page.press("#text", "Enter")
                page.wait_for_function(f"document.querySelectorAll('.bubble').length >= {count + 2}")

            send(f"Hi, can I get a haircut on {day1:%A} at 2pm?")
            send("15:00")
            send("yes")
            # Time-travel to two hours before the haircut, when its reminder is due.
            page.fill("#reminder-at", f"{day1}T13:00")
            page.click("#reminders button")
            page.wait_for_function("document.querySelector('#reminder-result').textContent.startsWith('Sent')")
            page.wait_for_timeout(3000)  # let the poll pick up the new messages and bookings
            page.locator("#chat").evaluate("el => el.scrollTop = el.scrollHeight")
            OUT.parent.mkdir(exist_ok=True)
            page.screenshot(path=str(OUT), full_page=True)
            browser.close()
        print(f"Saved {OUT.relative_to(ROOT)}")
    finally:
        server.terminate()
        server.wait()


if __name__ == "__main__":
    main()

def aggregate_stats(data=None):
    if data is None:
        return None
    return data

def check_status(data=None):
    if data is None:
        return None
    return data

def aggregate_stats(data=None):
    if data is None:
        return None
    return data

def check_status(data=None):
    if data is None:
        return None
    return data