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

from . import config, derivative_intelligence, trial25_shadow, trial25_universe, v12_earnings_calendar, v12_feasibility, v12_option_recorder, v12_trade_console

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
    force: bool = False,
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
        if last_date == now.date() and not force:
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



def trial25_preentry_calendar_refresh_due(now: dt.datetime) -> bool:
    """Refresh NSE earnings once near the 15:10 Trial-25 entry window."""
    if now.weekday() >= 5:
        return False
    minute = now.hour * 60 + now.minute
    return (15 * 60) <= minute <= (15 * 60 + 17)


def post_cash_derivative_window(now: dt.datetime) -> bool:
    """True only while derivatives remain open after the legacy 15:30 cash scan."""
    if now.weekday() >= 5:
        return False
    minute = now.hour * 60 + now.minute
    return (15 * 60 + 30) < minute <= (15 * 60 + 40)

def _trial25_process(kite, *, now, earnings_state, current_fno_symbols):
    frozen, freeze_status = trial25_shadow.load_frozen_symbols(
        config.TRIAL25_FEASIBILITY_FREEZE_FILE
    )
    if freeze_status != "OK":
        return {"status": "LOCKED_FEASIBILITY", "completed": 0, "target": 40}

    # Use the actual current NFO contract master, not only the scanner's cash
    # universe, so post-freeze F&O additions and phased removals are recorded
    # exactly as contracts become available/unavailable.
    contracts_map = derivative_intelligence.get_option_contracts_map(kite)
    universe = trial25_universe.update_universe_state(
        config.TRIAL25_ONBOARDING_FILE, frozen, contracts_map, now
    )
    summary = trial25_shadow.process_due_events(
        kite,
        now=now,
        earnings_state=earnings_state,
        current_fno_symbols=set(current_fno_symbols or set()),
        feasibility_report_file=config.TRIAL25_FEASIBILITY_FREEZE_FILE,
        state_file=config.TRIAL25_STATE_FILE,
        ledger_file=config.TRIAL25_LEDGER_FILE,
        raw_quote_file=config.TRIAL25_RAW_QUOTES_FILE,
        stage_d_file=config.TRIAL25_STAGE_D_FILE,
        stage_d_hash_file=config.TRIAL25_STAGE_D_HASH_FILE,
        contracts_map=contracts_map,
        grace_minutes=config.V12_SNAPSHOT_GRACE_MINUTES,
    )
    summary["cohort_a_live"] = len(universe.get("cohort_a_live") or [])
    summary["cohort_a_missing_contracts"] = len(universe.get("cohort_a_missing_contracts") or [])
    summary["new_fno_onboarding"] = len(universe.get("new_fno_onboarding") or [])
    summary["universe_asof"] = universe.get("asof")
    return summary


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
    current_fno_symbols: set[str] | None = None,
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

    try:
        trial25 = _trial25_process(
            kite,
            now=now,
            earnings_state=earnings_state,
            current_fno_symbols=current_fno_symbols or {str(r.get("symbol")) for r in results if r.get("symbol")},
        )
    except Exception as exc:  # noqa: BLE001 - Trial 25 can never stop V12/live scanning
        trial25 = {"status": "ERROR", "error": str(exc), "completed": 0, "target": 40}

    option_state = v12_option_recorder.load_v12_state(option_state_file)
    feasibility = v12_feasibility.summarize_feasibility(option_state)
    health = v12_option_recorder.recorder_health(
        option_snapshot_file, option_state_file, now=now,
        storage_mode=config.V12_STORAGE_MODE, storage_root=config.V12_STORAGE_ROOT,
    )
    recorder = {**recorder, "health": health}
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
        "trial25_shadow": trial25,
        "trial25_status": (
            TRIAL25_LOCKED_STATUS
            if trial25.get("status") == "LOCKED_FEASIBILITY"
            else "TRIAL 25 PREREGISTERED — STAGE D SHADOW COLLECTION."
        ),
    }
