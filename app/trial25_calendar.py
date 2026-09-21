"""Trial-25 verified NSE F&O trading-calendar helpers.

Only explicitly versioned calendar years are accepted. Unknown years fail
closed so a Trial-25 fixed-clock capture is never silently placed on a
weekend or exchange holiday.
"""
from __future__ import annotations

import datetime as dt


# NSE 2026 F&O holiday calendar, including the 15-Jan-2026 added holiday.
FNO_HOLIDAYS: dict[int, frozenset[dt.date]] = {
    2026: frozenset({
        dt.date(2026, 1, 15),
        dt.date(2026, 1, 26),
        dt.date(2026, 3, 3),
        dt.date(2026, 3, 26),
        dt.date(2026, 3, 31),
        dt.date(2026, 4, 3),
        dt.date(2026, 4, 14),
        dt.date(2026, 5, 1),
        dt.date(2026, 5, 28),
        dt.date(2026, 6, 26),
        dt.date(2026, 9, 14),
        dt.date(2026, 10, 2),
        dt.date(2026, 10, 20),
        dt.date(2026, 11, 10),
        dt.date(2026, 11, 24),
        dt.date(2026, 12, 25),
    }),
}


def _parse_date(value) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _parse_dt(value) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value
    if value is None:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _comparable(value: dt.datetime | None, reference: dt.datetime) -> dt.datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None and reference.tzinfo is None:
        return value.replace(tzinfo=None)
    if value.tzinfo is None and reference.tzinfo is not None:
        return value.replace(tzinfo=reference.tzinfo)
    return value


def is_fno_trading_day(day: dt.date) -> bool:
    holidays = FNO_HOLIDAYS.get(day.year)
    if holidays is None:
        return False
    return day.weekday() < 5 and day not in holidays


def previous_fno_session(day: dt.date) -> dt.date | None:
    if day.year not in FNO_HOLIDAYS:
        return None
    cur = day - dt.timedelta(days=1)
    for _ in range(14):
        if cur.year not in FNO_HOLIDAYS:
            return None
        if is_fno_trading_day(cur):
            return cur
        cur -= dt.timedelta(days=1)
    return None


def next_fno_session(day: dt.date) -> dt.date | None:
    if day.year not in FNO_HOLIDAYS:
        return None
    cur = day + dt.timedelta(days=1)
    for _ in range(14):
        if cur.year not in FNO_HOLIDAYS:
            return None
        if is_fno_trading_day(cur):
            return cur
        cur += dt.timedelta(days=1)
    return None


def resolve_event_sessions(event: dict, observed_before: dt.datetime) -> dict:
    """Resolve the meeting date that was actually knowable before entry."""
    event = dict(event or {})
    state = str(event.get("state") or "")
    if state not in ("ACTIVE", "REVISED"):
        return {"status": "EVENT_NOT_ACTIVE", "symbol": event.get("symbol")}

    first_seen = _comparable(_parse_dt(event.get("first_seen_at")), observed_before)
    if first_seen is None or first_seen > observed_before:
        return {"status": "EVENT_NOT_KNOWN_BEFORE_ENTRY", "symbol": event.get("symbol")}

    meeting = _parse_date(event.get("meeting_date"))
    changed_at = _comparable(_parse_dt(event.get("last_changed_at")), observed_before)
    if state == "REVISED" and changed_at is not None and changed_at > observed_before:
        prior = _parse_date(event.get("previous_meeting_date"))
        if prior is not None:
            meeting = prior

    if meeting is None:
        return {"status": "INVALID_MEETING_DATE", "symbol": event.get("symbol")}
    if meeting.year not in FNO_HOLIDAYS:
        return {
            "status": "UNVERIFIED_TRADING_CALENDAR",
            "symbol": event.get("symbol"),
            "meeting_date": meeting.isoformat(),
        }

    entry = previous_fno_session(meeting)
    exit_day = next_fno_session(meeting)
    if entry is None or exit_day is None:
        return {
            "status": "UNVERIFIED_TRADING_CALENDAR",
            "symbol": event.get("symbol"),
            "meeting_date": meeting.isoformat(),
        }
    return {
        "status": "KNOWN_BEFORE_ENTRY",
        "symbol": event.get("symbol"),
        "meeting_date": meeting.isoformat(),
        "entry_date": entry.isoformat(),
        "exit_date": exit_day.isoformat(),
    }
