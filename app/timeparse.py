"""Small, predictable parser for the dates and times people type in chat.

It understands things like "tomorrow 3pm", "fri at 10:30", "3rd nov",
"Nov 3", "03/11", "2026-11-03 14:00" and "noon". It deliberately doesn't
guess at anything vaguer ("sometime next week", "after work"): the bot then
shows a numbered list instead, which is quicker than a wrong guess.
"""

import re
from dataclasses import dataclass
from datetime import date, time, timedelta

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9, "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tues": 1, "tue": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thurs": 3, "thur": 3, "thu": 3, "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
_MONTH = "|".join(sorted(MONTHS, key=len, reverse=True))
_WEEKDAY = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
_AMPM = r"(a\.?m\.?|p\.?m\.?)"
_ORD = r"(?:st|nd|rd|th)?"

# Order matters: the most explicit forms are tried first.
_TIME_PATTERNS = [
    re.compile(rf"\b(\d{{1,2}})[:.](\d{{2}})\s*{_AMPM}(?![a-z])"),     # 3:30pm, 3.30 pm
    re.compile(r"\b(\d{1,2}):(\d{2})\b"),                             # 15:30
    re.compile(rf"\b(\d{{1,2}})\s*{_AMPM}(?![a-z])"),                  # 3pm, 11 am
    re.compile(r"\b(\d{1,2})\s*o'?\s?clock\b"),                       # 3 o'clock
    re.compile(rf"\bat\s+(\d{{1,2}})\b(?!\s*(?:{_ORD}\s*(?:of\s+)?(?:{_MONTH})\b|[/.\-]\d|st\b|nd\b|rd\b|th\b))"),  # at 3
]
_NOON = re.compile(r"\b(noon|midday|12\s*noon)\b")


_Time = time  # the field below shadows the type inside the class body


@dataclass(frozen=True)
class When:
    day: date | None = None
    time: _Time | None = None


def _hour_from_ampm(hour: int, marker: str | None) -> int | None:
    if marker is None:
        return hour if 0 <= hour <= 23 else None
    if not 1 <= hour <= 12:
        return None
    pm = marker.startswith("p")
    if hour == 12:
        return 12 if pm else 0
    return hour + 12 if pm else hour


def parse_time(text: str) -> tuple[time | None, str]:
    """Returns the time found in `text` and the text with that part removed."""
    text = text.lower()
    match = _NOON.search(text)
    if match:
        return time(12, 0), text[: match.start()] + " " + text[match.end():]

    for index, pattern in enumerate(_TIME_PATTERNS):
        match = pattern.search(text)
        if not match:
            continue
        groups = match.groups()
        hour = int(groups[0])
        minute = 0
        marker = None
        if index in (0, 1):
            minute = int(groups[1])
        if index == 0:
            marker = groups[2]
        elif index == 2:
            marker = groups[1]
        if 1 <= hour <= 7 and (index in (3, 4) or (index == 1 and len(groups[0]) == 1)):
            # "at 3", "3 o'clock" or "2:15" in a booking chat means the afternoon ("07:30" doesn't).
            hour += 12
        hour = _hour_from_ampm(hour, marker.replace(".", "") if marker else None)
        if hour is None or not 0 <= minute <= 59:
            return None, text
        return time(hour, minute), text[: match.start()] + " " + text[match.end():]
    return None, text


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _upcoming(today: date, month: int, day: int, year: int | None) -> date | None:
    """The date with no year given is the next one on or after today."""
    if year is not None:
        if year < 100:
            year += 2000
        return _safe_date(year, month, day)
    candidate = _safe_date(today.year, month, day)
    if candidate is None or candidate < today:
        candidate = _safe_date(today.year + 1, month, day)
    return candidate


def parse_day(text: str, today: date, date_order: str = "DMY") -> date | None:
    text = text.lower()

    if re.search(r"\b(day after tomorrow|day after tmrw)\b", text):
        return today + timedelta(days=2)
    if re.search(r"\b(tomorrow|tomorow|tommorow|tmrw|tmr|tmw)\b", text):
        return today + timedelta(days=1)
    if re.search(r"\b(today|tonight|this (morning|afternoon|evening))\b", text):
        return today

    match = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text)
    if match:
        return _safe_date(*map(int, match.groups()))

    match = re.search(r"\b(\d{1,2})[/.\-](\d{1,2})(?:[/.\-](\d{2}|\d{4}))?\b", text)
    if match:
        first, second = int(match.group(1)), int(match.group(2))
        day, month = (first, second) if date_order == "DMY" else (second, first)
        year = int(match.group(3)) if match.group(3) else None
        return _upcoming(today, month, day, year)

    match = re.search(rf"\b(\d{{1,2}}){_ORD}\s+(?:of\s+)?({_MONTH})\b(?:,?\s+(\d{{4}}))?", text)
    if match:
        year = int(match.group(3)) if match.group(3) else None
        return _upcoming(today, MONTHS[match.group(2)], int(match.group(1)), year)

    match = re.search(rf"\b({_MONTH})\s+(?:the\s+)?(\d{{1,2}}){_ORD}\b(?:,?\s+(\d{{4}}))?", text)
    if match:
        year = int(match.group(3)) if match.group(3) else None
        return _upcoming(today, MONTHS[match.group(1)], int(match.group(2)), year)

    match = re.search(rf"\b(this\s+|next\s+)?({_WEEKDAY})\b", text)
    if match:
        target = WEEKDAYS[match.group(2)]
        ahead = (target - today.weekday()) % 7
        if ahead == 0 and (match.group(1) or "").strip() != "this":
            ahead = 7
        return today + timedelta(days=ahead)

    match = re.search(r"\b(?:the\s+)(\d{1,2})(?:st|nd|rd|th)?\b|\b(\d{1,2})(?:st|nd|rd|th)\b", text)
    if match:
        day = int(match.group(1) or match.group(2))
        candidate = _safe_date(today.year, today.month, day)
        if candidate is None or candidate < today:
            nxt = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
            candidate = _safe_date(nxt.year, nxt.month, day)
        return candidate
    return None


def parse_when(text: str, today: date, date_order: str = "DMY") -> When:
    """Finds a day and/or a time in free text. Either part may be missing."""
    found_time, rest = parse_time(text)
    return When(day=parse_day(rest, today, date_order), time=found_time)


def parse_choice(text: str, count: int) -> int | None:
    """A numbered-list answer ("2", "2.", "#2", "option 2") as a 0-based index."""
    match = re.fullmatch(r"\s*(?:option\s*|no\.?\s*|#)?(\d{1,2})\s*[.)]?\s*", text.lower())
    if not match:
        return None
    number = int(match.group(1))
    return number - 1 if 1 <= number <= count else None

def prune_old_data(data=None):
    if data is None:
        return None
    return data

def process_data(data=None):
    if data is None:
        return None
    return data

def prune_old_data(data=None):
    if data is None:
        return None
    return data

def process_data(data=None):
    if data is None:
        return None
    return data