"""Owner-only Friday→Monday NIFTY research alert.

Research scope:
- Normal Friday 14:15 IST uses the frozen historical rule.
- If Friday is an NSE F&O holiday, Thursday 14:15 is recorded separately as
  HOLIDAY_WEEKEND_SHADOW. It is never mixed into the validated Friday sample.
- ATM monthly option only. No ITM/OTM optimisation, no orders, no Telegram,
  and no callbacks into Focus/Actionable desks.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import tempfile
from pathlib import Path
from typing import Iterable

import requests

log = logging.getLogger(__name__)

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NIFTY_QUOTE_KEY = "NSE:NIFTY 50"
THRESHOLD = 0.002
STRIKE_STEP = 50
ROLL_DAYS = 5
CAPTURE_START = (14, 15)
CAPTURE_END = (15, 25)
NSE_HOME = "https://www.nseindia.com/"
NSE_HOLIDAY_API = "https://www.nseindia.com/api/holiday-master?type=trading"

FRIDAY_VALIDATED = "FRIDAY_VALIDATED"
HOLIDAY_WEEKEND_SHADOW = "HOLIDAY_WEEKEND_SHADOW"


def _as_date(value) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_nse_fo_holidays(payload: dict | None) -> set[dt.date]:
    rows = (payload or {}).get("FO") or []
    out: set[dt.date] = set()
    for row in rows:
        day = _as_date((row or {}).get("tradingDate") or (row or {}).get("date"))
        if day is not None:
            out.add(day)
    return out


def session_mode(day: dt.date, holidays: set[dt.date]) -> str | None:
    if day.weekday() == 4 and day not in holidays:
        return FRIDAY_VALIDATED
    if day.weekday() == 3 and day not in holidays and (day + dt.timedelta(days=1)) in holidays:
        return HOLIDAY_WEEKEND_SHADOW
    return None


def in_capture_window(when: dt.datetime) -> bool:
    hm = (when.hour, when.minute)
    return CAPTURE_START <= hm <= CAPTURE_END


def signal_from_open(nifty_open: float, nifty_now: float, threshold: float = THRESHOLD) -> dict:
    o = float(nifty_open)
    p = float(nifty_now)
    if o <= 0:
        return {"signal": "DATA UNAVAILABLE", "reason": "NIFTY session open is unavailable."}
    move = (p - o) / o
    if move >= threshold:
        signal = "BUY CE"
    elif move <= -threshold:
        signal = "BUY PE"
    else:
        signal = "NO TRADE"
    return {
        "signal": signal,
        "move_pct": round(move * 100.0, 4),
        "threshold_pct": round(threshold * 100.0, 4),
    }


def atm_strike(spot: float, step: int = STRIKE_STEP) -> float:
    step = int(step)
    if step <= 0:
        raise ValueError("strike step must be positive")
    return float(math.floor((float(spot) + step / 2.0) / step) * step)


def _is_trading_session(day: dt.date, holidays: set[dt.date]) -> bool:
    return day.weekday() < 5 and day not in holidays


def trading_session_count(start: dt.date, end: dt.date, holidays: set[dt.date]) -> int:
    if end < start:
        return 0
    count = 0
    day = start
    while day <= end:
        if _is_trading_session(day, holidays):
            count += 1
        day += dt.timedelta(days=1)
    return count


def select_monthly_expiry(
    today: dt.date,
    expiries: Iterable[dt.date],
    holidays: set[dt.date],
    *,
    roll_days: int = ROLL_DAYS,
) -> tuple[dt.date, int]:
    future = sorted({_as_date(e) for e in expiries if _as_date(e) is not None and _as_date(e) >= today})
    if not future:
        raise RuntimeError("No upcoming NIFTY monthly expiry found")
    selected = future[0]
    sessions = trading_session_count(today, selected, holidays)
    if sessions <= int(roll_days) and len(future) > 1:
        selected = future[1]
        sessions = trading_session_count(today, selected, holidays)
    return selected, sessions


def monthly_expiries(instruments: Iterable[dict]) -> list[dt.date]:
    by_month: dict[tuple[int, int], dt.date] = {}
    for ins in instruments or []:
        if ins.get("name") != "NIFTY" or ins.get("instrument_type") not in ("CE", "PE"):
            continue
        expiry = _as_date(ins.get("expiry"))
        if expiry is None:
            continue
        key = (expiry.year, expiry.month)
        if key not in by_month or expiry > by_month[key]:
            by_month[key] = expiry
    return sorted(by_month.values())


def find_option(instruments: Iterable[dict], expiry: dt.date, strike: float, option_type: str) -> dict | None:
    for ins in instruments or []:
        if ins.get("name") != "NIFTY" or ins.get("instrument_type") != option_type:
            continue
        if _as_date(ins.get("expiry")) != expiry:
            continue
        try:
            if abs(float(ins.get("strike") or 0) - float(strike)) < 0.01:
                return dict(ins)
        except (TypeError, ValueError):
            continue
    return None


def next_trading_session(day: dt.date, holidays: set[dt.date]) -> dt.date:
    probe = day + dt.timedelta(days=1)
    for _ in range(14):
        if _is_trading_session(probe, holidays):
            return probe
        probe += dt.timedelta(days=1)
    raise RuntimeError("Could not resolve next NSE trading session")


def _read_json(path: str | os.PathLike | None) -> dict:
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh)
            return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _atomic_write_json(path: str | os.PathLike, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True, default=str, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def load_state(path: str | os.PathLike | None) -> dict:
    state = _read_json(path)
    if not state:
        return {
            "status": "WAITING",
            "validation_label": "FORWARD VALIDATION — NOT PRODUCTION VALIDATED",
            "production_controls": False,
            "orders_enabled": False,
        }
    state.setdefault("production_controls", False)
    state.setdefault("orders_enabled", False)
    return state


def _parse_cache(cache: dict) -> set[dt.date]:
    out: set[dt.date] = set()
    for value in cache.get("holidays") or []:
        day = _as_date(value)
        if day is not None:
            out.add(day)
    return out


def get_nse_fo_holidays(
    cache_file: str | os.PathLike,
    *,
    now: dt.datetime | None = None,
    http_session=None,
) -> tuple[set[dt.date], str]:
    now = now or dt.datetime.now(IST)
    today = now.date()
    cached = _read_json(cache_file)
    cached_days = _parse_cache(cached)
    if cached_days and cached.get("fetched_on") == today.isoformat():
        return cached_days, "NSE_CACHE_TODAY"

    sess = http_session or requests.Session()
    headers = {
        "Accept": "application/json,text/plain,*/*",
        "User-Agent": "Mozilla/5.0 (compatible; DBIndicator research calendar)",
        "Referer": NSE_HOME,
    }
    try:
        sess.get(NSE_HOME, headers=headers, timeout=8)
        response = sess.get(NSE_HOLIDAY_API, headers=headers, timeout=8)
        response.raise_for_status()
        payload = response.json()
        days = parse_nse_fo_holidays(payload)
        if not days:
            raise RuntimeError("NSE F&O holiday payload was empty")
        _atomic_write_json(
            cache_file,
            {
                "fetched_on": today.isoformat(),
                "source": "NSE_HOLIDAY_MASTER_FO",
                "holidays": sorted(d.isoformat() for d in days),
            },
        )
        return days, "NSE_LIVE"
    except Exception as exc:
        if cached_days:
            log.warning("NSE holiday refresh failed; using cached calendar: %s", exc)
            return cached_days, "NSE_CACHE_STALE"
        raise RuntimeError(f"NSE holiday calendar unavailable: {exc}") from exc


def _quote_timestamp_age_seconds(value, now: dt.datetime) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        parsed = None
        for text in (value, value.replace(" ", "T")):
            try:
                parsed = dt.datetime.fromisoformat(text)
                break
            except ValueError:
                continue
        value = parsed
    if not isinstance(value, dt.datetime):
        return None

    # Kite may return naive IST timestamps while callers may pass either naive
    # or timezone-aware datetimes. Normalize both sides before subtraction so
    # the recorder never mixes offset-naive and offset-aware datetime objects.
    if value.tzinfo is None:
        value = value.replace(tzinfo=IST)
    else:
        value = value.astimezone(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    else:
        now = now.astimezone(IST)

    return max(0.0, (now - value).total_seconds())


def _nifty_quote(kite, now: dt.datetime) -> tuple[float, float, float | None]:
    quote = kite.quote([NIFTY_QUOTE_KEY])[NIFTY_QUOTE_KEY]
    ohlc = quote.get("ohlc") or {}
    nifty_open = float(ohlc.get("open") or 0)
    spot = float(quote.get("last_price") or 0)
    if nifty_open <= 0 or spot <= 0:
        raise RuntimeError("NIFTY open/spot quote is unavailable")
    age = _quote_timestamp_age_seconds(quote.get("timestamp"), now)
    if age is not None and age > 120:
        raise RuntimeError(f"NIFTY quote is stale ({age:.0f}s)")
    return nifty_open, spot, age


def _option_quote(kite, symbol: str) -> dict:
    quote = kite.quote([symbol]).get(symbol) or {}
    depth = quote.get("depth") or {}
    bids = depth.get("buy") or []
    asks = depth.get("sell") or []
    bid = float((bids[0] or {}).get("price") or 0) if bids else 0.0
    ask = float((asks[0] or {}).get("price") or 0) if asks else 0.0
    last = float(quote.get("last_price") or 0)
    spread = (ask - bid) if bid > 0 and ask > 0 and ask >= bid else None
    spread_pct = (spread / ((ask + bid) / 2.0) * 100.0) if spread is not None and (ask + bid) > 0 else None
    return {
        "premium": round(last, 2) if last > 0 else None,
        "bid": round(bid, 2) if bid > 0 else None,
        "ask": round(ask, 2) if ask > 0 else None,
        "spread_points": round(spread, 2) if spread is not None else None,
        "spread_pct": round(spread_pct, 2) if spread_pct is not None else None,
        "volume": quote.get("volume"),
        "oi": quote.get("oi"),
    }


def evaluate(kite, *, when: dt.datetime, holidays: set[dt.date], calendar_source: str) -> dict:
    mode = session_mode(when.date(), holidays)
    base = {
        "status": "CAPTURED",
        "captured_at": when.isoformat(timespec="seconds"),
        "capture_date": when.date().isoformat(),
        "session_mode": mode,
        "validation_label": (
            "HISTORICAL FRIDAY RULE · FORWARD VALIDATION"
            if mode == FRIDAY_VALIDATED
            else "HOLIDAY-WEEKEND SHADOW · UNVALIDATED"
        ),
        "threshold_pct": THRESHOLD * 100.0,
        "calendar_source": calendar_source,
        "production_controls": False,
        "orders_enabled": False,
    }
    if mode is None:
        return {**base, "status": "INELIGIBLE", "signal": "NONE"}
    if not in_capture_window(when):
        return {**base, "status": "WAITING", "signal": "WAIT"}

    nifty_open, spot, quote_age = _nifty_quote(kite, when)
    decision = signal_from_open(nifty_open, spot)
    out = {
        **base,
        **decision,
        "nifty_open": round(nifty_open, 2),
        "nifty_spot": round(spot, 2),
        "quote_age_seconds": round(quote_age, 1) if quote_age is not None else None,
    }
    if decision["signal"] == "NO TRADE":
        return out

    option_type = "CE" if decision["signal"] == "BUY CE" else "PE"
    instruments = kite.instruments("NFO")
    expiries = monthly_expiries(instruments)
    expiry, dte_sessions = select_monthly_expiry(when.date(), expiries, holidays)
    strike = atm_strike(spot)
    contract = find_option(instruments, expiry, strike, option_type)
    out.update(
        option_type=option_type,
        strike=strike,
        expiry=expiry.isoformat(),
        trading_sessions_to_expiry=dte_sessions,
        exit_session=next_trading_session(when.date(), holidays).isoformat(),
        exit_window="09:15-09:30 IST",
        contract_policy="ATM MONTHLY ONLY — FROZEN RESEARCH RULE",
    )
    if not contract:
        return {
            **out,
            "status": "CONTRACT UNAVAILABLE",
            "contract_status": "NO EXECUTABLE ATM CONTRACT",
            "option": None,
        }

    symbol = f"NFO:{contract['tradingsymbol']}"
    try:
        option_quote = _option_quote(kite, symbol)
    except Exception as exc:
        log.warning("Friday-weekend option quote failed for %s: %s", symbol, exc)
        option_quote = {
            "premium": None, "bid": None, "ask": None,
            "spread_points": None, "spread_pct": None,
            "volume": None, "oi": None,
        }
    return {
        **out,
        "option": contract["tradingsymbol"],
        "lot_size": contract.get("lot_size"),
        "contract_status": "ATM CONTRACT RESOLVED",
        **option_quote,
    }


def maybe_capture(
    kite,
    *,
    state_file: str | os.PathLike,
    holiday_cache_file: str | os.PathLike,
    now: dt.datetime | None = None,
) -> dict:
    now = now or dt.datetime.now(IST)
    previous = load_state(state_file)
    if previous.get("capture_date") == now.date().isoformat():
        return previous
    if not in_capture_window(now):
        return previous

    try:
        holidays, calendar_source = get_nse_fo_holidays(holiday_cache_file, now=now)
    except Exception as exc:
        log.warning("Friday-weekend alert calendar unavailable: %s", exc)
        return {
            **previous,
            "status": "CALENDAR UNAVAILABLE",
            "last_error": str(exc),
            "production_controls": False,
            "orders_enabled": False,
        }

    mode = session_mode(now.date(), holidays)
    if mode is None:
        return previous

    try:
        state = evaluate(kite, when=now, holidays=holidays, calendar_source=calendar_source)
    except Exception as exc:
        log.exception("Friday-weekend alert evaluation failed")
        state = {
            "status": "DATA UNAVAILABLE",
            "capture_date": now.date().isoformat(),
            "captured_at": now.isoformat(timespec="seconds"),
            "session_mode": mode,
            "signal": "NONE",
            "reason": str(exc),
            "calendar_source": calendar_source,
            "validation_label": (
                "HISTORICAL FRIDAY RULE · FORWARD VALIDATION"
                if mode == FRIDAY_VALIDATED
                else "HOLIDAY-WEEKEND SHADOW · UNVALIDATED"
            ),
            "production_controls": False,
            "orders_enabled": False,
        }
    _atomic_write_json(state_file, state)
    return state
