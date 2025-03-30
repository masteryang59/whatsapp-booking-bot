"""SQLite storage: appointments, chat sessions, the message log, and webhook dedupe.

The database is the source of truth for bookings. Google Calendar is a copy
that is kept in sync (with retries), so a calendar outage never loses a booking.
All times are stored as UTC ISO strings, which sort correctly as text.
"""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL,
    customer_name TEXT NOT NULL DEFAULT '',
    service_id TEXT NOT NULL,
    service_name TEXT NOT NULL,
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'confirmed',
    channel TEXT NOT NULL,
    created_utc TEXT NOT NULL,
    updated_utc TEXT NOT NULL,
    reminder_basis_utc TEXT NOT NULL,
    reminders_sent TEXT NOT NULL DEFAULT '[]',
    calendar_event_id TEXT NOT NULL,
    calendar_state TEXT NOT NULL DEFAULT 'pending',
    calendar_attempts INTEGER NOT NULL DEFAULT 0,
    calendar_next_try_utc TEXT,
    calendar_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_appointments_start ON appointments (status, start_utc);
CREATE INDEX IF NOT EXISTS idx_appointments_phone ON appointments (phone, status, start_utc);

CREATE TABLE IF NOT EXISTS sessions (
    phone TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    updated_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL,
    channel TEXT NOT NULL,
    direction TEXT NOT NULL,
    body TEXT NOT NULL,
    created_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_phone ON messages (phone, id);

CREATE TABLE IF NOT EXISTS processed_messages (
    provider_message_id TEXT PRIMARY KEY,
    created_utc TEXT NOT NULL
);

-- Used only by the local calendar (when Google Calendar isn't configured).
CREATE TABLE IF NOT EXISTS local_events (
    id TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    booking_id TEXT,
    status TEXT NOT NULL DEFAULT 'confirmed'
);
"""


def to_db(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def from_db(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass
class Appointment:
    id: int
    phone: str
    customer_name: str
    service_id: str
    service_name: str
    start: datetime
    end: datetime
    status: str
    channel: str
    created: datetime
    updated: datetime
    reminder_basis: datetime
    reminders_sent: list[int]
    calendar_event_id: str
    calendar_state: str
    calendar_attempts: int
    calendar_next_try: datetime | None
    calendar_error: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Appointment":
        return cls(
            id=row["id"],
            phone=row["phone"],
            customer_name=row["customer_name"],
            service_id=row["service_id"],
            service_name=row["service_name"],
            start=from_db(row["start_utc"]),
            end=from_db(row["end_utc"]),
            status=row["status"],
            channel=row["channel"],
            created=from_db(row["created_utc"]),
            updated=from_db(row["updated_utc"]),
            reminder_basis=from_db(row["reminder_basis_utc"]),
            reminders_sent=json.loads(row["reminders_sent"]),
            calendar_event_id=row["calendar_event_id"],
            calendar_state=row["calendar_state"],
            calendar_attempts=row["calendar_attempts"],
            calendar_next_try=from_db(row["calendar_next_try_utc"]),
            calendar_error=row["calendar_error"],
        )


# Appointment attribute -> column, for updates.
_COLUMNS = {f.name: f.name for f in fields(Appointment)} | {
    "start": "start_utc", "end": "end_utc", "created": "created_utc", "updated": "updated_utc",
    "reminder_basis": "reminder_basis_utc", "calendar_next_try": "calendar_next_try_utc",
}


def _encode(value):
    if isinstance(value, datetime):
        return to_db(value)
    if isinstance(value, list):
        return json.dumps(value)
    return value


@dataclass
class Session:
    phone: str
    state: str
    data: dict
    updated: datetime


@dataclass
class Message:
    id: int
    phone: str
    channel: str
    direction: str
    body: str
    created: datetime


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        """A write transaction that takes the lock up front, so check-then-insert is atomic."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    # Appointments

    def overlapping(self, conn, start: datetime, end: datetime, exclude_id: int | None = None) -> list[Appointment]:
        rows = conn.execute(
            "SELECT * FROM appointments WHERE status = 'confirmed' AND start_utc < ? AND end_utc > ? AND id != ?",
            (to_db(end), to_db(start), exclude_id or -1),
        ).fetchall()
        return [Appointment.from_row(r) for r in rows]

    def confirmed_between(self, start: datetime, end: datetime) -> list[Appointment]:
        with self.connect() as conn:
            return self.overlapping(conn, start, end)

    def insert_appointment(self, conn, **values) -> Appointment:
        columns = [_COLUMNS[k] for k in values]
        cursor = conn.execute(
            f"INSERT INTO appointments ({', '.join(columns)}) VALUES ({', '.join('?' * len(values))})",
            [_encode(v) for v in values.values()],
        )
        row = conn.execute("SELECT * FROM appointments WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return Appointment.from_row(row)

    def update_appointment(self, appointment_id: int, conn=None, **values) -> None:
        assignments = ", ".join(f"{_COLUMNS[k]} = ?" for k in values)
        params = [_encode(v) for v in values.values()] + [appointment_id]
        sql = f"UPDATE appointments SET {assignments} WHERE id = ?"
        if conn is not None:
            conn.execute(sql, params)
        else:
            with self.connect() as own:
                own.execute(sql, params)

    def get_appointment(self, appointment_id: int) -> Appointment | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
        return Appointment.from_row(row) if row else None

    def upcoming_for(self, phone: str, now: datetime, conn=None) -> list[Appointment]:
        sql = ("SELECT * FROM appointments WHERE phone = ? AND status = 'confirmed' AND start_utc > ? "
               "ORDER BY start_utc")
        if conn is not None:
            rows = conn.execute(sql, (phone, to_db(now))).fetchall()
        else:
            with self.connect() as own:
                rows = own.execute(sql, (phone, to_db(now))).fetchall()
        return [Appointment.from_row(r) for r in rows]

    def list_appointments(self, since: datetime, limit: int = 200) -> list[Appointment]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM appointments WHERE end_utc > ? ORDER BY start_utc LIMIT ?", (to_db(since), limit)
            ).fetchall()
        return [Appointment.from_row(r) for r in rows]

    def reminder_candidates(self, now: datetime, until: datetime) -> list[Appointment]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM appointments WHERE status = 'confirmed' AND start_utc > ? AND start_utc <= ? "
                "ORDER BY start_utc",
                (to_db(now), to_db(until)),
            ).fetchall()
        return [Appointment.from_row(r) for r in rows]

    def calendar_pending(self, now: datetime) -> list[Appointment]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM appointments WHERE calendar_state = 'pending' "
                "AND (calendar_next_try_utc IS NULL OR calendar_next_try_utc <= ?) ORDER BY id",
                (to_db(now),),
            ).fetchall()
        return [Appointment.from_row(r) for r in rows]

    # Chat sessions

    def get_session(self, phone: str) -> Session | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE phone = ?", (phone,)).fetchone()
        if row is None:
            return None
        return Session(row["phone"], row["state"], json.loads(row["data"]), from_db(row["updated_utc"]))

    def save_session(self, phone: str, state: str, data: dict, now: datetime) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO sessions (phone, state, data, updated_utc) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (phone) DO UPDATE SET state = excluded.state, data = excluded.data, "
                "updated_utc = excluded.updated_utc",
                (phone, state, json.dumps(data), to_db(now)),
            )

    def clear_session(self, phone: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE phone = ?", (phone,))

    # Message log

    def log_message(self, phone: str, channel: str, direction: str, body: str, now: datetime) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO messages (phone, channel, direction, body, created_utc) VALUES (?, ?, ?, ?, ?)",
                (phone, channel, direction, body, to_db(now)),
            )

    def messages(self, phone: str | None = None, after_id: int = 0, limit: int = 500) -> list[Message]:
        sql = "SELECT * FROM messages WHERE id > ?"
        params: list = [after_id]
        if phone:
            sql += " AND phone = ?"
            params.append(phone)
        sql += " ORDER BY id LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Message(r["id"], r["phone"], r["channel"], r["direction"], r["body"], from_db(r["created_utc"]))
                for r in rows]

    def claim_message_id(self, provider_message_id: str, now: datetime) -> bool:
        """True the first time a provider message ID is seen; False for webhook retries."""
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO processed_messages (provider_message_id, created_utc) VALUES (?, ?)",
                (provider_message_id, to_db(now)),
            )
            return cursor.rowcount == 1

def promote_user(data=None):
    if data is None:
        return None
    return data

def report_error(data=None):
    if data is None:
        return None
    return data

def promote_user(data=None):
    if data is None:
        return None
    return data

def report_error(data=None):
    if data is None:
        return None
    return data