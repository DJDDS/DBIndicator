"""V12 live orchestration.

The orchestration layer is deliberately fail-soft for live scanning: the trade
console is built from data the scanner already has, while the forward option
recorder and official earnings-calendar feed are auxiliary evidence collectors.
An outage in either collector must never stop the normal scan loop.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Callable

import requests

from . import v12_earnings_calendar, v12_feasibility, v12_option_recorder, v12_trade_console

TRIAL25_LOCKED_STATUS = "TRIAL 25 LOCKED — FORWARD INDIAN OPTION DATA REQUIRED."


def _calendar_state(path) -> dict:
    return v12_earnings_calendar._load_state(path)  # central parser/state contract lives in that module


def refresh_earnings_calendar(
    fno_symbols: set[str],
    *,
    now: dt.datetime,
    state_file,
    ledger_file,
    session_factory: Callable[[], object] = requests.Session,
    horizon_days: int = 45,
) -> dict:
    """Refresh the point-in-time earnings calendar at most once per IST date.

    A failed fetch returns UNAVAILABLE while preserving the last-known ledger;
    it never fabricates an inferred quarterly date.
    """
    prior = _calendar_state(state_file)
    last_refresh = prior.get("last_refresh_at")
    if last_refresh:
        try:
            last_date = dt.datetime.fromisoformat(str(last_refresh)).date()
        except (TypeError, ValueError):
            last_date = None
        if last_date == now.date():
            return prior
    last_attempt = prior.get("last_attempt_at")
    if last_attempt:
        try:
            attempt_dt = dt.datetime.fromisoformat(str(last_attempt))
            if attempt_dt.tzinfo is None and now.tzinfo is not None:
                attempt_dt = attempt_dt.replace(tzinfo=now.tzinfo)
            if now - attempt_dt < dt.timedelta(minutes=60):
                return prior
        except (TypeError, ValueError):
            pass

    start = now.date()
    end = start + dt.timedelta(days=max(1, int(horizon_days)))
    fetched = v12_earnings_calendar.fetch_upcoming_earnings(
        session_factory(), set(fno_symbols or set()), start, end
    )
    if fetched.get("status") != "OK":
        failed = {
            **prior,
            "status": "UNAVAILABLE",
            "last_attempt_at": now.isoformat(timespec="seconds"),
            "error": fetched.get("error") or "NSE earnings calendar unavailable",
        }
        v12_earnings_calendar._save_state(state_file, failed)
        return failed
    state = v12_earnings_calendar.record_calendar_observation(
        fetched.get("events") or [],
        now=now,
        ledger_file=ledger_file,
        state_file=state_file,
    )
    state["error"] = None
    state["last_attempt_at"] = now.isoformat(timespec="seconds")
    v12_earnings_calendar._save_state(state_file, state)
    return state



def post_cash_derivative_window(now: dt.datetime) -> bool:
    """True only while derivatives remain open after the legacy 15:30 cash scan."""
    if now.weekday() >= 5:
        return False
    minute = now.hour * 60 + now.minute
    return (15 * 60 + 30) < minute <= (15 * 60 + 40)

def process_live_scan(
    kite,
    results: list[dict],
    radar: dict,
    swing_research: dict,
    *,
    now: dt.datetime,
    option_snapshot_file,
    option_state_file,
    earnings_state_file,
    deep_symbol_limit: int = 40,
    grace_minutes: int = 7,
) -> dict:
    """Build the live candidate console and, when due, record option quotes."""
    trade_console = v12_trade_console.build_trade_console(radar, swing_research, results, limit=5)
    earnings_state = _calendar_state(earnings_state_file)
    earnings_symbols = v12_earnings_calendar.upcoming_earnings_symbols(
        earnings_state, now.date(), days=7
    )
    try:
        recorder = v12_option_recorder.record_snapshot(
            kite,
            results,
            earnings_symbols,
            now=now,
            snapshot_file=option_snapshot_file,
            state_file=option_state_file,
            deep_symbol_limit=deep_symbol_limit,
            grace_minutes=grace_minutes,
        )
    except Exception as exc:  # noqa: BLE001 - recorder evidence must never stop the live scan
        recorder = {"status": "ERROR", "error": str(exc)}

    option_state = v12_option_recorder.load_v12_state(option_state_file)
    feasibility = v12_feasibility.summarize_feasibility(option_state)
    return {
        "trade_console": trade_console,
        "recorder": recorder,
        "feasibility": feasibility,
        "earnings": {
            "status": earnings_state.get("status") or "EMPTY",
            "active_count": earnings_state.get("active_count", 0),
            "last_refresh_at": earnings_state.get("last_refresh_at"),
            "upcoming_7d": list(earnings_symbols),
        },
        "trial25_status": TRIAL25_LOCKED_STATUS,
    }
