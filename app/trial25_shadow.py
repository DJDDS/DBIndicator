"""Persistent, no-P&L Trial-25 event evidence state machine."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from collections import Counter
from pathlib import Path

from . import derivative_intelligence, trial25_calendar, trial25_execution, trial25_stage_d


TERMINAL_UNAVAILABLE_PREFIX = "UNAVAILABLE_"
REQUIRED_ROLES = ("atm_call", "atm_put", "lower_put", "upper_call")
FROZEN_FEASIBILITY_REPORT_SHA256 = "d14361328e8ed09a5ecb81071e55ceff9d890f76dac3c5154e38e5dc32871260"
FROZEN_COHORT_SIZE = 195


def event_id(symbol, meeting_date, entry_date) -> str:
    raw = f"{symbol}|{meeting_date}|{entry_date}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def empty_state() -> dict:
    return {
        "version": 1,
        "events": {},
        "entry_capture_count": 0,
        "exit_capture_count": 0,
        "last_error": None,
        "last_updated_at": None,
    }


def load_state(path) -> dict:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("events"), dict):
            raw.setdefault("entry_capture_count", 0)
            raw.setdefault("exit_capture_count", 0)
            raw.setdefault("last_error", None)
            raw.setdefault("last_updated_at", None)
            return raw
    except (OSError, ValueError, TypeError):
        pass
    return empty_state()


def _atomic_save(path, payload: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str), encoding="utf-8")
    tmp.replace(p)


def _append(path, payload: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str) + "\n")


def _event_key(event: dict) -> str:
    return event_id(event.get("symbol"), event.get("meeting_date"), event.get("entry_date"))


def _transition(ledger_file, event_id_value: str, old: str | None, new: str, when, reason=None) -> None:
    _append(ledger_file, {
        "event_id": event_id_value,
        "from": old,
        "to": new,
        "at": when.isoformat(timespec="seconds"),
        "reason": reason,
    })


def _stale_audit(quotes: dict | None) -> dict:
    """Summarize book executability versus legacy last-trade staleness without P&L."""
    out = {
        "legs": 0,
        "live_book_old_trade_legs": 0,
        "live_book_recent_trade_legs": 0,
        "live_book_unknown_trade_age_legs": 0,
        "non_executable_legs": 0,
    }
    for snap in (quotes or {}).values():
        if not isinstance(snap, dict):
            continue
        out["legs"] += 1
        if not bool(snap.get("two_sided")):
            out["non_executable_legs"] += 1
        elif snap.get("last_trade_stale_600s") is True:
            out["live_book_old_trade_legs"] += 1
        elif snap.get("last_trade_stale_600s") is False:
            out["live_book_recent_trade_legs"] += 1
        else:
            out["live_book_unknown_trade_age_legs"] += 1
    return out


def record_entry(*, state_file, ledger_file, raw_quote_file, event: dict, structure: dict, quotes: dict, captured_at) -> dict:
    state = load_state(state_file)
    key = _event_key(event)
    existing = (state.get("events") or {}).get(key)
    if existing and existing.get("status") in ("ENTRY_CAPTURED", "EXIT_DUE", "COMPLETED_RAW"):
        return dict(existing)
    if existing and str(existing.get("status") or "").startswith(TERMINAL_UNAVAILABLE_PREFIX):
        return dict(existing)

    identities = dict((structure or {}).get("contract_identities") or {})
    if set(identities) != set(REQUIRED_ROLES):
        unavailable = {
            **dict(event),
            "event_id": key,
            "status": "UNAVAILABLE_WING_BOOK",
            "reason": "FOUR_CONTRACT_IDENTITIES_REQUIRED",
        }
        state.setdefault("events", {})[key] = unavailable
        state["last_updated_at"] = captured_at.isoformat(timespec="seconds")
        _transition(ledger_file, key, (existing or {}).get("status"), unavailable["status"], captured_at, unavailable["reason"])
        _atomic_save(state_file, state)
        return dict(unavailable)

    missing = [role for role in REQUIRED_ROLES if role not in (quotes or {})]
    if missing:
        unavailable = {
            **dict(event), "event_id": key, "status": "UNAVAILABLE_ENTRY_BOOK",
            "reason": "MISSING_ENTRY_QUOTES:" + ",".join(missing),
        }
        state.setdefault("events", {})[key] = unavailable
        state["last_updated_at"] = captured_at.isoformat(timespec="seconds")
        _transition(ledger_file, key, (existing or {}).get("status"), unavailable["status"], captured_at, unavailable["reason"])
        _atomic_save(state_file, state)
        return dict(unavailable)

    old_status = (existing or {}).get("status") or event.get("status") or "ENTRY_DUE"
    row = {
        **dict(event),
        "event_id": key,
        "status": "ENTRY_CAPTURED",
        "expiry": structure.get("expiry"),
        "atm_strike": structure.get("atm_strike"),
        "lot_size": int(structure.get("lot_size") or 0),
        "contracts": identities,
        "entry_captured_at": captured_at.isoformat(timespec="seconds"),
        "entry_stale_audit": _stale_audit(quotes),
    }
    _append(raw_quote_file, {
        "event_id": key,
        "capture": "ENTRY",
        "captured_at": row["entry_captured_at"],
        "quotes": quotes,
    })
    _transition(ledger_file, key, old_status, "ENTRY_CAPTURED", captured_at)
    state.setdefault("events", {})[key] = row
    state["entry_capture_count"] = int(state.get("entry_capture_count") or 0) + 1
    state["last_updated_at"] = row["entry_captured_at"]
    _atomic_save(state_file, state)
    return dict(row)


def _quote_identity_matches(expected: dict, observed: dict) -> bool:
    return (
        str(expected.get("tradingsymbol") or "") == str(observed.get("tradingsymbol") or "")
        and str(expected.get("instrument_token")) == str(observed.get("instrument_token"))
    )


def record_exit(*, state_file, ledger_file, raw_quote_file, event_id: str, quotes: dict, captured_at) -> dict:
    state = load_state(state_file)
    event = (state.get("events") or {}).get(event_id)
    if event is None:
        return {"event_id": event_id, "status": "UNAVAILABLE_UNKNOWN_EVENT"}
    if event.get("status") == "COMPLETED_RAW":
        return dict(event)
    if str(event.get("status") or "").startswith(TERMINAL_UNAVAILABLE_PREFIX):
        return dict(event)

    contracts = event.get("contracts") or {}
    for role in REQUIRED_ROLES:
        expected = contracts.get(role)
        observed = (quotes or {}).get(role)
        if expected is None or observed is None:
            event["status"] = "UNAVAILABLE_EXIT_BOOK"
            event["reason"] = f"MISSING_EXIT_QUOTE:{role}"
            event["unavailable_at"] = captured_at.isoformat(timespec="seconds")
            _transition(ledger_file, event_id, "ENTRY_CAPTURED", event["status"], captured_at, event["reason"])
            state["last_updated_at"] = event["unavailable_at"]
            _atomic_save(state_file, state)
            return dict(event)
        if not _quote_identity_matches(expected, observed):
            event["status"] = "UNAVAILABLE_CONTRACT_CHANGED"
            event["reason"] = f"CONTRACT_IDENTITY_MISMATCH:{role}"
            event["unavailable_at"] = captured_at.isoformat(timespec="seconds")
            _transition(ledger_file, event_id, "ENTRY_CAPTURED", event["status"], captured_at, event["reason"])
            state["last_updated_at"] = event["unavailable_at"]
            _atomic_save(state_file, state)
            return dict(event)

    exit_at = captured_at.isoformat(timespec="seconds")
    _append(raw_quote_file, {
        "event_id": event_id,
        "capture": "EXIT",
        "captured_at": exit_at,
        "quotes": quotes,
    })
    _transition(ledger_file, event_id, event.get("status"), "COMPLETED_RAW", captured_at)
    event["status"] = "COMPLETED_RAW"
    event["exit_captured_at"] = exit_at
    event["exit_stale_audit"] = _stale_audit(quotes)
    state["exit_capture_count"] = int(state.get("exit_capture_count") or 0) + 1
    state["last_updated_at"] = exit_at
    _atomic_save(state_file, state)
    return dict(event)


def public_summary(state: dict | None) -> dict:
    events = list(((state or {}).get("events") or {}).values())
    counts = Counter(str(event.get("status") or "UNKNOWN") for event in events)
    unavailable = {k: v for k, v in sorted(counts.items()) if k.startswith(TERMINAL_UNAVAILABLE_PREFIX)}
    stale = {
        "legs": 0,
        "live_book_old_trade_legs": 0,
        "live_book_recent_trade_legs": 0,
        "live_book_unknown_trade_age_legs": 0,
        "non_executable_legs": 0,
    }
    for event in events:
        for key in ("entry_stale_audit", "exit_stale_audit"):
            audit = event.get(key) or {}
            for metric in stale:
                stale[metric] += int(audit.get(metric) or 0)

    queue = []
    for event in sorted(
        events,
        key=lambda row: (
            str(row.get("entry_date") or "9999-12-31"),
            str(row.get("symbol") or ""),
            str(row.get("event_id") or ""),
        ),
    )[:20]:
        queue.append({
            "event_id": event.get("event_id"),
            "symbol": event.get("symbol"),
            "meeting_date": event.get("meeting_date"),
            "entry_date": event.get("entry_date"),
            "exit_date": event.get("exit_date"),
            "status": event.get("status"),
        })
    return {
        "status": "PREREGISTERED_WAITING_EVENTS" if not events else "STAGE_D_COLLECTING",
        "events_total": len(events),
        "entry_captured": counts.get("ENTRY_CAPTURED", 0) + counts.get("EXIT_DUE", 0) + counts.get("COMPLETED_RAW", 0),
        "completed": counts.get("COMPLETED_RAW", 0),
        "target": 40,
        "unavailable_reasons": unavailable,
        "stale_audit": stale,
        "event_queue": queue,
    }


ENTRY_CLOCK = dt.time(15, 10)
EXIT_CLOCK = dt.time(9, 30)
DEFAULT_GRACE_MINUTES = 7


def _parse_date(value) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def capture_kind_due(now: dt.datetime, entry_date, exit_date, *, grace_minutes: int = DEFAULT_GRACE_MINUTES) -> str | None:
    entry = _parse_date(entry_date)
    exit_day = _parse_date(exit_date)
    compare_now = now.replace(tzinfo=None) if now.tzinfo is not None else now
    for kind, day, clock in (("ENTRY", entry, ENTRY_CLOCK), ("EXIT", exit_day, EXIT_CLOCK)):
        if day is None or compare_now.date() != day:
            continue
        scheduled = dt.datetime.combine(day, clock)
        delta = (compare_now - scheduled).total_seconds() / 60.0
        if 0 <= delta <= max(0, int(grace_minutes)):
            return kind
    return None


def _capture_missed(now: dt.datetime, day_value, clock: dt.time, *, grace_minutes: int) -> bool:
    day = _parse_date(day_value)
    if day is None:
        return False
    compare_now = now.replace(tzinfo=None) if now.tzinfo is not None else now
    if compare_now.date() < day:
        return False
    if compare_now.date() > day:
        return True
    scheduled = dt.datetime.combine(day, clock) + dt.timedelta(minutes=max(0, int(grace_minutes)))
    return compare_now > scheduled


def load_frozen_symbols(feasibility_report_file) -> tuple[set[str], str]:
    """Load only the exact immutable 2026-09-21 feasibility cohort."""
    try:
        raw = Path(feasibility_report_file).read_bytes()
    except OSError:
        return set(), "LOCKED_FEASIBILITY"
    if hashlib.sha256(raw).hexdigest() != FROZEN_FEASIBILITY_REPORT_SHA256:
        return set(), "LOCKED_FEASIBILITY_HASH"
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError):
        return set(), "LOCKED_FEASIBILITY"
    feasibility = payload.get("feasibility") if isinstance(payload.get("feasibility"), dict) else payload
    if feasibility.get("status") != "STOCK OPTIONS PRACTICALLY TESTABLE":
        return set(), "LOCKED_FEASIBILITY"
    listed = [str(x) for x in (feasibility.get("tradeable_symbol_list") or []) if str(x)]
    symbols = set(listed)
    if len(listed) != FROZEN_COHORT_SIZE or len(symbols) != FROZEN_COHORT_SIZE:
        return set(), "LOCKED_FEASIBILITY_COHORT_SIZE"
    return symbols, "OK"


def _mark_unavailable(state: dict, ledger_file, event: dict, status: str, when: dt.datetime, reason: str) -> dict:
    if event.get("status") == "COMPLETED_RAW" or str(event.get("status") or "").startswith(TERMINAL_UNAVAILABLE_PREFIX):
        return event
    old = event.get("status")
    event["status"] = status
    event["reason"] = reason
    event["unavailable_at"] = when.isoformat(timespec="seconds")
    state["last_updated_at"] = event["unavailable_at"]
    _transition(ledger_file, event["event_id"], old, status, when, reason)
    return event


def discover_events(state: dict, earnings_state: dict, frozen_symbols: set[str], *, now: dt.datetime, ledger_file) -> dict:
    events = state.setdefault("events", {})
    for symbol, source in sorted(((earnings_state or {}).get("events") or {}).items()):
        symbol = str(symbol)
        if symbol not in frozen_symbols:
            continue
        source = dict(source or {})
        source["symbol"] = symbol

        # A cancellation/removal observed before we have entered must kill the
        # previously discovered event. Once entry is captured, keep the event
        # intention-to-treat; a later calendar change cannot rewrite it.
        if str(source.get("state") or "") == "REMOVED":
            for prior in list(events.values()):
                if prior.get("symbol") == symbol and prior.get("status") in ("DISCOVERED", "ENTRY_DUE"):
                    _mark_unavailable(
                        state, ledger_file, prior, "UNAVAILABLE_REMOVED_BEFORE_ENTRY", now,
                        "POINT_IN_TIME_EARNINGS_EVENT_REMOVED_BEFORE_ENTRY",
                    )
            continue

        resolved = trial25_calendar.resolve_event_sessions(source, now)
        if resolved.get("status") != "KNOWN_BEFORE_ENTRY":
            continue

        # Once an event has entered the market, a later calendar revision may
        # not create a replacement event for the same symbol before its exit.
        open_market_event = next((
            row for row in events.values()
            if row.get("symbol") == symbol and row.get("status") in ("ENTRY_CAPTURED", "EXIT_DUE")
        ), None)
        if open_market_event is not None:
            continue

        key = event_id(symbol, resolved["meeting_date"], resolved["entry_date"])
        if key in events:
            continue
        for prior in list(events.values()):
            if prior.get("symbol") == symbol and prior.get("status") in ("DISCOVERED", "ENTRY_DUE") and prior.get("event_id") != key:
                _mark_unavailable(
                    state, ledger_file, prior, "UNAVAILABLE_REVISED_BEFORE_ENTRY", now,
                    "POINT_IN_TIME_EARNINGS_DATE_REVISED_BEFORE_ENTRY",
                )
        row = {
            "event_id": key,
            "symbol": symbol,
            "meeting_date": resolved["meeting_date"],
            "entry_date": resolved["entry_date"],
            "exit_date": resolved["exit_date"],
            "status": "DISCOVERED",
            "discovered_at": now.isoformat(timespec="seconds"),
            "source_fingerprint": source.get("source_fingerprint"),
            "calendar_first_seen_at": source.get("first_seen_at"),
            "calendar_last_changed_at": source.get("last_changed_at"),
        }
        events[key] = row
        _transition(ledger_file, key, None, "DISCOVERED", now)
    return state


def _eligible_expiry_rows(contracts: list[dict], entry_date: dt.date, exit_date: dt.date) -> tuple[dt.date | None, list[dict]]:
    eligible = []
    for row in contracts or []:
        expiry = _parse_date(row.get("expiry"))
        if row.get("instrument_type") not in ("CE", "PE") or expiry is None:
            continue
        if expiry <= exit_date or (expiry - entry_date).days < 5:
            continue
        eligible.append((expiry, row))
    expiries = sorted({expiry for expiry, _ in eligible})
    if not expiries:
        return None, []
    expiry = expiries[0]
    return expiry, [row for exp, row in eligible if exp == expiry]


def _atm_pair(contracts: list[dict], spot: float, entry_date: dt.date, exit_date: dt.date) -> tuple[dict | None, dict | None]:
    _expiry, rows = _eligible_expiry_rows(contracts, entry_date, exit_date)
    strikes = sorted({float(row.get("strike")) for row in rows if row.get("strike") is not None})
    if not strikes:
        return None, None
    atm = min(strikes, key=lambda strike: (abs(strike - float(spot)), strike))
    call = next((row for row in rows if row.get("instrument_type") == "CE" and float(row.get("strike")) == atm), None)
    put = next((row for row in rows if row.get("instrument_type") == "PE" and float(row.get("strike")) == atm), None)
    return call, put


def _clock_now(reference: dt.datetime) -> dt.datetime:
    return dt.datetime.now(reference.tzinfo) if reference.tzinfo is not None else dt.datetime.now()


def _quote(kite, keys: list[str], reference: dt.datetime, *, sleep_fn, pace: bool) -> tuple[dict, dt.datetime, dt.datetime]:
    if pace:
        sleep_fn(1.02)
    requested = _clock_now(reference)
    payload = kite.quote(keys) or {}
    received = _clock_now(reference)
    if not isinstance(payload, dict):
        raise RuntimeError("Kite quote payload is not a dict")
    return payload, requested, received


def _contract_from_identity(identity: dict) -> dict:
    return {
        "tradingsymbol": identity.get("tradingsymbol"),
        "instrument_token": identity.get("instrument_token"),
        "instrument_type": identity.get("type"),
        "strike": identity.get("strike"),
        "expiry": _parse_date(identity.get("expiry")),
        "lot_size": identity.get("lot_size"),
    }


def frozen_contracts_available(identities: dict, live_contracts: list[dict]) -> bool:
    """True only when every frozen entry contract still exists in the live NFO master."""
    if set(identities or {}) != set(REQUIRED_ROLES):
        return False
    live = {
        (str(row.get("tradingsymbol") or ""), str(row.get("instrument_token")))
        for row in (live_contracts or [])
        if row.get("tradingsymbol") and row.get("instrument_token") is not None
    }
    return all(
        (
            str((identities.get(role) or {}).get("tradingsymbol") or ""),
            str((identities.get(role) or {}).get("instrument_token")),
        ) in live
        for role in REQUIRED_ROLES
    )


def _entry_capture(kite, state, event, contracts_map, *, now, state_file, ledger_file, raw_quote_file, sleep_fn):
    symbol = event["symbol"]
    contracts = list((contracts_map or {}).get(symbol) or [])
    entry_date = _parse_date(event["entry_date"])
    exit_date = _parse_date(event["exit_date"])
    if not contracts or entry_date is None or exit_date is None:
        _mark_unavailable(state, ledger_file, event, "UNAVAILABLE_NOT_CURRENT_FNO", now, "NO_CURRENT_OPTSTK_CONTRACTS")
        _atomic_save(state_file, state)
        return

    spot_payload, _, _ = _quote(kite, [f"NSE:{symbol}"], now, sleep_fn=sleep_fn, pace=True)
    spot = ((spot_payload.get(f"NSE:{symbol}") or {}).get("last_price"))
    try:
        spot = float(spot)
    except (TypeError, ValueError):
        spot = 0.0
    if spot <= 0:
        _mark_unavailable(state, ledger_file, event, "UNAVAILABLE_ENTRY_SNAPSHOT", now, "INVALID_UNDERLYING_SPOT")
        _atomic_save(state_file, state)
        return

    atm_call, atm_put = _atm_pair(contracts, spot, entry_date, exit_date)
    if atm_call is None or atm_put is None:
        _mark_unavailable(state, ledger_file, event, "UNAVAILABLE_EXPIRY", now, "NO_QUALIFYING_ATM_EXPIRY")
        _atomic_save(state_file, state)
        return
    atm_keys = [f"NFO:{atm_call['tradingsymbol']}", f"NFO:{atm_put['tradingsymbol']}"]
    atm_payload, req, recv = _quote(kite, atm_keys, now, sleep_fn=sleep_fn, pace=True)
    atm_c = trial25_execution.normalize_live_quote(atm_call, atm_payload.get(atm_keys[0]) or {}, req, recv)
    atm_p = trial25_execution.normalize_live_quote(atm_put, atm_payload.get(atm_keys[1]) or {}, req, recv)
    for snap in (atm_c, atm_p):
        ok, reason = trial25_execution.validate_leg("SELL", snap, int(snap.get("lot_size") or 0))
        if not ok:
            _mark_unavailable(state, ledger_file, event, "UNAVAILABLE_ATM_BOOK", now, reason or "ATM_BOOK")
            _atomic_save(state_file, state)
            return
    move = float(atm_c["best_ask"]) + float(atm_p["best_ask"])
    structure = trial25_execution.select_structure(contracts, spot, entry_date, exit_date, implied_move_points=move)
    if structure.get("status") != "OK":
        status = "UNAVAILABLE_EXPIRY" if structure.get("status") == "UNAVAILABLE_EXPIRY" else "UNAVAILABLE_WING_BOOK"
        _mark_unavailable(state, ledger_file, event, status, now, structure.get("status") or status)
        _atomic_save(state_file, state)
        return

    roles = structure["contract_identities"]
    keys = [f"NFO:{roles[role]['tradingsymbol']}" for role in REQUIRED_ROLES]
    final_payload, req, recv = _quote(kite, keys, now, sleep_fn=sleep_fn, pace=True)
    snaps = {}
    for role, key in zip(REQUIRED_ROLES, keys):
        snap = trial25_execution.normalize_live_quote(_contract_from_identity(roles[role]), final_payload.get(key) or {}, req, recv)
        side = "SELL" if role in ("atm_call", "atm_put") else "BUY"
        ok, reason = trial25_execution.validate_leg(side, snap, structure["lot_size"])
        if not ok:
            status = "UNAVAILABLE_QUANTITY" if reason == "INSUFFICIENT_TOP_QTY" else ("UNAVAILABLE_ATM_BOOK" if role.startswith("atm_") else "UNAVAILABLE_WING_BOOK")
            _mark_unavailable(state, ledger_file, event, status, now, reason or status)
            _atomic_save(state_file, state)
            return
        snaps[role] = snap
    bid_total = float(snaps["atm_call"]["best_bid"]) + float(snaps["atm_put"]["best_bid"])
    ask_total = float(snaps["atm_call"]["best_ask"]) + float(snaps["atm_put"]["best_ask"])
    mid = (bid_total + ask_total) / 2.0
    spread_pct = (ask_total - bid_total) / mid * 100.0 if mid > 0 else 999.0
    if spread_pct > 4.0:
        _mark_unavailable(state, ledger_file, event, "UNAVAILABLE_ATM_BOOK", now, "ATM_STRADDLE_SPREAD_GT_4PCT")
        _atomic_save(state_file, state)
        return
    event["spot_at_entry"] = spot
    event["atm_straddle_spread_pct"] = round(spread_pct, 4)
    record_entry(
        state_file=state_file, ledger_file=ledger_file, raw_quote_file=raw_quote_file,
        event=event, structure=structure, quotes=snaps, captured_at=now,
    )


def _exit_capture(kite, state, event, contracts_map, *, now, state_file, ledger_file, raw_quote_file, sleep_fn):
    identities = event.get("contracts") or {}
    if set(identities) != set(REQUIRED_ROLES):
        _mark_unavailable(state, ledger_file, event, "UNAVAILABLE_CONTRACT_CHANGED", now, "FROZEN_CONTRACT_SET_INCOMPLETE")
        _atomic_save(state_file, state)
        return
    live_contracts = list((contracts_map or {}).get(event.get("symbol")) or [])
    if not frozen_contracts_available(identities, live_contracts):
        _mark_unavailable(
            state, ledger_file, event, "UNAVAILABLE_CONTRACT_CHANGED", now,
            "FROZEN_EXIT_CONTRACTS_NOT_IN_LIVE_MASTER",
        )
        _atomic_save(state_file, state)
        return
    keys = [f"NFO:{identities[role]['tradingsymbol']}" for role in REQUIRED_ROLES]
    payload, req, recv = _quote(kite, keys, now, sleep_fn=sleep_fn, pace=True)
    snaps = {}
    lot = int(event.get("lot_size") or 0)
    for role, key in zip(REQUIRED_ROLES, keys):
        snap = trial25_execution.normalize_live_quote(_contract_from_identity(identities[role]), payload.get(key) or {}, req, recv)
        side = "BUY" if role in ("atm_call", "atm_put") else "SELL"
        ok, reason = trial25_execution.validate_leg(side, snap, lot)
        if not ok:
            status = "UNAVAILABLE_QUANTITY" if reason == "INSUFFICIENT_TOP_QTY" else "UNAVAILABLE_EXIT_BOOK"
            _mark_unavailable(state, ledger_file, event, status, now, reason or status)
            _atomic_save(state_file, state)
            return
        snaps[role] = snap
    record_exit(
        state_file=state_file, ledger_file=ledger_file, raw_quote_file=raw_quote_file,
        event_id=event["event_id"], quotes=snaps, captured_at=now,
    )


def process_due_events(
    kite,
    *,
    now: dt.datetime,
    earnings_state: dict,
    current_fno_symbols: set[str],
    feasibility_report_file,
    state_file,
    ledger_file,
    raw_quote_file,
    stage_d_file,
    stage_d_hash_file,
    contracts_map: dict | None = None,
    sleep_fn=None,
    grace_minutes: int = DEFAULT_GRACE_MINUTES,
) -> dict:
    """Advance Trial-25 events using only the two preregistered fixed slots."""
    frozen, freeze_status = load_frozen_symbols(feasibility_report_file)
    if freeze_status != "OK":
        return {"status": "LOCKED_FEASIBILITY", "completed": 0, "target": 40}
    state = load_state(state_file)
    discover_events(state, earnings_state, frozen, now=now, ledger_file=ledger_file)
    _atomic_save(state_file, state)
    sleep_fn = sleep_fn or time.sleep

    due_events = []
    for event in list(state.get("events", {}).values()):
        status = event.get("status")
        if status in ("COMPLETED_RAW",) or str(status or "").startswith(TERMINAL_UNAVAILABLE_PREFIX):
            continue
        kind = capture_kind_due(now, event.get("entry_date"), event.get("exit_date"), grace_minutes=grace_minutes)
        if kind == "ENTRY" and status in ("DISCOVERED", "ENTRY_DUE"):
            event["status"] = "ENTRY_DUE"
            due_events.append(("ENTRY", event))
        elif kind == "EXIT" and status in ("ENTRY_CAPTURED", "EXIT_DUE"):
            event["status"] = "EXIT_DUE"
            due_events.append(("EXIT", event))
        elif status in ("DISCOVERED", "ENTRY_DUE") and _capture_missed(now, event.get("entry_date"), ENTRY_CLOCK, grace_minutes=grace_minutes):
            _mark_unavailable(state, ledger_file, event, "UNAVAILABLE_ENTRY_SNAPSHOT", now, "FIXED_1510_SLOT_MISSED")
        elif status in ("ENTRY_CAPTURED", "EXIT_DUE") and _capture_missed(now, event.get("exit_date"), EXIT_CLOCK, grace_minutes=grace_minutes):
            _mark_unavailable(state, ledger_file, event, "UNAVAILABLE_EXIT_BOOK", now, "FIXED_0930_SLOT_MISSED")
    _atomic_save(state_file, state)

    if due_events:
        contracts_map = contracts_map if contracts_map is not None else derivative_intelligence.get_option_contracts_map(kite)
        for kind, event in due_events:
            # New post-freeze names never arrive here; frozen names are gated
            # by actual current option contracts, not merely scanner membership.
            if event["symbol"] not in frozen:
                continue
            if kind == "ENTRY":
                _entry_capture(
                    kite, state, event, contracts_map, now=now, state_file=state_file,
                    ledger_file=ledger_file, raw_quote_file=raw_quote_file, sleep_fn=sleep_fn,
                )
            else:
                _exit_capture(
                    kite, state, event, contracts_map, now=now, state_file=state_file,
                    ledger_file=ledger_file, raw_quote_file=raw_quote_file, sleep_fn=sleep_fn,
                )
            state = load_state(state_file)

    summary = public_summary(state)
    summary["frozen_cohort_size"] = len(frozen)
    summary["current_fno_overlap"] = len(frozen & {str(x) for x in (current_fno_symbols or set())})
    completed = len([e for e in state.get("events", {}).values() if e.get("status") == "COMPLETED_RAW"])
    if completed >= 40:
        calibration = trial25_stage_d.maybe_freeze_calibration(
            state, raw_quote_file, stage_d_file, stage_d_hash_file
        )
        summary["calibration_status"] = calibration.get("status")
        if calibration.get("stage_c_required_n") is not None:
            summary["stage_c_required_n"] = calibration.get("stage_c_required_n")
    else:
        summary["calibration_status"] = "WAITING_FOR_40"
    return summary
