import datetime as dt

from app import trial25_calendar as cal


def test_fno_holiday_and_weekend_resolution():
    assert cal.is_fno_trading_day(dt.date(2026, 9, 14)) is False
    assert cal.previous_fno_session(dt.date(2026, 9, 14)) == dt.date(2026, 9, 11)
    assert cal.next_fno_session(dt.date(2026, 9, 14)) == dt.date(2026, 9, 15)


def test_october_holidays_are_not_guessed_as_sessions():
    assert cal.is_fno_trading_day(dt.date(2026, 10, 2)) is False
    assert cal.previous_fno_session(dt.date(2026, 10, 2)) == dt.date(2026, 10, 1)
    assert cal.next_fno_session(dt.date(2026, 10, 2)) == dt.date(2026, 10, 5)


def test_unknown_calendar_year_fails_closed():
    assert cal.is_fno_trading_day(dt.date(2027, 1, 4)) is False
    assert cal.previous_fno_session(dt.date(2027, 1, 4)) is None
    assert cal.next_fno_session(dt.date(2027, 1, 4)) is None
    out = cal.resolve_event_sessions(
        {
            "symbol": "ABC",
            "meeting_date": "2027-01-04",
            "state": "ACTIVE",
            "first_seen_at": "2026-12-20T10:00:00+05:30",
            "last_changed_at": "2026-12-20T10:00:00+05:30",
        },
        dt.datetime(2026, 12, 31, 15, 10),
    )
    assert out["status"] == "UNVERIFIED_TRADING_CALENDAR"


def test_event_known_before_entry_resolves_fixed_sessions():
    observed_before = dt.datetime(2026, 10, 8, 15, 10)
    event = {
        "symbol": "INFY",
        "meeting_date": "2026-10-09",
        "state": "ACTIVE",
        "first_seen_at": "2026-10-01T09:20:00+05:30",
        "last_changed_at": "2026-10-01T09:20:00+05:30",
    }
    out = cal.resolve_event_sessions(event, observed_before)
    assert out["status"] == "KNOWN_BEFORE_ENTRY"
    assert out["meeting_date"] == "2026-10-09"
    assert out["entry_date"] == "2026-10-08"
    assert out["exit_date"] == "2026-10-12"


def test_revision_first_observed_after_entry_cannot_rewrite_event():
    observed_before = dt.datetime(2026, 10, 8, 15, 10)
    event = {
        "symbol": "INFY",
        "meeting_date": "2026-10-12",
        "state": "REVISED",
        "first_seen_at": "2026-10-01T09:20:00+05:30",
        "last_changed_at": "2026-10-09T08:00:00+05:30",
        "previous_meeting_date": "2026-10-09",
    }
    out = cal.resolve_event_sessions(event, observed_before)
    assert out["status"] == "KNOWN_BEFORE_ENTRY"
    assert out["meeting_date"] == "2026-10-09"
    assert out["entry_date"] == "2026-10-08"
    assert out["exit_date"] == "2026-10-12"


def test_removed_event_fails_closed():
    out = cal.resolve_event_sessions(
        {
            "symbol": "INFY",
            "meeting_date": "2026-10-09",
            "state": "REMOVED",
            "first_seen_at": "2026-10-01T09:20:00+05:30",
            "last_changed_at": "2026-10-07T11:00:00+05:30",
        },
        dt.datetime(2026, 10, 8, 15, 10),
    )
    assert out["status"] == "EVENT_NOT_ACTIVE"
