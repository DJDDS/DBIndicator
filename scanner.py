"""
Pulls candles from Kite for each stock in the watchlist and runs the
Scanner confluence logic on them. Kite's historical_data() needs an
instrument_token (not just the trading symbol), so the instrument list
is fetched once and cached in memory.
"""
import datetime as dt
import logging

import pandas as pd

from .config import settings
from .indicators import compute_signal

log = logging.getLogger(__name__)

_instrument_cache = {}

# Index/sector futures on NFO whose underlying isn't a tradeable stock -
# excluded when deriving the F&O stock universe from Kite's instrument
# list (see get_fno_stock_list below).
_NON_STOCK_FNO_NAMES = {
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
    "NIFTY NEXT 50", "SENSEX", "BANKEX", "NIFTYIT", "NIFTY IT",
}

_fno_cache = {"date": None, "symbols": []}


def _load_instrument_map(kite):
    global _instrument_cache
    if _instrument_cache:
        return _instrument_cache
    instruments = kite.instruments("NSE")
    _instrument_cache = {
        row["tradingsymbol"]: row["instrument_token"]
        for row in instruments
        if row.get("segment") == "NSE"
    }
    return _instrument_cache


def get_fno_stock_list(kite) -> list:
    """Returns the exact list of NSE stocks currently eligible for F&O
    trading, derived live from Kite's own NFO instrument list (the
    stock-futures "name" field) rather than a hardcoded list that could
    go stale as SEBI/NSE periodically revise F&O eligibility. Cached
    for the day since this rarely changes intraday."""
    today = dt.date.today().isoformat()
    if _fno_cache["date"] == today and _fno_cache["symbols"]:
        return _fno_cache["symbols"]
    instruments = kite.instruments("NFO")
    names = set()
    for row in instruments:
        if row.get("instrument_type") == "FUT":
            name = (row.get("name") or "").strip()
            if name and name.upper() not in _NON_STOCK_FNO_NAMES:
                names.add(name)
    symbols = sorted(names)
    if symbols:
        _fno_cache["date"] = today
        _fno_cache["symbols"] = symbols
    return symbols


def _lookback_days(timeframe: str) -> int:
    # Enough history for indicator warm-up (BB-20/MACD-slow) plus a
    # reasonable scanning window, without requesting more than Kite
    # allows in one call for a given interval.
    return {
        "15minute": 15,
        "30minute": 30,
        "60minute": 90,
        "4hour": 120,
        "day": 400,
    }.get(timeframe, 30)


def fetch_candles(kite, instrument_token, timeframe: str) -> pd.DataFrame:
    to_date = dt.datetime.now()
    from_date = to_date - dt.timedelta(days=_lookback_days(timeframe))
    if timeframe == "4hour":
        interval = "60minute"
    elif timeframe == "week":
        interval = "day"
    else:
        interval = timeframe
    data = kite.historical_data(instrument_token, from_date, to_date, interval)
    df = pd.DataFrame(data)
    if df.empty:
        return df
    df = df.rename(columns={"date": "timestamp"}).set_index("timestamp")
    if timeframe == "week":
        df = df.resample("W").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna()
    elif timeframe == "4hour":
        # Kite has no native 4-hour interval, so it's synthesized here by
        # resampling 60-minute candles into 4-hour blocks anchored to the
        # NSE session open (9:15 IST): 9:15-13:15, 13:15-15:30 (short bar).
        df = df.resample("4h", origin="start_day", offset="9h15min").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna()
    return df


def scan_watchlist(kite) -> list:
    """Returns a list of per-stock result dicts, or an error dict per
    stock if that symbol's data couldn't be fetched (e.g. bad symbol,
    rate limit) - one bad symbol never aborts the whole scan."""
    instruments = _load_instrument_map(kite)
    results = []
    for symbol in settings.WATCHLIST:
        token = instruments.get(symbol)
        if not token:
            results.append({"symbol": symbol, "error": "symbol not found on NSE"})
            continue
        try:
            df = fetch_candles(kite, token, settings.TIMEFRAME)
            if df.empty:
                results.append({"symbol": symbol, "error": "no candle data returned"})
                continue
            signal = compute_signal(df, settings.TIMEFRAME)
            signal["symbol"] = symbol
            results.append(signal)
        except Exception as exc:  # noqa: BLE001 - keep scanning the rest of the watchlist
            log.warning("Scan failed for %s: %s", symbol, exc)
            results.append({"symbol": symbol, "error": str(exc)})
    return results


def is_market_open() -> bool:
    now = dt.datetime.now()
    if now.weekday() >= 5:  # Sat/Sun
        return False
    open_t = now.replace(hour=9, minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t
